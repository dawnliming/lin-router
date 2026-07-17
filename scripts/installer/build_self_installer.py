from __future__ import annotations

import argparse
import ctypes
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

APP_NAME = "Lin Router"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "LinRouter"

INSTALLER_SOURCE = r"""
from __future__ import annotations

import argparse
import ctypes
import os
import shutil
import subprocess
import sys
from pathlib import Path

APP_NAME = "Lin Router"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "LinRouter"
DEFAULT_INSTALL_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Programs" / "LinRouter"
PAYLOAD_NAME = "LinRouter.exe"


def is_windows() -> bool:
    return sys.platform.startswith("win32")


def message_box(title: str, message: str, flags: int = 0x40) -> None:
    if is_windows():
        ctypes.windll.user32.MessageBoxW(None, message, title, flags)
    else:
        print(f"{title}: {message}")


def set_console_title(title: str) -> None:
    if is_windows():
        ctypes.windll.kernel32.SetConsoleTitleW(title)


def embedded_exe_path() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / PAYLOAD_NAME


def target_exe_path(install_dir: Path) -> Path:
    return install_dir / "dist" / PAYLOAD_NAME


def create_shortcut(shortcut_path: Path, target_path: Path, working_dir: Path) -> bool:
    if not is_windows():
        return False
    shortcut_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        win32com_client = __import__("win32com.client", fromlist=("client",))
        shell = win32com_client.Dispatch("WScript.Shell")
        shortcut = shell.CreateShortcut(str(shortcut_path))
        shortcut.TargetPath = str(target_path)
        shortcut.WorkingDirectory = str(working_dir)
        shortcut.IconLocation = str(target_path)
        shortcut.Save()
        return True
    except Exception:
        ps_command = (
            "$shell = New-Object -ComObject WScript.Shell; "
            f"$shortcut = $shell.CreateShortcut('{shortcut_path}'); "
            f"$shortcut.TargetPath = '{target_path}'; "
            f"$shortcut.WorkingDirectory = '{working_dir}'; "
            f"$shortcut.IconLocation = '{target_path}'; "
            "$shortcut.Save()"
        )
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_command],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except Exception:
            return False


def desktop_dir() -> Path:
    if is_windows():
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders") as key:
                value, _ = winreg.QueryValueEx(key, "Desktop")
                return Path(value)
        except Exception:
            pass
    return Path.home() / "Desktop"


def start_menu_dir() -> Path:
    return Path(os.environ.get("APPDATA", str(Path.home()))) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "LinRouter"


def set_autostart(exe_path: Path, enabled: bool) -> None:
    if not is_windows():
        return
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, f'"{exe_path}" --tray')
        else:
            try:
                winreg.DeleteValue(key, RUN_VALUE)
            except FileNotFoundError:
                pass


def write_uninstaller(install_dir: Path) -> None:
    uninstall_script = install_dir / "uninstall.cmd"
    uninstall_script.write_text(
        "@echo off\r\n"
        "setlocal\r\n"
        "reg delete HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run /v LinRouter /f >nul 2>nul\r\n"
        "del \"%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\LinRouter\\Lin Router.lnk\" >nul 2>nul\r\n"
        "rmdir \"%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\LinRouter\" >nul 2>nul\r\n"
        "del \"%USERPROFILE%\\Desktop\\Lin Router.lnk\" >nul 2>nul\r\n"
        "cd /d %TEMP%\r\n"
        f"rmdir /s /q \"{install_dir}\"\r\n",
        encoding="utf-8",
    )


def install(args: argparse.Namespace) -> int:
    set_console_title(f"{APP_NAME} 安装器")
    source_exe = embedded_exe_path()
    if not source_exe.exists():
        message_box("安装失败", f"安装包缺少 {PAYLOAD_NAME}", 0x10)
        return 1

    install_dir = Path(args.dir).expanduser().resolve() if args.dir else DEFAULT_INSTALL_DIR
    exe_path = target_exe_path(install_dir)
    exe_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        shutil.copy2(source_exe, exe_path)
        write_uninstaller(install_dir)
        create_shortcut(start_menu_dir() / "Lin Router.lnk", exe_path, exe_path.parent)
        if args.desktop:
            create_shortcut(desktop_dir() / "Lin Router.lnk", exe_path, exe_path.parent)
        set_autostart(exe_path, bool(args.autostart))
    except Exception as exc:
        message_box("安装失败", str(exc), 0x10)
        return 1

    if not args.silent:
        message_box("安装完成", f"{APP_NAME} 已安装到：\n{install_dir}")
    if args.run:
        subprocess.Popen([str(exe_path)], cwd=str(exe_path.parent), close_fds=True)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=f"{APP_NAME} installer")
    parser.add_argument("--dir", help="安装目录，默认安装到当前用户 LocalAppData")
    parser.add_argument("--desktop", dest="desktop", action="store_true", default=True, help="创建桌面快捷方式")
    parser.add_argument("--no-desktop", dest="desktop", action="store_false", help="不创建桌面快捷方式")
    parser.add_argument("--autostart", action="store_true", help="写入开机自启")
    parser.add_argument("--run", action="store_true", default=True, help="安装后启动")
    parser.add_argument("--no-run", dest="run", action="store_false", help="安装后不启动")
    parser.add_argument("--silent", action="store_true", help="静默安装")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(install(parse_args()))
"""


def ensure_pyinstaller() -> None:
    try:
        import PyInstaller.__main__  # type: ignore
        return
    except Exception as exc:
        raise SystemExit("PyInstaller 未安装，请先运行 pip install pyinstaller") from exc


def build_installer(source_exe: Path, output_exe: Path, work_dir: Path) -> None:
    ensure_pyinstaller()
    import PyInstaller.__main__  # type: ignore

    if not source_exe.exists():
        raise SystemExit(f"源程序不存在：{source_exe}")

    with tempfile.TemporaryDirectory(prefix="linrouter-installer-") as temp_name:
        temp_dir = Path(temp_name)
        installer_py = temp_dir / "linrouter_installer.py"
        payload_exe = temp_dir / "LinRouter.exe"
        installer_py.write_text(INSTALLER_SOURCE, encoding="utf-8")
        shutil.copy2(source_exe, payload_exe)

        dist_dir = temp_dir / "dist"
        build_dir = temp_dir / "build"
        args = [
            str(installer_py),
            "--onefile",
            "--windowed",
            "--noconfirm",
            "--clean",
            "--name",
            output_exe.stem,
            "--add-binary",
            f"{payload_exe}{';' if sys.platform.startswith('win32') else ':'}.",
            "--distpath",
            str(dist_dir),
            "--workpath",
            str(build_dir),
            "--specpath",
            str(temp_dir),
        ]
        icon_path = work_dir / "resources" / "win32" / "LinRouter.ico"
        if icon_path.exists():
            args.extend(["--icon", str(icon_path)])

        PyInstaller.__main__.run(args)
        built_exe = dist_dir / f"{output_exe.stem}.exe"
        output_exe.parent.mkdir(parents=True, exist_ok=True)
        if output_exe.exists():
            output_exe.unlink()
        shutil.copy2(built_exe, output_exe)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Lin Router self-contained Windows installer")
    parser.add_argument("--source-exe", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--project-root", default=str(Path(__file__).resolve().parents[2]))
    args = parser.parse_args()
    if sys.platform.startswith("win32"):
        for stream in (sys.stdout, sys.stderr):
          try:
              stream.reconfigure(encoding="utf-8")
          except Exception:
              pass
    build_installer(Path(args.source_exe).resolve(), Path(args.output).resolve(), Path(args.project_root).resolve())
    print(f"Windows installer build complete: {Path(args.output).resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
