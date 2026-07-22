"""v0.6.4 发布元数据一致性回归。"""

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_v064_release_metadata_is_consistent() -> None:
    build_script = (ROOT / "scripts" / "build.sh").read_text(encoding="utf-8")
    settings_js = (ROOT / "static" / "js" / "settings-panel.js").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert 'APP_VERSION="0.6.4"' in build_script
    assert "<span>v0.6.4</span>" in settings_js
    assert "LinRouter-v0.6.4-win-x64.zip" in readme
    assert "LinRouter-Setup-v0.6.4-win-x64.exe" in readme
