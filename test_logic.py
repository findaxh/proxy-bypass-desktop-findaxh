# -*- coding: utf-8 -*-
"""ProxyBypass 核心逻辑测试(临时改动注册表, 结束后自动恢复)"""
import os
import sys
import winreg

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import proxy_bypass as pb


def read_reg():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, pb.REG_KEY_PATH) as k:
            v, _ = winreg.QueryValueEx(k, pb.V_PROXY_OVERRIDE)
            return v
    except FileNotFoundError:
        return None


def write_reg(v):
    if v is None:
        pb.reg_delete_override()
    else:
        pb.reg_write_override(v)
    pb.notify_wininet()


def check(name, cond):
    print(("[PASS] " if cond else "[FAIL] ") + name)
    return bool(cond)


def main():
    backup = read_reg()
    print("backup ProxyOverride:", repr(backup))
    ok = True
    try:
        # ---- normalize_domain ----
        cases = [
            ("example.com", "example.com"),
            ("https://Example.com/path?x=1", "example.com"),
            ("*.example.com", "example.com"),
            ("WWW.Example.com:8080", "www.example.com"),
            ("  sub.domain.co.uk  ", "sub.domain.co.uk"),
            ("example.com.", "example.com"),
            ("", None),
            ("   ", None),
            ("http://", None),
            ("not a domain", None),
            ("nodot", None),
        ]
        for raw, expect in cases:
            got = pb.normalize_domain(raw)
            ok &= check("normalize(%r) -> %r (expect %r)" % (raw, got, expect),
                        got == expect)

        # ---- domain_to_entries ----
        e = pb.domain_to_entries("example.com")
        ok &= check("entries(example.com)=%r" % e,
                    e == ["example.com", "*.example.com"])
        e = pb.domain_to_entries("1.2.3.4")
        ok &= check("entries(1.2.3.4)=%r" % e, e == ["1.2.3.4"])

        # ---- controller lifecycle ----
        c = pb.BypassController()
        c.deactivate()  # 清理可能残留

        c.activate(["pb-test-a.com", "pb-test-b.org"])
        v = read_reg()
        print("after activate:", repr(v))
        ok &= check("activate writes entries",
                    v is not None and "pb-test-a.com" in v
                    and "*.pb-test-a.com" in v and "pb-test-b.org" in v
                    and "*.pb-test-b.org" in v)

        # 模拟 Clash 改写绕过列表
        write_reg("<local>;10.*")
        c.tick()
        v = read_reg()
        print("after tick:", repr(v))
        ok &= check("tick restores managed entries",
                    v is not None and "pb-test-a.com" in v
                    and "<local>" in v and "10.*" in v)

        # 暂停
        c.deactivate()
        v = read_reg()
        print("after deactivate:", repr(v))
        ok &= check("deactivate removes managed entries",
                    v is not None and "pb-test-a.com" not in v
                    and "<local>" in v and "10.*" in v)

        # 幂等
        c.activate(["pb-test-a.com"])
        v1 = read_reg()
        c.activate(["pb-test-a.com"])
        v2 = read_reg()
        ok &= check("activate idempotent", v1 == v2)

        # 崩溃恢复: 状态残留 + 注册表残留
        c.activate(["pb-test-a.com", "pb-test-c.net"])
        c2 = pb.BypassController()  # 模拟重启: 读取持久化状态
        c2.activate(["pb-test-a.com"])
        v = read_reg()
        print("after crash recovery:", repr(v))
        ok &= check("crash recovery drops stale entries",
                    v is not None and "pb-test-c.net" not in v
                    and "*.pb-test-c.net" not in v)
        ok &= check("crash recovery applies current entries",
                    v is not None and "pb-test-a.com" in v)

        c2.deactivate()
        v = read_reg()
        ok &= check("final cleanup", v is not None and "pb-test-a.com" not in v)
    finally:
        c3 = pb.BypassController()
        c3.deactivate()
        write_reg(backup)
        if os.path.exists(pb.STATE_FILE):
            os.remove(pb.STATE_FILE)
        print("restored ProxyOverride:", repr(read_reg()))

    print("=" * 40)
    print("ALL PASS" if ok else "SOME FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
