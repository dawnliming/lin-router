from __future__ import annotations

import argparse
import ctypes
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any, Dict, Optional

from app import DEFAULT_CONFIG_FILE, DEFAULT_PUBLIC_API_KEY, DEFAULT_START_PORT, create_server
from settings_store import SettingsStore

HOST = "127.0.0.1"
APP_TITLE = "Lin Router"

# Windows API constants for single-instance guard
ERROR_ALREADY_EXISTS = 183


class WindowsRegistry:
    """HKCU 注册表操作封装，用于开机自启。"""

    RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
    APP_NAME = "LinRouter"

    @classmethod
    def _exe_command(cls) -> str:
        # 打包后 sys.executable 是 exe 本身；开发时 fallback 到当前脚本
        if getattr(sys, "frozen", False):
            exe = Path(sys.executable).resolve()
        else:
            exe = Path(__file__).resolve()
        return f'"{exe}" --tray'

    @classmethod
    def is_auto_start_enabled(cls) -> bool:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, cls.RUN_KEY, 0, winreg.KEY_READ) as key:
                winreg.QueryValueEx(key, cls.APP_NAME)
                return True
        except FileNotFoundError:
            return False
        except Exception:
            return False

    @classmethod
    def set_auto_start(cls, enabled: bool) -> bool:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, cls.RUN_KEY, 0, winreg.KEY_WRITE) as key:
                if enabled:
                    winreg.SetValueEx(key, cls.APP_NAME, 0, winreg.REG_SZ, cls._exe_command())
                else:
                    try:
                        winreg.DeleteValue(key, cls.APP_NAME)
                    except FileNotFoundError:
                        pass
            return True
        except Exception:
            return False


class SingleInstanceGuard:
    """Windows 命名互斥量，防止程序多开。"""

    MUTEX_NAME = "Local\\LinRouterSingleInstance"

    def __init__(self) -> None:
        self._handle: Optional[int] = None
        self._already_running = False

    def acquire(self) -> bool:
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        self._handle = kernel32.CreateMutexW(None, False, self.MUTEX_NAME)
        last_error = kernel32.GetLastError()
        if last_error == ERROR_ALREADY_EXISTS:
            self._already_running = True
            return False
        return True

    def release(self) -> None:
        if self._handle is not None:
            ctypes.windll.kernel32.CloseHandle(self._handle)
            self._handle = None

    def is_already_running(self) -> bool:
        return self._already_running


def create_tray_icon() -> Any:
    """用 Pillow 生成一个 64x64 的 LR 图标（绿色底白字）。"""
    from PIL import Image, ImageDraw, ImageFont
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # 绿色圆角背景
    draw.rounded_rectangle([0, 0, size - 1, size - 1], radius=12, fill=(34, 197, 94, 255))
    # 文字 LR
    try:
        font = ImageFont.truetype("segoeui.ttf", 28)
    except Exception:
        font = ImageFont.load_default()
    text = "LR"
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    x = (size - text_w) // 2 - bbox[0]
    y = (size - text_h) // 2 - bbox[1]
    draw.text((x, y), text, font=font, fill=(255, 255, 255, 255))
    return img


def copy_to_clipboard(text: str) -> bool:
    """把文本写入 Windows 剪贴板。"""
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        cf_unicode_text = 13
        gmem_moveable = 0x0002
        data = text.encode("utf-16-le") + b"\x00\x00"
        handle = kernel32.GlobalAlloc(gmem_moveable, len(data))
        if not handle:
            return False
        locked = kernel32.GlobalLock(handle)
        if not locked:
            return False
        ctypes.memmove(locked, data, len(data))
        kernel32.GlobalUnlock(handle)
        if user32.OpenClipboard(0):
            user32.EmptyClipboard()
            user32.SetClipboardData(cf_unicode_text, handle)
            user32.CloseClipboard()
            return True
    except Exception:
        pass
    return False


def focus_existing_instance(port: int) -> bool:
    """尝试打开已有实例的管理页面。"""
    try:
        url = f"http://{HOST}:{port}"
        urllib.request.urlopen(url, timeout=1.0)
        webbrowser.open(url)
        return True
    except Exception:
        return False


class LinRouterTray:
    def __init__(self, tray_mode: bool = False, config_path: Optional[Path] = None) -> None:
        self.tray_mode = tray_mode
        self.server: Optional[Any] = None
        self.server_thread: Optional[threading.Thread] = None
        self.port = DEFAULT_START_PORT
        self.ui_url = f"http://{HOST}:{self.port}"
        self.base_url = f"{self.ui_url}/v1"
        self.config_path = config_path or self.resolve_config_path()
        self.settings_store = SettingsStore(self.config_path)
        self.tray_icon: Optional[Any] = None
        self._stop_event = threading.Event()

    def resolve_config_path(self) -> Path:
        # 固定使用项目根目录的 lin-router-config.json。
        # 开发模式：desktop.py 所在目录即项目根目录。
        # 打包模式：exe 在 dist/ 子目录，父目录即项目根目录；若被单独复制走，仍回退到 exe 父目录。
        if getattr(sys, "frozen", False):
            exe_dir = Path(sys.executable).resolve().parent
            project_dir = exe_dir.parent
        else:
            project_dir = Path(__file__).resolve().parent
        return project_dir / DEFAULT_CONFIG_FILE

    def start_server(self) -> bool:
        try:
            from app import create_server
            self.server, self.port, self.config_path = create_server(HOST, DEFAULT_START_PORT, self.config_path)
        except Exception as exc:
            print(f"启动失败：{exc}")
            return False
        self.ui_url = f"http://{HOST}:{self.port}"
        self.base_url = f"{self.ui_url}/v1"
        self.server_thread = threading.Thread(target=self.server.serve_forever, name="LinRouterServer", daemon=True)
        self.server_thread.start()
        # 从配置中读取 settings，并与注册表同步
        self._sync_settings_with_registry()
        return True

    def stop_server(self) -> None:
        if self.server is not None:
            try:
                self.server.shutdown()
                self.server.server_close()
            except Exception:
                pass
            self.server = None
        self._stop_event.set()

    def _sync_settings_with_registry(self) -> None:
        # 以注册表中的开机自启状态为准，回写独立 settings 文件
        self.settings_store.update({"auto_start": WindowsRegistry.is_auto_start_enabled()})

    def open_ui(self) -> None:
        webbrowser.open(self.ui_url)

    def _build_menu(self, icon, menu_item):
        from pystray import Menu, MenuItem

        auto_start_enabled = WindowsRegistry.is_auto_start_enabled()
        # 启动最小化由配置文件里的 settings.start_minimized 决定
        start_minimized = self._load_start_minimized()

        def toggle_auto_start(item):
            new_state = not WindowsRegistry.is_auto_start_enabled()
            if WindowsRegistry.set_auto_start(new_state):
                # 回写配置文件
                self._update_config_setting("auto_start", new_state)
                self._refresh_menu()
            else:
                # 失败时弹一个极简提示：通过打开 UI 让用户看到
                copy_to_clipboard("开机自启设置失败，请以管理员身份运行 Lin Router")

        def toggle_start_minimized(item):
            new_state = not self._load_start_minimized()
            self._update_config_setting("start_minimized", new_state)
            self._refresh_menu()

        return Menu(
            MenuItem("打开管理面板", lambda icon, item: self.open_ui()),
            MenuItem("复制本地地址", lambda icon, item: copy_to_clipboard(self.ui_url)),
            MenuItem("复制 Base URL", lambda icon, item: copy_to_clipboard(self.base_url)),
            MenuItem(f"复制全局 Key（{DEFAULT_PUBLIC_API_KEY}）", lambda icon, item: copy_to_clipboard(DEFAULT_PUBLIC_API_KEY)),
            Menu.SEPARATOR,
            MenuItem("开机自启", toggle_auto_start, checked=lambda item: WindowsRegistry.is_auto_start_enabled()),
            MenuItem("启动后最小化到托盘", toggle_start_minimized, checked=lambda item: self._load_start_minimized()),
            Menu.SEPARATOR,
            MenuItem("退出", lambda icon, item: self._exit(icon)),
        )

    def _refresh_menu(self) -> None:
        if self.tray_icon:
            self.tray_icon.menu = self._build_menu(self.tray_icon, None)
            self.tray_icon.update_menu()

    def _exit(self, icon) -> None:
        self.stop_server()
        icon.stop()

    def _load_start_minimized(self) -> bool:
        return bool(self.settings_store.get("start_minimized", False))

    def _update_config_setting(self, key: str, value: Any) -> None:
        self.settings_store.update({key: value})

    def run(self) -> None:
        from pystray import Icon

        if not self.start_server():
            return

        # 非托盘模式启动时打开浏览器；托盘模式或 start_minimized 时不打开
        open_browser = not self.tray_mode and not self._load_start_minimized()

        icon_image = create_tray_icon()
        self.tray_icon = Icon(
            "LinRouter",
            icon_image,
            f"{APP_TITLE} ({HOST}:{self.port})",
            menu=self._build_menu(None, None),
        )

        # 左键点击打开面板
        self.tray_icon.on_clicked = lambda icon, button, time: self.open_ui()

        if open_browser:
            threading.Thread(target=self._open_ui_after_delay, daemon=True).start()

        self.tray_icon.run()

    def _open_ui_after_delay(self) -> None:
        time.sleep(0.5)
        self.open_ui()


def main() -> None:
    parser = argparse.ArgumentParser(description="Lin Router desktop tray")
    parser.add_argument("--tray", action="store_true", help="启动后最小化到系统托盘，不自动打开浏览器")
    # 默认不指定 --config，由 resolve_config_path 固定到项目根目录，避免跟随当前工作目录
    parser.add_argument("--config", default=None, help="配置文件路径（默认使用项目根目录 lin-router-config.json）")
    args = parser.parse_args()

    # 单实例保护
    guard = SingleInstanceGuard()
    if not guard.acquire():
        # 已有实例在运行，尝试打开其管理页面后退出
        focus_existing_instance(DEFAULT_START_PORT)
        sys.exit(0)

    try:
        config_path = Path(args.config) if args.config else None
        app = LinRouterTray(tray_mode=args.tray, config_path=config_path)
        app.run()
    finally:
        guard.release()


if __name__ == "__main__":
    main()
