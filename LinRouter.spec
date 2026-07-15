# -*- mode: python ; coding: utf-8 -*-

import subprocess
import sys
from pathlib import Path

# 平台相关配置
is_win32 = sys.platform.startswith('win32')
is_darwin = sys.platform.startswith('darwin')

# pystray 的官方 PyInstaller hook 会无条件收集所有平台后端。这里显式排除
# 当前目标平台不会执行的后端，避免 Windows 包携带 macOS/Linux 依赖，反之亦然。
if is_win32:
    pystray_submodules = [
        "pystray._base",
        "pystray._util",
        "pystray._util.win32",
        "pystray._win32",
    ]
    pystray_excludes = [
        "pystray._appindicator",
        "pystray._darwin",
        "pystray._gtk",
        "pystray._xorg",
        "pystray._util.gtk",
        "pystray._util.notify_dbus",
    ]
elif is_darwin:
    pystray_submodules = ["pystray._base", "pystray._darwin"]
    pystray_excludes = [
        "pystray._appindicator",
        "pystray._gtk",
        "pystray._win32",
        "pystray._xorg",
        "pystray._util.gtk",
        "pystray._util.notify_dbus",
        "pystray._util.win32",
    ]
else:
    pystray_submodules = []
    pystray_excludes = []


def _ensure_icon(target: str, ext: str) -> str | None:
    """如果平台图标不存在，尝试调用 scripts/generate_icon.py 生成。"""
    icon_path = Path(f"resources/{target}/LinRouter.{ext}")
    if not icon_path.exists():
        try:
            subprocess.run(
                [sys.executable, "scripts/generate_icon.py", target, str(icon_path)],
                check=True,
            )
        except Exception:
            # 生成失败（例如 macOS 下缺少 iconutil）时回退到无图标构建
            return None
    return str(icon_path)


datas = [('static', 'static')]
icon = None
info_plist = None
argv_emulation = False

if is_win32:
    datas.append(('resources/win32', 'resources/win32'))
    icon = _ensure_icon('win32', 'ico')
elif is_darwin:
    datas.append(('resources/darwin', 'resources/darwin'))
    icon = _ensure_icon('darwin', 'icns')
    info_plist = {'LSUIElement': True}
    argv_emulation = True

hiddenimports = [
    "settings_store",
    "upstream_client",
    "debug_capture",
    "certifi",
    "httpx",
    "h2",
    "PIL",
    "PIL.Image",
    "PIL.ImageDraw",
    "PIL.ImageFont",
] + pystray_submodules

a = Analysis(
    ['desktop.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # numpy 仅被 Pillow 的 TYPE_CHECKING 分支引用；AVIF 与托盘 ICO/PNG 无关。
    excludes=["numpy", "PIL.AvifImagePlugin", "PIL._avif"] + pystray_excludes,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe_args = [pyz, a.scripts, a.binaries, a.datas, []]
exe_kwargs = dict(
    name='LinRouter',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=argv_emulation,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
if icon:
    exe_kwargs['icon'] = icon

exe = EXE(*exe_args, **exe_kwargs)

if is_darwin:
    app = BUNDLE(
        exe,
        name='LinRouter.app',
        icon=icon,
        bundle_identifier='com.linrouter.launcher',
        info_plist=info_plist,
    )
