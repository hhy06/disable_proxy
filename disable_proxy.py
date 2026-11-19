#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
disable_proxy.py - 关闭 Windows 10 的系统代理（WinINET / “使用代理服务器”）

改的是注册表 HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings，
也就是「设置 -> 网络和 Internet -> 代理」这一页面对应的开关。

默认行为（等同于把代理页面上的开关全部关掉）：
    1. ProxyEnable  = 0        关闭“使用代理服务器”
    2. 删除 ProxyServer        清掉代理地址
    3. 删除 AutoConfigURL      清掉“使用设置脚本(PAC)” —— 不清的话代理仍会生效
    4. AutoDetect   = 0        关闭“自动检测设置”(WPAD)
    5. 通知系统刷新 WinINET 配置，已打开的程序（浏览器等）能立即感知

常用命令：
    python disable_proxy.py                  # 关闭代理
    python disable_proxy.py --dry-run        # 只查看当前状态，不修改任何东西
    python disable_proxy.py --keep-pac       # 保留 PAC 脚本地址（企业网常见）
    python disable_proxy.py --clear-env-user # 顺带清掉用户环境变量里的 HTTP(S)_PROXY
    python disable_proxy.py --reset-winhttp  # 顺带重置 WinHTTP 代理（需管理员）
    python disable_proxy.py --restore proxy_backup.json   # 从备份回滚

退出码：0 成功 / 1 出错 / 2 非 Windows 平台
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
from ctypes import wintypes
from datetime import datetime

try:
    import winreg
except ImportError:  # 非 Windows 平台
    winreg = None  # type: ignore[assignment]


# ---------------------------------------------------------------- 常量

IE_SETTINGS_KEY = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
USER_ENV_KEY = r"Environment"

# WinINET 刷新用的常量
INTERNET_OPTION_REFRESH = 37
INTERNET_OPTION_SETTINGS_CHANGED = 39
HWND_BROADCAST = 0xFFFF
WM_SETTINGCHANGE = 0x001A
SMTO_ABORTIFHUNG = 0x0002

# 需要清理的注册表值名
NAME_PROXY_ENABLE = "ProxyEnable"
NAME_PROXY_SERVER = "ProxyServer"
NAME_PROXY_OVERRIDE = "ProxyOverride"
NAME_AUTO_CONFIG_URL = "AutoConfigURL"
NAME_AUTO_DETECT = "AutoDetect"

# 会被各类命令行工具 / pip / git / docker 读取的代理环境变量
PROXY_ENV_VARS = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "FTP_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "ftp_proxy",
)


# ---------------------------------------------------------------- 注册表读写

def read_values() -> dict:
    """读取当前 Internet Settings 中与代理相关的值，返回 {名称: {"value":..,"type":..}}。"""
    result = {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, IE_SETTINGS_KEY, 0, winreg.KEY_READ) as key:
            for name in (NAME_PROXY_ENABLE, NAME_PROXY_SERVER, NAME_PROXY_OVERRIDE,
                         NAME_AUTO_CONFIG_URL, NAME_AUTO_DETECT):
                try:
                    value, vtype = winreg.QueryValueEx(key, name)
                    result[name] = {"value": value, "type": vtype}
                except FileNotFoundError:
                    continue
    except FileNotFoundError:
        pass
    return result


def write_value(name: str, value, vtype: int) -> None:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, IE_SETTINGS_KEY, 0,
                        winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as key:
        winreg.SetValueEx(key, name, 0, vtype, value)


def delete_value(name: str) -> bool:
    """删除某个值；不存在时返回 False。"""
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, IE_SETTINGS_KEY, 0,
                        winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as key:
        try:
            winreg.DeleteValue(key, name)
            return True
        except FileNotFoundError:
            return False


# ---------------------------------------------------------------- 系统通知

def notify_settings_changed() -> None:
    """通知 WinINET 重新加载代理配置，并向所有窗口广播 WM_SETTINGCHANGE。"""
    try:
        wininet = ctypes.WinDLL("wininet", use_last_error=True)
        wininet.InternetSetOptionW(None, INTERNET_OPTION_SETTINGS_CHANGED, None, 0)
        wininet.InternetSetOptionW(None, INTERNET_OPTION_REFRESH, None, 0)
    except OSError as exc:
        print(f"  ! 刷新 WinINET 配置失败（不影响注册表已生效）：{exc}")

    try:
        param = ctypes.c_wchar_p(IE_SETTINGS_KEY)
        out = wintypes.DWORD()
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_SETTINGCHANGE, 0, param,
            SMTO_ABORTIFHUNG, 1500, ctypes.byref(out),
        )
    except Exception:  # 广播失败无所谓，仅影响部分已打开的程序是否立刻刷新
        pass


# ---------------------------------------------------------------- 生效状态查询

class _IEProxyConfig(ctypes.Structure):
    _fields_ = [
        ("fAutoDetect", wintypes.BOOL),
        ("lpszAutoConfigUrl", wintypes.LPWSTR),
        ("lpszProxy", wintypes.LPWSTR),
        ("lpszProxyBypass", wintypes.LPWSTR),
    ]


def query_effective_proxy() -> dict | None:
    """用 WinHTTP API 查询系统实际生效的代理配置，返回 None 表示查询失败。"""
    try:
        winhttp = ctypes.WinDLL("winhttp", use_last_error=True)
        cfg = _IEProxyConfig()
        if not winhttp.WinHttpGetIEProxyConfigForCurrentUser(ctypes.byref(cfg)):
            return None
        return {
            "auto_detect": bool(cfg.fAutoDetect),
            "pac_url": cfg.lpszAutoConfigUrl or "",
            "proxy": cfg.lpszProxy or "",
            "bypass": cfg.lpszProxyBypass or "",
        }
    except Exception:
        return None


# ---------------------------------------------------------------- 展示

def format_types(values: dict) -> str:
    if not values:
        return "（注册表中没有任何代理相关值）"
    lines = []
    for name, item in values.items():
        lines.append(f"  {name:<15} = {item['value']!r}  (type={item['type']})")
    return "\n".join(lines)


def print_current(title: str = "当前状态") -> None:
    print(f"[{title}] 注册表 HKCU\\{IE_SETTINGS_KEY}")
    print(format_types(read_values()))

    effective = query_effective_proxy()
    print("[当前状态] 系统实际生效的代理配置")
    if effective is None:
        print("  （查询失败，可忽略）")
    else:
        proxy_on = bool(effective["proxy"]) or bool(effective["pac_url"]) or effective["auto_detect"]
        print(f"  是否走代理 : {'是' if proxy_on else '否'}")
        print(f"  手动代理   : {effective['proxy'] or '（无）'}")
        print(f"  PAC 脚本   : {effective['pac_url'] or '（无）'}")
        print(f"  自动检测   : {'开' if effective['auto_detect'] else '关'}")
        if effective["bypass"]:
            print(f"  例外列表   : {effective['bypass']}")


# ---------------------------------------------------------------- 备份 / 恢复

def backup_path_default() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "proxy_backup.json")


def read_user_env() -> dict:
    """读取当前用户已持久化的代理环境变量。"""
    found = {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, USER_ENV_KEY, 0, winreg.KEY_READ) as key:
            for name in PROXY_ENV_VARS:
                try:
                    found[name] = winreg.QueryValueEx(key, name)[0]
                except FileNotFoundError:
                    continue
    except FileNotFoundError:
        pass
    return found


def make_backup(path: str) -> None:
    data = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "registry": read_values(),
        "user_env": read_user_env(),
    }
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False, indent=2)
    print(f"[备份] 原有代理配置已保存到 {path}")


def restore_from(path: str) -> int:
    with open(path, "r", encoding="utf-8") as fp:
        data = json.load(fp)

    print(f"[恢复] 读取备份 {path}（创建于 {data.get('created_at', '未知时间')}）")
    for name, item in (data.get("registry") or {}).items():
        write_value(name, item["value"], int(item["type"]))
        print(f"  恢复 {name} = {item['value']!r}")

    env = data.get("user_env") or {}
    for name, value in env.items():
        set_user_env(name, value)
    if env:
        print(f"  恢复用户环境变量：{', '.join(env)}")

    notify_settings_changed()
    print_current("恢复后的状态")
    return 0


# ---------------------------------------------------------------- 环境变量

def set_user_env(name: str, value: str | None) -> None:
    """写入/删除当前用户的持久环境变量（HKCU\\Environment）。"""
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, USER_ENV_KEY, 0,
                        winreg.KEY_SET_VALUE) as key:
        if value is None:
            try:
                winreg.DeleteValue(key, name)
            except FileNotFoundError:
                pass
        else:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)


def clear_proxy_env(also_persist: bool) -> None:
    """清除当前进程 + 可选持久化的代理环境变量。"""
    removed = []
    for name in PROXY_ENV_VARS:
        if os.environ.pop(name, None) is not None:
            removed.append(name)
    print(f"[环境变量] 当前进程已移除：{', '.join(removed) if removed else '无'}")

    if also_persist:
        persisted = []
        for name in read_user_env():
            set_user_env(name, None)
            persisted.append(name)
        print(f"[环境变量] 已从用户环境变量删除：{', '.join(persisted) if persisted else '无'}")
        print("           注意：已打开的命令行窗口需要重开才会生效")


# ---------------------------------------------------------------- WinHTTP

def reset_winhttp_proxy() -> None:
    """重置 WinHTTP 代理（服务、部分后台程序用的是它，和 IE 代理是两套）。"""
    try:
        show = subprocess.run(["netsh", "winhttp", "show", "proxy"],
                              capture_output=True, text=True, timeout=20)
        if show.stdout.strip():
            print("[WinHTTP] 重置前：")
            for line in show.stdout.strip().splitlines():
                print(f"  {line.strip()}")
    except Exception:
        pass

    try:
        res = subprocess.run(["netsh", "winhttp", "reset", "proxy"],
                             capture_output=True, text=True, timeout=30)
    except Exception as exc:
        print(f"[WinHTTP] 执行失败：{exc}")
        return

    output = (res.stdout + res.stderr).strip()
    if res.returncode == 0:
        print("[WinHTTP] 已重置为「直接访问」(direct)")
    elif "管理员" in output or "elevat" in output.lower() or res.returncode == 5:
        print("[WinHTTP] 需要管理员权限，已跳过。请以管理员身份重新运行，"
              "或手动执行：netsh winhttp reset proxy")
    else:
        print(f"[WinHTTP] 重置失败（退出码 {res.returncode}）：{output}")


# ---------------------------------------------------------------- 主流程

def disable_proxy(args: argparse.Namespace) -> int:
    print_current("修改前")

    if args.dry_run:
        print("\n[dry-run] 未做任何修改。去掉 --dry-run 即会执行关闭。")
        return 0

    if not args.no_backup:
        make_backup(args.backup or backup_path_default())

    print("\n[执行] 关闭系统代理")
    write_value(NAME_PROXY_ENABLE, 0, winreg.REG_DWORD)
    print(f"  {NAME_PROXY_ENABLE} = 0")

    # 手动代理地址
    if delete_value(NAME_PROXY_SERVER):
        print(f"  已删除 {NAME_PROXY_SERVER}")

    # PAC 脚本：优先级高于手动代理，必须清掉才算“不用代理”
    if args.keep_pac:
        print(f"  保留 {NAME_AUTO_CONFIG_URL}（--keep-pac）")
    elif delete_value(NAME_AUTO_CONFIG_URL):
        print(f"  已删除 {NAME_AUTO_CONFIG_URL}")

    # WPAD 自动检测
    if args.keep_autodetect:
        print(f"  保留自动检测设置（--keep-autodetect）")
    else:
        write_value(NAME_AUTO_DETECT, 0, winreg.REG_DWORD)
        print(f"  {NAME_AUTO_DETECT} = 0")

    notify_settings_changed()

    if args.clear_env_user:
        clear_proxy_env(also_persist=True)
    elif args.clear_env:
        clear_proxy_env(also_persist=False)

    if args.reset_winhttp:
        reset_winhttp_proxy()

    print_current("修改后")

    effective = query_effective_proxy()
    if effective is not None:
        still = bool(effective["proxy"]) or bool(effective["pac_url"]) or effective["auto_detect"]
        print("\n[结果] " + ("⚠ 仍有代理配置生效，请检查上表。" if still else "✓ 系统代理已关闭，当前为直连。"))

    print("\n提示：已打开的浏览器一般会立即生效；部分程序（终端、IDE、服务）"
          "需要重启后才会读到新的系统代理设置。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="关闭 Windows 10 系统代理（WinINET / IE 代理设置），默认清掉手动代理、PAC 和自动检测。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python disable_proxy.py --dry-run\n"
               "  python disable_proxy.py\n"
               "  python disable_proxy.py --keep-pac --reset-winhttp\n"
               "  python disable_proxy.py --restore proxy_backup.json",
    )
    parser.add_argument("--dry-run", action="store_true", help="只查看当前代理状态，不做任何修改")
    parser.add_argument("--keep-pac", action="store_true",
                        help="保留 AutoConfigURL（PAC 自动配置脚本地址）")
    parser.add_argument("--keep-autodetect", action="store_true",
                        help="保留“自动检测设置”(WPAD)，默认会关闭")
    parser.add_argument("--clear-env", action="store_true",
                        help="仅清除当前进程的 HTTP(S)_PROXY 等环境变量")
    parser.add_argument("--clear-env-user", action="store_true",
                        help="同时从用户环境变量(HKCU\\Environment)中删除代理变量，影响之后新开的程序")
    parser.add_argument("--reset-winhttp", action="store_true",
                        help="同时执行 netsh winhttp reset proxy（需要管理员权限）")
    parser.add_argument("--no-backup", action="store_true", help="不生成备份文件")
    parser.add_argument("--backup", metavar="PATH", help="指定备份文件路径")
    parser.add_argument("--restore", metavar="PATH", help="从备份文件恢复原有代理设置后退出")
    return parser


def main() -> int:
    if sys.platform != "win32":
        print("本脚本只适用于 Windows。", file=sys.stderr)
        return 2

    args = build_parser().parse_args()

    if args.restore:
        try:
            return restore_from(args.restore)
        except FileNotFoundError:
            print(f"备份文件不存在：{args.restore}", file=sys.stderr)
            return 1
        except Exception as exc:
            print(f"恢复失败：{exc}", file=sys.stderr)
            return 1

    try:
        return disable_proxy(args)
    except PermissionError as exc:
        print(f"权限不足，无法写入注册表：{exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"执行出错：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
