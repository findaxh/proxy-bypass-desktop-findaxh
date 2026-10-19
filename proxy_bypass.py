# -*- coding: utf-8 -*-
"""
ProxyBypass-Desktop — Clash 代理分流助手

原理
----
程序运行时, 将域名列表写入 Windows 系统代理的"绕过代理"列表
(HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings\\ProxyOverride)。
这些域名的流量不经过 Clash, 直接连接; 其余域名仍按 Clash 规则分流。
程序退出时自动恢复原始绕过列表; 若 Clash 改写列表, 监控线程会自动补回。

形态
----
系统托盘驻留 + 本地 Web 设置界面(127.0.0.1, 带随机 token, 仅本机可访问)。
不依赖 tkinter 等 GUI 库, 打包体积小。

死代理守护
----------
监控线程同时检测系统代理状态: 若系统代理已开启但代理端口无监听
(Clash 未运行或已崩溃), 连续确认约 10 秒后自动关闭系统代理,
此时全部流量直连; Clash 重新运行后由其自身恢复系统代理。

适用场景
--------
Clash Verge Rev 等客户端使用"系统代理"模式。若使用 TUN 模式, 本方式不适用,
需在 Clash 规则中为这些域名配置 DIRECT。
"""

import atexit
import ctypes
import json
import logging
import os
import re
import secrets
import socket
import sys
import threading
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from urllib.parse import parse_qs, urlparse

import winreg

try:
    import pystray
    from PIL import Image, ImageDraw, ImageFont
    HAS_TRAY = True
except ImportError:  # 允许在无依赖环境下导入(测试用)
    pystray = None
    HAS_TRAY = False

try:
    _RESAMPLE = Image.Resampling.LANCZOS
except AttributeError:
    _RESAMPLE = Image.LANCZOS

APP_NAME = "ProxyBypass-Desktop"
APP_TITLE = "ProxyBypass-Desktop — 代理分流助手"

REG_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
V_PROXY_ENABLE = "ProxyEnable"
V_PROXY_SERVER = "ProxyServer"
V_PROXY_OVERRIDE = "ProxyOverride"
RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _setting_dir():
    """配置文件目录: exe(或脚本)同级的 setting 目录(便携模式)"""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(os.path.abspath(sys.executable))
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "setting")


SETTING_DIR = _setting_dir()
DOMAINS_FILE = os.path.join(SETTING_DIR, "domains.json")
STATE_FILE = os.path.join(SETTING_DIR, "state.json")
LOG_FILE = os.path.join(SETTING_DIR, "proxy_bypass.log")

WEB_PORT = 17897          # 首选端口, 被占用时自动改用随机端口
MONITOR_INTERVAL = 2      # 监控线程间隔(秒)
IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")

log = logging.getLogger("proxy_bypass")


# ---------------------------------------------------------------- 注册表操作

def reg_read_override():
    """读取 ProxyOverride 字符串; 不存在返回 None"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY_PATH) as key:
            value, _ = winreg.QueryValueEx(key, V_PROXY_OVERRIDE)
            return value
    except FileNotFoundError:
        return None
    except OSError:
        log.exception("读取 ProxyOverride 失败")
        return None


def reg_write_override(value):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY_PATH, 0,
                        winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, V_PROXY_OVERRIDE, 0, winreg.REG_SZ, value)


def reg_delete_override():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY_PATH, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, V_PROXY_OVERRIDE)
    except FileNotFoundError:
        pass


def reg_read_proxy_state():
    """返回 (ProxyEnable:int|None, ProxyServer:str)"""
    enable, server = None, ""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY_PATH) as key:
            try:
                enable, _ = winreg.QueryValueEx(key, V_PROXY_ENABLE)
            except FileNotFoundError:
                pass
            try:
                server, _ = winreg.QueryValueEx(key, V_PROXY_SERVER)
            except FileNotFoundError:
                pass
    except OSError:
        log.exception("读取代理状态失败")
    return enable, server


def _is_local_proxy(server):
    """代理服务器是否为本机地址(仅本机代理才做死代理检测)"""
    if not server:
        return False
    s = server.strip().lower()
    return (s.startswith("127.") or s.startswith("localhost")
            or s.startswith("[::1]"))


def _parse_proxy_server(server):
    """从 ProxyServer 提取 (host, port); 失败返回 (None, None)"""
    if not server:
        return None, None
    m = re.search(r"(\d{1,3}(?:\.\d{1,3}){3}|localhost|\[::1\]):(\d{1,5})",
                  server.strip())
    if not m:
        return None, None
    host = m.group(1).strip("[]")
    return host, int(m.group(2))


def _port_is_listening(host, port, timeout=1.0):
    """检测端口是否有服务监听"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _set_proxy_enable(value):
    """设置 ProxyEnable(0=关闭系统代理, 1=开启)"""
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY_PATH, 0,
                        winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, V_PROXY_ENABLE, 0, winreg.REG_DWORD, int(value))
    notify_wininet()


def notify_wininet():
    """通知 WinINET 代理设置已变更, 使新设置立即对新连接生效"""
    try:
        ctypes.windll.wininet.InternetSetOptionW(0, 39, 0, 0)  # SETTINGS_CHANGED
        ctypes.windll.wininet.InternetSetOptionW(0, 37, 0, 0)  # REFRESH
    except Exception:
        log.debug("WinINET 通知失败(可忽略)", exc_info=True)


# ---------------------------------------------------------------- 域名处理

def normalize_domain(raw):
    """把用户输入规范化为域名(小写, 去协议/路径/端口/通配前缀)。无法识别返回 None。"""
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if not s:
        return None
    if "://" in s:
        s = s.split("://", 1)[1]
    s = s.split("/", 1)[0]
    s = s.split("?", 1)[0]
    s = s.split("#", 1)[0]
    if s.startswith("*."):
        s = s[2:]
    elif s.startswith("*"):
        s = s.lstrip("*")
    if ":" in s:  # 去端口(仅当端口为数字)
        host, _, port = s.rpartition(":")
        if port.isdigit():
            s = host
    s = s.strip().strip(".")
    if not s:
        return None
    if ":" in s:
        return None
    if any(c in s for c in " \t;,/\\@"):
        return None
    if s == "localhost":
        return s
    if "." not in s:
        return None
    return s


def domain_to_entries(domain):
    """域名 -> ProxyOverride 条目(含子域通配)"""
    if IPV4_RE.match(domain):
        return [domain]
    return [domain, "*." + domain]


def parse_override(value):
    if not value:
        return []
    return [e.strip() for e in value.split(";") if e.strip()]


# ---------------------------------------------------------------- 域名存储

def load_domains():
    try:
        with open(DOMAINS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [d for d in data.get("domains", []) if isinstance(d, str) and d]
    except Exception:
        return []


def save_domains(domains):
    os.makedirs(SETTING_DIR, exist_ok=True)
    tmp = DOMAINS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"domains": domains}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DOMAINS_FILE)


# ---------------------------------------------------------------- 分流控制器

class BypassController:
    """管理 ProxyOverride: 生效时把域名条目并入, 暂停时恢复原值。

    状态持久化在 state.json: 程序异常退出后重启可自愈
    (重新读取注册表, 剥离上次残留的托管条目, 再按当前域名列表生效)。
    """

    def __init__(self):
        self.lock = threading.RLock()
        self.state = self._load_state()

    def _load_state(self):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                s = json.load(f)
            if not isinstance(s, dict):
                raise ValueError("bad state file")
        except Exception:
            s = {}
        return {
            "original_existed": bool(s.get("original_existed", True)),
            "original_entries": list(s.get("original_entries") or []),
            "managed": list(s.get("managed") or []),
            "active": bool(s.get("active", False)),
        }

    def _save_state(self):
        try:
            os.makedirs(SETTING_DIR, exist_ok=True)
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
            os.replace(tmp, STATE_FILE)
        except Exception:
            log.exception("保存状态失败")

    def _read_current(self):
        raw = reg_read_override()
        return parse_override(raw), raw is not None

    def _write_entries(self, entries, existed):
        if entries:
            reg_write_override(";".join(entries))
        elif existed:
            reg_write_override("")
        else:
            reg_delete_override()
        notify_wininet()

    @property
    def is_active(self):
        with self.lock:
            return self.state.get("active", False)

    @property
    def managed_count(self):
        with self.lock:
            return len(self.state.get("managed", []))

    def activate(self, domains):
        """使域名列表生效(可重复调用, 自动同步最新列表)"""
        with self.lock:
            cur, existed = self._read_current()
            old_managed = self.state.get("managed", [])
            base = [e for e in cur if e not in old_managed]
            managed = []
            for d in domains:
                for e in domain_to_entries(d):
                    if e not in managed:
                        managed.append(e)
            expected = base + [e for e in managed if e not in base]
            self.state = {
                "original_existed": existed,
                "original_entries": base,
                "managed": managed,
                "active": True,
            }
            if expected != cur:
                self._write_entries(expected, existed)
                log.info("已生效: %d 个域名 / %d 条规则", len(domains), len(managed))
            self._save_state()

    def deactivate(self):
        """暂停生效: 移除托管条目, 恢复原始绕过列表"""
        with self.lock:
            if not self.state.get("active"):
                return
            cur, existed = self._read_current()
            managed = self.state.get("managed", [])
            base = [e for e in cur if e not in managed]
            self.state["managed"] = []
            self.state["active"] = False
            self.state["original_entries"] = base
            self.state["original_existed"] = existed
            if base != cur:
                self._write_entries(base, existed)
                log.info("已暂停, 绕过列表已恢复")
            self._save_state()

    def tick(self):
        """监控调用: 若绕过列表被外部(如 Clash)改写, 增量补全托管条目"""
        with self.lock:
            if not self.state.get("active"):
                return
            cur, existed = self._read_current()
            managed = self.state.get("managed", [])
            missing = [e for e in managed if e not in cur]
            if not missing:
                return
            base = [e for e in cur if e not in managed]
            expected = base + [e for e in managed if e not in base]
            self.state["original_entries"] = base
            self.state["original_existed"] = existed
            self._write_entries(expected, existed)
            self._save_state()
            log.info("检测到绕过列表被外部修改, 已重新生效 (%d 条)", len(missing))


# 守护状态(供界面显示): alive=None 表示未检测/无需检测, True/False 为最近一次端口探测结果
GUARD = {"alive": None, "server": ""}


def monitor_loop(controller, stop_event):
    dead_count = 0
    while not stop_event.wait(MONITOR_INTERVAL):
        try:
            controller.tick()
            # 死代理守护: 系统代理开启但代理端口无监听(Clash 未运行/已崩溃)时,
            # 连续确认约 10 秒后关闭系统代理, 此时全部流量直连
            enable, server = reg_read_proxy_state()
            if enable == 1 and _is_local_proxy(server):
                host, port = _parse_proxy_server(server)
                alive = _port_is_listening(host, port) if (host and port) else None
                GUARD["alive"] = alive
                GUARD["server"] = server or ""
                if host and port and not alive:
                    dead_count += 1
                    if dead_count >= 5:  # 连续 5 次(约 10 秒)
                        _set_proxy_enable(0)
                        log.info("代理端口 %s:%d 无响应(Clash 未运行), "
                                 "已关闭系统代理, 全部直连", host, port)
                        dead_count = 0
                else:
                    dead_count = 0
            else:
                dead_count = 0
                GUARD["alive"] = None
                GUARD["server"] = server or ""
        except Exception:
            log.exception("监控循环异常")


# ---------------------------------------------------------------- 图标

def _load_script_font(px):
    """加载花体字体(按优先级), 全部失败返回 None"""
    candidates = [
        "segoesc.ttf",     # Segoe Script
        "lhandw.ttf",      # Lucida Handwriting
        "FRSCRIPT.TTF",    # Freestyle Script
    ]
    fonts_dir = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    for name in candidates:
        path = os.path.join(fonts_dir, name)
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, px)
            except Exception:
                continue
    return None


def _fit_script_text(draw, text, max_w, max_h, start_px):
    """按最大宽高自动缩小花体字号, 返回 (font, bbox); 无字体返回 None"""
    px = int(start_px)
    font = _load_script_font(px)
    if font is None:
        return None
    bbox = draw.textbbox((0, 0), text, font=font)
    while px > 10:
        w = bbox[2] - bbox[0]
        h = bbox[3] - bbox[1]
        if w <= max_w and h <= max_h:
            break
        px = int(px * 0.92)
        font = _load_script_font(px)
        if font is None:
            return None
        bbox = draw.textbbox((0, 0), text, font=font)
    return font, bbox


def create_icon_image(size=64):
    """生成图标: 蓝色圆角方块 + 白色花体 PB"""
    S = size * 4  # 超采样抗锯齿
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([S * 0.03, S * 0.03, S * 0.97, S * 0.97],
                        radius=S * 0.22, fill=(37, 99, 235, 255))
    fit = _fit_script_text(d, "PB", S * 0.86, S * 0.60, S * 0.72)
    if fit is not None:
        font, bbox = fit
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        x = (S - tw) / 2 - bbox[0]
        y = (S - th) / 2 - bbox[1]
        d.text((x, y), "PB", font=font, fill=(255, 255, 255, 255))
    return img.resize((size, size), _RESAMPLE)


# ---------------------------------------------------------------- 核心状态

class Core:
    """域名列表 + 控制器 + 日志缓冲, 供 Web 界面与托盘共享"""

    def __init__(self):
        self.controller = BypassController()
        self.lock = threading.RLock()
        self.domains = load_domains()
        self.log_lines = deque(maxlen=100)
        self.autostart = is_autostart_enabled()

    # ---- 域名操作 ----
    def add_domain(self, raw):
        d = normalize_domain(raw)
        if not d:
            return False, "无法识别该输入, 请输入域名(如 example.com)", None
        with self.lock:
            if d in self.domains:
                return False, "%s 已在列表中" % d, d
            self.domains.append(d)
            self._persist_and_sync()
        return True, "已添加 %s" % d, d

    def remove_domain(self, d):
        with self.lock:
            if d in self.domains:
                self.domains.remove(d)
                self._persist_and_sync()
                return True
        return False

    def clear_domains(self):
        with self.lock:
            self.domains = []
            self._persist_and_sync()

    def batch_import(self, text):
        added = 0
        with self.lock:
            for line in (text or "").splitlines():
                d = normalize_domain(line)
                if d and d not in self.domains:
                    self.domains.append(d)
                    added += 1
            if added:
                self._persist_and_sync()
        return added

    def _persist_and_sync(self):
        save_domains(self.domains)
        if self.controller.is_active:
            self.controller.activate(self.domains)

    # ---- 开关 ----
    def pause(self):
        self.controller.deactivate()

    def resume(self):
        self.controller.activate(self.domains)

    def set_autostart(self, enable):
        set_autostart(enable)
        self.autostart = is_autostart_enabled()

    # ---- 状态 ----
    def append_log(self, msg):
        self.log_lines.append(msg)

    def status(self):
        with self.lock:
            enable, server = reg_read_proxy_state()
            raw = reg_read_override() or ""
            return {
                "proxy_enable": enable,
                "proxy_server": server,
                "active": self.controller.is_active,
                "domains": list(self.domains),
                "managed_count": self.controller.managed_count,
                "autostart": self.autostart,
                "proxy_alive": GUARD["alive"],
                "override": raw[:600],
                "log": list(self.log_lines)[-30:],
            }


class CoreLogHandler(logging.Handler):
    """把日志写入 Core 的缓冲(供 Web 界面显示)"""

    def __init__(self, core):
        super().__init__()
        self._core = core
        self.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))

    def emit(self, record):
        try:
            self._core.append_log(self.format(record))
        except Exception:
            pass


# ---------------------------------------------------------------- 开机自启

def autostart_command():
    if getattr(sys, "frozen", False):
        return '"%s" --autostart' % sys.executable
    return '"%s" "%s" --autostart' % (sys.executable, os.path.abspath(__file__))


def is_autostart_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH) as key:
            value, _ = winreg.QueryValueEx(key, APP_NAME)
            return bool(value)
    except FileNotFoundError:
        return False


def set_autostart(enable):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0,
                        winreg.KEY_SET_VALUE) as key:
        if enable:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass


# ---------------------------------------------------------------- Web 界面

PAGE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ProxyBypass-Desktop 代理分流助手</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect x='4' y='4' width='56' height='56' rx='14' fill='%232563eb'/%3E%3Ccircle cx='32' cy='32' r='13' fill='none' stroke='white' stroke-width='5'/%3E%3C/svg%3E">
<style>
:root{--accent:#2563eb;--bg:#f3f5f9;--panel:#fff;--text:#1f2937;--muted:#6b7280;--border:#e5e7eb;--ok:#16a34a;--warn:#d97706;--danger:#dc2626}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:"Segoe UI","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--text);padding:24px;max-width:880px;margin:0 auto}
header{margin-bottom:18px;display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
h1{font-size:22px}
.subtitle{color:var(--muted);font-size:13px;flex:1 0 100%;line-height:1.75;margin-top:2px}
.subtitle .notice{display:block;margin-top:5px;color:var(--text)}
.signature{font-family:"Segoe Script","Lucida Handwriting","Freestyle Script","Microsoft YaHei",cursive;font-size:13px;color:var(--accent);white-space:nowrap}
.cards{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:16px}
.card{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:14px 16px}
.card-label{font-size:12px;color:var(--muted);margin-bottom:6px}
.card-value{font-size:15px;font-weight:600;word-break:break-all}
.card-value.ok{color:var(--ok)}
.card-value.warn{color:var(--warn)}
.panel{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:16px;margin-bottom:16px}
.panel h2{font-size:15px;margin-bottom:12px}
.add-row{display:flex;gap:8px;margin-bottom:12px}
.add-row input{flex:1;padding:8px 12px;border:1px solid var(--border);border-radius:8px;font-size:14px;outline:none;font-family:Consolas,monospace}
.add-row input:focus{border-color:var(--accent)}
button{padding:8px 16px;border:none;border-radius:8px;background:var(--accent);color:#fff;font-size:14px;cursor:pointer;white-space:nowrap}
button:hover{opacity:.88}
button.secondary{background:#e5e7eb;color:var(--text)}
ul#domain-list{list-style:none;max-height:300px;overflow-y:auto;border:1px solid var(--border);border-radius:8px}
ul#domain-list li{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:8px 12px;border-bottom:1px solid var(--border);font-family:Consolas,monospace;font-size:13px}
ul#domain-list li:last-child{border-bottom:none}
ul#domain-list li.empty{color:var(--muted);justify-content:center;font-family:inherit;padding:20px}
button.del{background:transparent;color:var(--danger);padding:4px 10px;font-size:12px}
button.del:hover{background:#fee2e2;opacity:1}
.ops{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.ops .spacer{flex:1}
label.autostart{display:flex;align-items:center;gap:6px;font-size:14px;cursor:pointer;user-select:none}
pre#log{background:#f8fafc;border:1px solid var(--border);border-radius:8px;padding:12px;font-size:12px;line-height:1.6;max-height:170px;overflow-y:auto;white-space:pre-wrap;font-family:Consolas,monospace;color:var(--muted)}
.hint{color:var(--muted);font-size:13px;margin-bottom:10px}
details.panel summary{cursor:pointer;font-size:14px;font-weight:600}
details.panel ul{margin:10px 0 0 18px;color:var(--muted);font-size:13px;line-height:1.9}
.modal-mask{display:none;position:fixed;inset:0;background:rgba(15,23,42,.45);align-items:center;justify-content:center;z-index:10}
.modal-mask.show{display:flex}
.modal{background:#fff;border-radius:12px;padding:20px;width:480px;max-width:92vw;box-shadow:0 20px 50px rgba(0,0,0,.2)}
.modal h2{font-size:16px;margin-bottom:4px}
.modal textarea{width:100%;height:200px;border:1px solid var(--border);border-radius:8px;padding:10px;font-family:Consolas,monospace;font-size:13px;margin:12px 0;resize:vertical;outline:none}
.modal textarea:focus{border-color:var(--accent)}
</style>
</head>
<body>
<header>
  <h1>ProxyBypass-Desktop</h1>
  <span class="signature">by 王雪豪</span>
  <span class="subtitle">Clash 代理分流助手说明：名单中的域名直接连接，不启用代理；其余域名流量遵循 Clash 配置规则分流。<span class="notice">本工具使用 Clash 的目的仅为访问 Codex、ChatGPT 等人工智能相关服务。本人及任何使用者，必须将相关使用场景限定在科学研究或学业范畴内，仅允许用于实验设计、代码优化以及文本润色。</span></span>
</header>

<section class="cards">
  <div class="card"><div class="card-label">系统代理</div><div class="card-value" id="proxy">—</div></div>
  <div class="card"><div class="card-label">分流状态</div><div class="card-value" id="state">—</div></div>
  <div class="card"><div class="card-label">托管规模</div><div class="card-value" id="count">—</div></div>
  <div class="card"><div class="card-label">Clash 端口</div><div class="card-value" id="alive">—</div></div>
</section>

<section class="panel">
  <h2>不走代理的域名</h2>
  <div class="add-row">
    <input id="domain-input" placeholder="输入主域名, 如 example.com (自动覆盖子域), 也可粘贴网址">
    <button id="add-btn">添加</button>
  </div>
  <ul id="domain-list"></ul>
  <div class="ops" style="margin-top:12px">
    <button class="secondary" id="import-btn">批量导入</button>
    <button class="secondary" id="clear-btn">全部清空</button>
    <span class="spacer"></span>
    <label class="autostart"><input type="checkbox" id="autostart"> 开机自启</label>
    <button id="pause-btn">暂停生效</button>
  </div>
</section>

<section class="panel">
  <h2>最近日志</h2>
  <pre id="log"></pre>
</section>

<details class="panel">
  <summary>工作原理与注意事项</summary>
  <ul>
    <li>程序把列表中的域名写入 Windows 系统代理的"绕过代理"列表, 这些域名的流量不经过 Clash, 其余域名仍按 Clash 规则分流。</li>
    <li>添加主域名即可, 自动覆盖其全部子域(如 example.com 含 *.example.com)。</li>
    <li>修改列表立即生效, 无需重启 Clash; 若 Clash 改写绕过列表, 程序会在约 2 秒内自动补回。</li>
    <li>仅对使用系统代理的应用生效(浏览器等)。</li>
    <li>死代理守护: 若系统代理处于开启状态但代理端口无响应(Clash 未运行/已崩溃), 程序在连续确认约 10 秒后自动关闭系统代理, 此时全部域名直连; Clash 重新运行后会由其自身恢复系统代理。</li>
    <li>若 Clash 使用 TUN 模式, 本方式不适用, 需在 Clash 规则中为这些域名配置 DIRECT。</li>
    <li>程序需保持运行(驻留托盘); 退出时自动恢复原列表。</li>
  </ul>
</details>

<div id="import-mask" class="modal-mask">
  <div class="modal">
    <h2>批量导入域名</h2>
    <p class="hint">每行一个域名, 支持 example.com 或 https://example.com/path</p>
    <textarea id="import-text" placeholder="example.com&#10;https://sub.example.com/path&#10;*.cdn.example.net"></textarea>
    <div class="ops" style="justify-content:flex-end">
      <button class="secondary" id="import-cancel">取消</button>
      <button id="import-ok">导入</button>
    </div>
  </div>
</div>

<script>
const TOKEN = "__TOKEN__";

async function api(path, method, body) {
  const opts = {method: method || "GET", headers: {"X-Token": TOKEN}};
  if (body !== undefined && body !== null) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || ("HTTP " + r.status));
  return data;
}

async function refresh() {
  let s;
  try { s = await api("/api/status"); }
  catch (e) { return; }
  const pe = document.getElementById("proxy");
  pe.textContent = s.proxy_enable === 1
    ? ("已开启 " + (s.proxy_server || "")).trim() : "未开启";
  pe.className = "card-value " + (s.proxy_enable === 1 ? "ok" : "warn");
  const st = document.getElementById("state");
  st.textContent = s.active ? "生效中" : "已暂停";
  st.className = "card-value " + (s.active ? "ok" : "warn");
  document.getElementById("count").textContent =
    s.domains.length + " 个域名 / " + s.managed_count + " 条规则";
  const al = document.getElementById("alive");
  if (s.proxy_enable !== 1) {
    al.textContent = "未使用(全部直连)";
    al.className = "card-value warn";
  } else if (s.proxy_alive === true) {
    al.textContent = "正常 — 分流生效";
    al.className = "card-value ok";
  } else if (s.proxy_alive === false) {
    al.textContent = "无响应 — 即将转直连";
    al.className = "card-value warn";
  } else {
    al.textContent = "检测中";
    al.className = "card-value";
  }
  const ul = document.getElementById("domain-list");
  ul.innerHTML = "";
  if (s.domains.length === 0) {
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent = "暂无域名 — 添加后, 这些域名将不走代理";
    ul.appendChild(li);
  } else {
    for (const d of s.domains) {
      const li = document.createElement("li");
      const span = document.createElement("span");
      span.textContent = d;
      const btn = document.createElement("button");
      btn.className = "del";
      btn.textContent = "删除";
      btn.onclick = () => removeDomain(d);
      li.appendChild(span);
      li.appendChild(btn);
      ul.appendChild(li);
    }
  }
  document.getElementById("pause-btn").textContent = s.active ? "暂停生效" : "恢复生效";
  document.getElementById("autostart").checked = !!s.autostart;
  document.getElementById("log").textContent = (s.log || []).join("\\n");
}

async function addDomain() {
  const input = document.getElementById("domain-input");
  const v = input.value.trim();
  if (!v) return;
  try {
    const r = await api("/api/domains", "POST", {domain: v});
    if (r.ok) { input.value = ""; }
    else { alert(r.msg || "添加失败"); }
  } catch (e) { alert(String(e)); }
  refresh();
}

async function removeDomain(d) {
  await api("/api/domains/remove", "POST", {domain: d});
  refresh();
}

async function clearAll() {
  if (!confirm("确定清空全部域名?")) return;
  await api("/api/clear", "POST", {});
  refresh();
}

async function togglePause() {
  const btn = document.getElementById("pause-btn");
  if (btn.textContent === "暂停生效") await api("/api/pause", "POST", {});
  else await api("/api/resume", "POST", {});
  refresh();
}

async function toggleAutostart() {
  const cb = document.getElementById("autostart");
  await api("/api/autostart", "POST", {enable: cb.checked});
  refresh();
}

function openImport() { document.getElementById("import-mask").classList.add("show"); }
function closeImport() { document.getElementById("import-mask").classList.remove("show"); }
async function doImport() {
  const text = document.getElementById("import-text").value;
  const r = await api("/api/import", "POST", {text: text});
  closeImport();
  document.getElementById("import-text").value = "";
  alert("新增 " + r.added + " 个域名");
  refresh();
}

document.getElementById("add-btn").onclick = addDomain;
document.getElementById("domain-input").addEventListener("keydown",
  e => { if (e.key === "Enter") addDomain(); });
document.getElementById("clear-btn").onclick = clearAll;
document.getElementById("import-btn").onclick = openImport;
document.getElementById("import-cancel").onclick = closeImport;
document.getElementById("import-ok").onclick = doImport;
document.getElementById("pause-btn").onclick = togglePause;
document.getElementById("autostart").onchange = toggleAutostart;

setInterval(refresh, 2000);
refresh();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "ProxyBypass/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass  # 静默默认访问日志

    # ---- 基础响应 ----
    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self._write(body)

    def _send_html(self, html, code=200):
        body = html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self._write(body)

    def _write(self, body):
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ---- 鉴权: 仅本机 + token ----
    def _authorized(self, parsed):
        host = (self.headers.get("Host") or "").lower().split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            return False
        qs = parse_qs(parsed.query)
        token = (qs.get("token") or [""])[0] or self.headers.get("X-Token") or ""
        return bool(token) and token == self.server.token

    # ---- 路由 ----
    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/":
                if not self._authorized(parsed):
                    self._send_json({"error": "forbidden"}, 403)
                    return
                self._send_html(PAGE_HTML.replace("__TOKEN__", self.server.token))
            elif parsed.path == "/api/status":
                if not self._authorized(parsed):
                    self._send_json({"error": "forbidden"}, 403)
                    return
                self._send_json(self.server.core.status())
            else:
                self._send_json({"error": "not found"}, 404)
        except Exception:
            log.exception("Web GET 处理失败")
            try:
                self._send_json({"error": "internal"}, 500)
            except Exception:
                pass

    def do_POST(self):
        try:
            parsed = urlparse(self.path)
            if not self._authorized(parsed):
                self._send_json({"error": "forbidden"}, 403)
                return
            length = min(int(self.headers.get("Content-Length") or 0),
                         1024 * 1024)
            raw = self.rfile.read(length) if length else b""
            try:
                data = json.loads(raw.decode("utf-8")) if raw else {}
            except Exception:
                data = {}
            core = self.server.core
            if parsed.path == "/api/domains":
                ok, msg, d = core.add_domain(data.get("domain", ""))
                self._send_json({"ok": ok, "msg": msg, "domain": d})
            elif parsed.path == "/api/domains/remove":
                core.remove_domain(data.get("domain", ""))
                self._send_json({"ok": True})
            elif parsed.path == "/api/clear":
                core.clear_domains()
                self._send_json({"ok": True})
            elif parsed.path == "/api/import":
                added = core.batch_import(data.get("text", ""))
                self._send_json({"ok": True, "added": added})
            elif parsed.path == "/api/pause":
                core.pause()
                self._send_json({"ok": True})
            elif parsed.path == "/api/resume":
                core.resume()
                self._send_json({"ok": True})
            elif parsed.path == "/api/autostart":
                core.set_autostart(bool(data.get("enable")))
                self._send_json({"ok": True})
            else:
                self._send_json({"error": "not found"}, 404)
        except Exception:
            log.exception("Web POST 处理失败")
            try:
                self._send_json({"error": "internal"}, 500)
            except Exception:
                pass


class WebServer:
    def __init__(self, core):
        self.core = core
        self.token = secrets.token_hex(16)
        self.httpd = None
        self.port = None
        self.thread = None

    def start(self):
        for port in (WEB_PORT, 0):
            try:
                self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
                break
            except OSError:
                continue
        if self.httpd is None:
            raise RuntimeError("无法启动本地 Web 服务")
        self.httpd.core = self.core
        self.httpd.token = self.token
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True, name="web")
        self.thread.start()

    def url(self):
        return "http://127.0.0.1:%d/?token=%s" % (self.port, self.token)

    def stop(self):
        if self.httpd is not None:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:
                pass


# ---------------------------------------------------------------- 托盘

def build_menu(core, webserver, quit_event):
    def open_settings(_item=None):
        try:
            webbrowser.open(webserver.url())
        except Exception:
            log.exception("打开浏览器失败")

    def pause_text(_item):
        return "暂停生效" if core.controller.is_active else "恢复生效"

    def toggle(_item=None):
        if core.controller.is_active:
            core.pause()
        else:
            core.resume()

    def quit_app(_item=None):
        quit_event.set()

    return pystray.Menu(
        pystray.MenuItem("打开设置", open_settings, default=True),
        pystray.MenuItem(pause_text, toggle),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", quit_app),
    )


# ---------------------------------------------------------------- 入口

_MUTEX = None


def ensure_single_instance():
    global _MUTEX
    _MUTEX = ctypes.windll.kernel32.CreateMutexW(None, True,
                                                 "ProxyBypass_Desktop_SingleInstance")
    return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def show_message(title, text):
    try:
        ctypes.windll.user32.MessageBoxW(0, text, title, 0x40)
    except Exception:
        pass


def setup_logging():
    os.makedirs(SETTING_DIR, exist_ok=True)
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")
    fh = RotatingFileHandler(LOG_FILE, maxBytes=1024 * 1024, backupCount=1,
                             encoding="utf-8")
    fh.setFormatter(fmt)
    root_logger.addHandler(fh)


def _cleanup(core, webserver, stop_event, monitor, icon):
    stop_event.set()
    try:
        monitor.join(timeout=3)
    except Exception:
        pass
    try:
        was_active = core.controller.is_active
        core.controller.deactivate()
        if was_active:
            log.info("程序退出, 绕过列表已恢复")
    except Exception:
        log.exception("退出清理失败")
    try:
        webserver.stop()
    except Exception:
        pass
    if icon is not None:
        try:
            icon.stop()
        except Exception:
            pass


def main():
    setup_logging()
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    autostart_mode = "--autostart" in sys.argv

    if not ensure_single_instance():
        show_message(APP_NAME, "程序已在运行, 请查看系统托盘图标。")
        return

    core = Core()
    # 尽早挂接 UI 日志处理器, 使启动信息也显示在设置页面
    logging.getLogger().addHandler(CoreLogHandler(core))

    core.controller.activate(core.domains)
    log.info("程序启动: %d 个域名", len(core.domains))

    enable, server = reg_read_proxy_state()
    if enable == 1:
        log.info("系统代理: 已开启 (%s)", server or "")
    else:
        log.info("系统代理: 未开启")

    webserver = WebServer(core)
    webserver.start()
    log.info("设置界面: %s", webserver.url())

    stop_event = threading.Event()
    monitor = threading.Thread(target=monitor_loop,
                               args=(core.controller, stop_event),
                               daemon=True, name="monitor")
    monitor.start()

    quit_event = threading.Event()
    icon = None
    if HAS_TRAY:
        icon = pystray.Icon(APP_NAME, create_icon_image(), APP_TITLE,
                            menu=build_menu(core, webserver, quit_event))
        icon_thread = threading.Thread(target=icon.run, daemon=True,
                                       name="tray")
        icon_thread.start()

    if not autostart_mode:
        # 手动启动时自动打开设置界面
        try:
            webbrowser.open(webserver.url())
        except Exception:
            log.exception("打开浏览器失败")

    atexit.register(_cleanup, core, webserver, stop_event, monitor, icon)

    try:
        quit_event.wait()  # 等待托盘"退出"
    finally:
        _cleanup(core, webserver, stop_event, monitor, icon)


if __name__ == "__main__":
    main()
