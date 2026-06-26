from __future__ import annotations

import ctypes
import sys
import threading
import time
import webbrowser
from ctypes import wintypes
from pathlib import Path
from typing import Optional

from app import DEFAULT_CONFIG_FILE, DEFAULT_START_PORT, create_server


HOST = "127.0.0.1"
APP_TITLE = "Lin Router"

WM_DESTROY = 0x0002
WM_COMMAND = 0x0111
WM_CLOSE = 0x0010
WS_OVERLAPPEDWINDOW = 0x00CF0000
WS_VISIBLE = 0x10000000
WS_CHILD = 0x40000000
SS_LEFT = 0x00000000
BS_PUSHBUTTON = 0x00000000
CW_USEDEFAULT = 0x80000000
SW_SHOWNORMAL = 1
COLOR_WINDOW = 5
IDC_ARROW = 32512

ID_OPEN_UI = 1001
ID_COPY_BASE = 1002
ID_COPY_UI = 1003
ID_EXIT = 1004

LRESULT = ctypes.c_ssize_t
WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_ssize_t

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class WNDCLASS(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HCURSOR),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", WPARAM),
        ("lParam", LPARAM),
        ("time", wintypes.DWORD),
        ("pt", wintypes.POINT),
    ]


WndProcType = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, WPARAM, LPARAM)

user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASS)]
user32.RegisterClassW.restype = wintypes.ATOM
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.DWORD,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HWND,
    wintypes.HMENU,
    wintypes.HINSTANCE,
    wintypes.LPVOID,
]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.DestroyWindow.restype = wintypes.BOOL
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.PostQuitMessage.restype = None
user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = wintypes.BOOL
user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
user32.TranslateMessage.restype = wintypes.BOOL
user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
user32.DispatchMessageW.restype = LRESULT
user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
user32.LoadCursorW.restype = wintypes.HCURSOR
user32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
user32.MessageBoxW.restype = ctypes.c_int
user32.SetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
user32.SetWindowTextW.restype = wintypes.BOOL

kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE


class LinRouterDesktop:
    def __init__(self) -> None:
        self.hinstance = kernel32.GetModuleHandleW(None)
        self.hwnd: Optional[int] = None
        self.server = None
        self.server_thread: Optional[threading.Thread] = None
        self.port = DEFAULT_START_PORT
        self.ui_url = f"http://{HOST}:{self.port}"
        self.base_url = f"{self.ui_url}/v1"
        self.config_path = self.resolve_config_path()
        self.status_handle: Optional[int] = None
        self.ui_handle: Optional[int] = None
        self.base_handle: Optional[int] = None
        self.config_handle: Optional[int] = None
        self._wndproc = WndProcType(self.window_proc)

    def resolve_config_path(self) -> Path:
        exe_dir = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
        local_config = exe_dir / DEFAULT_CONFIG_FILE
        if local_config.exists():
            return local_config

        project_config = exe_dir.parent / DEFAULT_CONFIG_FILE
        if exe_dir.name.lower() == "dist" and project_config.exists():
            return project_config

        return local_config

    def run(self) -> None:
        class_name = "LinRouterDesktopWindow"
        wc = WNDCLASS()
        wc.lpfnWndProc = ctypes.cast(self._wndproc, ctypes.c_void_p).value
        wc.hInstance = self.hinstance
        wc.hCursor = user32.LoadCursorW(None, ctypes.cast(IDC_ARROW, wintypes.LPCWSTR))
        wc.hbrBackground = COLOR_WINDOW + 1
        wc.lpszClassName = class_name

        if not user32.RegisterClassW(ctypes.byref(wc)):
            raise ctypes.WinError(ctypes.get_last_error())

        self.hwnd = user32.CreateWindowExW(
            0,
            class_name,
            APP_TITLE,
            WS_OVERLAPPEDWINDOW | WS_VISIBLE,
            CW_USEDEFAULT,
            CW_USEDEFAULT,
            610,
            350,
            None,
            None,
            self.hinstance,
            None,
        )
        if not self.hwnd:
            raise ctypes.WinError(ctypes.get_last_error())

        self.start_server()
        msg = MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def window_proc(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        if msg == WM_COMMAND:
            command_id = wparam & 0xFFFF
            if command_id == ID_OPEN_UI:
                self.open_ui()
                return 0
            if command_id == ID_COPY_BASE:
                self.copy_to_clipboard(self.base_url)
                self.set_status("Copied Base URL")
                return 0
            if command_id == ID_COPY_UI:
                self.copy_to_clipboard(self.ui_url)
                self.set_status("Copied UI URL")
                return 0
            if command_id == ID_EXIT:
                user32.DestroyWindow(hwnd)
                return 0
        if msg == WM_CLOSE:
            user32.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            self.stop_server()
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def create_label(self, text: str, x: int, y: int, width: int, height: int = 22) -> int:
        return user32.CreateWindowExW(
            0,
            "STATIC",
            text,
            WS_CHILD | WS_VISIBLE | SS_LEFT,
            x,
            y,
            width,
            height,
            self.hwnd,
            None,
            self.hinstance,
            None,
        )

    def create_button(self, text: str, command_id: int, x: int, y: int, width: int, height: int = 32) -> int:
        return user32.CreateWindowExW(
            0,
            "BUTTON",
            text,
            WS_CHILD | WS_VISIBLE | BS_PUSHBUTTON,
            x,
            y,
            width,
            height,
            self.hwnd,
            command_id,
            self.hinstance,
            None,
        )

    def build_controls(self) -> None:
        self.create_label(APP_TITLE, 24, 22, 520, 28)
        self.create_label("本地 Hermes/OpenAI 兼容模型调度器", 24, 52, 520, 24)

        self.create_label("状态", 28, 96, 120)
        self.status_handle = self.create_label("Starting...", 150, 96, 400)

        self.create_label("管理页面", 28, 128, 120)
        self.ui_handle = self.create_label(self.ui_url, 150, 128, 400)

        self.create_label("Hermes Base URL", 28, 160, 120)
        self.base_handle = self.create_label(self.base_url, 150, 160, 400)

        self.create_label("配置文件", 28, 192, 120)
        self.config_handle = self.create_label(str(self.config_path), 150, 192, 400, 42)

        self.create_label("Hermes API Key 请使用页面里对应连接组生成的 lr-... key。", 28, 236, 540)

        self.create_button("打开管理页面", ID_OPEN_UI, 28, 276, 120)
        self.create_button("复制 Base URL", ID_COPY_BASE, 160, 276, 120)
        self.create_button("复制页面地址", ID_COPY_UI, 292, 276, 120)
        self.create_button("退出", ID_EXIT, 464, 276, 80)

    def start_server(self) -> None:
        self.build_controls()
        try:
            self.server, self.port, self.config_path = create_server(HOST, DEFAULT_START_PORT, self.config_path)
        except Exception as exc:
            self.set_status("Start failed")
            user32.MessageBoxW(
                self.hwnd,
                f"启动失败：{exc}\n\n请确认 {DEFAULT_START_PORT} 端口没有被其他程序占用。",
                APP_TITLE,
                0x10,
            )
            return

        self.ui_url = f"http://{HOST}:{self.port}"
        self.base_url = f"{self.ui_url}/v1"
        self.set_label(self.ui_handle, self.ui_url)
        self.set_label(self.base_handle, self.base_url)
        self.set_label(self.config_handle, str(self.config_path))
        self.set_status(f"Running on {HOST}:{self.port}")

        self.server_thread = threading.Thread(target=self.server.serve_forever, name="LinRouterServer", daemon=True)
        self.server_thread.start()
        threading.Thread(target=self.open_ui_when_ready, name="LinRouterBrowser", daemon=True).start()

    def stop_server(self) -> None:
        if self.server is not None:
            try:
                self.server.shutdown()
                self.server.server_close()
            except Exception:
                pass
            self.server = None

    def open_ui_when_ready(self) -> None:
        time.sleep(0.35)
        self.open_ui()

    def open_ui(self) -> None:
        webbrowser.open(self.ui_url)

    def set_status(self, value: str) -> None:
        self.set_label(self.status_handle, value)

    def set_label(self, handle: Optional[int], value: str) -> None:
        if handle:
            user32.SetWindowTextW(handle, value)

    def copy_to_clipboard(self, text: str) -> None:
        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.OpenClipboard.restype = wintypes.BOOL
        user32.EmptyClipboard.argtypes = []
        user32.EmptyClipboard.restype = wintypes.BOOL
        user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        user32.SetClipboardData.restype = wintypes.HANDLE
        user32.CloseClipboard.argtypes = []
        user32.CloseClipboard.restype = wintypes.BOOL

        kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalLock.restype = wintypes.LPVOID
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalUnlock.restype = wintypes.BOOL

        cf_unicode_text = 13
        gmem_moveable = 0x0002
        data = text.encode("utf-16-le") + b"\x00\x00"
        handle = kernel32.GlobalAlloc(gmem_moveable, len(data))
        if not handle:
            return
        locked = kernel32.GlobalLock(handle)
        if not locked:
            return
        ctypes.memmove(locked, data, len(data))
        kernel32.GlobalUnlock(handle)

        if user32.OpenClipboard(self.hwnd):
            user32.EmptyClipboard()
            user32.SetClipboardData(cf_unicode_text, handle)
            user32.CloseClipboard()


def main() -> None:
    app = LinRouterDesktop()
    app.run()


if __name__ == "__main__":
    main()
