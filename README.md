# ProxyBypass-Desktop

[中文](#中文) · [English](#english)

---

> ## ⚠️ 使用声明 / Usage Notice（请务必阅读 / Please read before use）
>
> **中文**
>
> Clash 代理分流助手说明：名单中的域名直接连接，不启用代理；其余域名流量遵循 Clash 配置规则分流。本工具使用 Clash 的目的仅为访问 Codex、ChatGPT 等人工智能相关服务。本人及任何使用者，必须将相关使用场景限定在科学研究或学业范畴内，仅允许用于实验设计、代码优化以及文本润色。
>
> **English**
>
> Clash proxy routing assistant: domains on the list connect directly with no proxy enabled; traffic to all other domains follows the routing rules configured in Clash. This tool uses Clash solely to access AI-related services such as Codex and ChatGPT. The author and every user of this tool must confine its use to scientific research or academic study, and only for experiment design, code optimization, and text polishing.

---

<a id="中文"></a>

## 中文

### 简介

ProxyBypass-Desktop 是一个 Windows 桌面工具，配合 Clash（Verge Rev 等）代理客户端，实现"指定域名不走代理"的分流管理。

程序运行时，将你记录的域名写入 Windows 系统代理的"绕过代理"列表（ProxyOverride）：这些域名的流量直接连接、不经过 Clash；其余域名仍按 Clash 规则分流。程序退出时自动恢复原始列表，无需频繁开关代理。

### 功能特性

- **域名分流管理**：添加主域名即自动覆盖其全部子域（如 `example.com` 含 `*.example.com`）
- **即时生效**：修改列表立即生效，无需重启 Clash
- **防覆盖自动补回**：Clash 每次开启系统代理都会重写绕过列表，程序每 2 秒检查一次，被改写立即补回（不破坏 Clash 的内网列表）
- **崩溃自愈**：异常退出后重启一次即自动恢复同步
- **死代理守护**：系统代理已开启但代理端口无响应（Clash 未运行或已崩溃）时，连续确认约 10 秒后自动关闭系统代理，全部域名直连；Clash 重新运行后由其自身恢复系统代理
- **状态自动检测**：页面显示的系统代理地址与端口实时读取注册表，非写死
- **便携配置**：配置文件存放在 exe 同级 `setting` 目录，整体拷贝即可迁移
- **托盘驻留**：系统托盘常驻 + 本地 Web 设置界面（仅本机可访问，带随机 token 防护）

### 使用方法

1. 启动 Clash（系统代理模式）
2. 运行 `ProxyBypass-Desktop.exe`，自动打开设置页面
3. 添加不走代理的域名（支持批量导入、直接粘贴网址）
4. 关闭网页标签不影响运行；右键托盘图标可暂停生效或退出

### 工作原理

程序把域名列表写入注册表 `HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings\ProxyOverride`（分号分隔）。使用系统代理的应用（浏览器等）对列表中的域名直连，其余流量进入 Clash 按规则分流。

### 注意事项

- 仅对使用系统代理的应用生效（浏览器等）
- 若 Clash 使用 TUN 模式，本方式不适用，需在 Clash 规则中为这些域名配置 `DIRECT`
- 仅在本机代理地址（127.0.0.1 / localhost / ::1）时启用死代理守护，避免误关企业代理
- 程序需保持运行（驻留托盘）

### 配置文件

配置文件位于 exe 同级 `setting` 目录：

| 文件                 | 说明          |
| ------------------ | ----------- |
| `domains.json`     | 域名列表（核心配置）  |
| `state.json`       | 运行状态（崩溃恢复用） |
| `proxy_bypass.log` | 运行日志        |

### 技术栈

Python 3 · pystray（托盘）· Pillow（图标）· stdlib http.server（本地设置页）· PyInstaller（打包）

---

<a id="english"></a>

## English

### Introduction

ProxyBypass-Desktop is a Windows desktop utility that works alongside Clash (Verge Rev, etc.) proxy clients to manage per-domain proxy bypass.

While running, it writes your domain list into the Windows system proxy bypass list (ProxyOverride). Traffic to these domains connects directly, bypassing Clash; all other domains follow Clash's rules as usual. The original list is restored automatically on exit — no more toggling the proxy on and off.

### Features

- **Domain bypass management**: add a root domain and all its subdomains are covered automatically (e.g. `example.com` includes `*.example.com`)
- **Instant effect**: changes take effect immediately, no Clash restart needed
- **Auto re-apply**: Clash rewrites the bypass list every time system proxy is enabled; the app checks every 2 seconds and restores your entries without touching Clash's own list
- **Crash self-healing**: restart once after an abnormal exit to re-sync automatically
- **Dead-proxy guard**: if system proxy is enabled but the proxy port does not respond (Clash not running or crashed), the app disables system proxy after ~10 seconds of continuous confirmation, so all domains go direct; Clash restores its own system proxy when it runs again
- **Live status detection**: the proxy address/port shown on the page is read from the registry in real time, never hardcoded
- **Portable config**: config files live in a `setting` folder next to the exe — copy the whole folder to migrate
- **Tray resident**: lives in the system tray with a localhost-only web settings page (random token protected)

### Usage

1. Start Clash (system proxy mode)
2. Run `ProxyBypass-Desktop.exe` — the settings page opens automatically
3. Add domains to bypass (batch import and URL paste supported)
4. Closing the browser tab keeps the app running; right-click the tray icon to pause or exit

### How It Works

The app writes the domain list into the registry key `HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings\ProxyOverride` (semicolon-separated). Applications using the system proxy (browsers, etc.) connect directly to listed domains; all other traffic goes through Clash and follows its rules.

### Notes

- Only affects applications that use the system proxy (browsers, etc.)
- If Clash runs in TUN mode, this approach does not apply — configure `DIRECT` rules in Clash instead
- The dead-proxy guard only applies to local proxy addresses (127.0.0.1 / localhost / ::1), so a corporate proxy is never disabled by mistake
- The app must keep running (tray resident)

### Configuration Files

Config files are stored in the `setting` folder next to the exe:

| File               | Description                        |
| ------------------ | ---------------------------------- |
| `domains.json`     | The domain list (core config)      |
| `state.json`       | Runtime state (for crash recovery) |
| `proxy_bypass.log` | Runtime log                        |

### Tech Stack

Python 3 · pystray (tray) · Pillow (icon) · stdlib http.server (local settings page) · PyInstaller (packaging)

---

## License / 协议

Licensed under the Apache License, Version 2.0. See LICENSE for the full text.  
本项目采用 Apache License 2.0 协议，完整文本见 LICENSE。

## AI 协助声明 / AI Assistance

本项目在开发过程中使用 Codex / 豆包 App/Workbuddy 协助编写与调试，所用账号为：  
This project was developed with assistance from Codex / Doubao App, using the following accounts:

<findaxh@outlook.com>  
<wangxuehao2024@163.com>

## Author / 作者

王雪豪  
Email: <findaxh@outlook.com>  
Version: v1.0  
Updated: 2026-10-19

