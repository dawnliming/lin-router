from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
HELPER_PATH = ROOT / "scripts" / "sign_windows_artifact.py"


spec = importlib.util.spec_from_file_location("sign_windows_artifact", HELPER_PATH)
assert spec and spec.loader
signing = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = signing
spec.loader.exec_module(signing)


def signing_env(tmp_path: Path) -> dict[str, str]:
    certificate = tmp_path / "release-cert.pfx"
    certificate.write_bytes(b"test placeholder; not a real certificate")
    signtool = tmp_path / "signtool.exe"
    signtool.write_bytes(b"test placeholder; not a real executable")
    password = "x" * 32
    return {
        signing.SIGNTOOL_ENV: str(signtool),
        signing.CERTIFICATE_ENV: str(certificate),
        signing.TIMESTAMP_ENV: "https://timestamp.example.test",
        signing.PASSWORD_ENV: password,
    }


@pytest.mark.parametrize(
    "missing,expected",
    [
        (signing.SIGNTOOL_ENV, "signtool"),
        (signing.CERTIFICATE_ENV, "证书路径"),
        (signing.TIMESTAMP_ENV, "时间戳 URL"),
        (signing.PASSWORD_ENV, "PFX 密码"),
    ],
)
def test_explicit_signing_requires_every_input(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    missing: str,
    expected: str,
) -> None:
    env = signing_env(tmp_path)
    env.pop(missing)

    # Hosted Linux runner may happen to expose an unrelated signtool command.
    # This case verifies the configured-input error path, not host autodiscovery.
    if missing == signing.SIGNTOOL_ENV:
        monkeypatch.setattr(signing.shutil, "which", lambda _name: None)
        monkeypatch.setattr(signing, "_find_windows_sdk_signtool", lambda: None)

    with pytest.raises(signing.SigningConfigError, match=expected):
        signing.validate_signing_config(env=env)


def test_signing_command_uses_sha256_and_password_is_redacted(tmp_path: Path) -> None:
    env = signing_env(tmp_path)
    config = signing.validate_signing_config(env=env)
    artifact = tmp_path / "LinRouter.exe"
    artifact.write_bytes(b"test payload")

    command = signing.build_signtool_command(config, artifact)
    rendered = signing.redact_command(command, config.password)

    assert command[1:7] == ["sign", "/fd", "sha256", "/tr", env[signing.TIMESTAMP_ENV], "/td"]
    assert command[7] == "sha256"
    assert "/f" in command
    assert "/p" in command
    assert config.password in command
    assert config.password not in rendered
    assert "<redacted>" in rendered


def test_sign_artifact_redacts_signtool_output(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    env = signing_env(tmp_path)
    config = signing.validate_signing_config(env=env)
    artifact = tmp_path / "LinRouter.exe"
    artifact.write_bytes(b"test payload")

    def fake_run(*args: object, **kwargs: object) -> SimpleNamespace:
        command = args[0]
        assert config.password in command
        return SimpleNamespace(
            returncode=0,
            stdout=f"signed with {config.password}",
            stderr=f"debug {config.password}",
        )

    monkeypatch.setattr(signing.subprocess, "run", fake_run)
    signing.sign_artifact(artifact, config)
    output = capsys.readouterr()

    assert config.password not in output.out
    assert config.password not in output.err
    assert "<redacted>" in output.out
    assert "<redacted>" in output.err


def test_build_script_signs_payload_before_packaging_and_installer_afterwards() -> None:
    script = (ROOT / "scripts" / "build.sh").read_text(encoding="utf-8")
    payload_anchor = 'sign_windows_artifact "$DIST_DIR/LinRouter_windows.exe"'
    zip_anchor = "build_windows_zip"
    installer_anchor = "build_windows_installer"
    installer_sign_anchor = 'sign_windows_artifact "$output_file"'

    payload_position = script.index(payload_anchor, script.index('win32)'))
    zip_position = script.index(zip_anchor, payload_position)
    installer_call_position = script.index("      build_windows_installer", zip_position)
    installer_function_position = script.index("build_windows_installer()")
    installer_function_end = script.index("case \"$TARGET\"", installer_function_position)
    installer_sign_position = script.index(installer_sign_anchor, installer_function_position, installer_function_end)

    assert payload_position < zip_position < installer_call_position
    assert installer_function_position < installer_sign_position < installer_function_end
    assert script.index('echo "Windows 安装包构建完成：$output_file"', installer_function_position) < installer_sign_position
    assert "--sign" in script
    assert 'if [[ "$SIGN_WINDOWS" != "1" ]]; then' in script


def test_build_script_can_force_self_installer_for_hosted_ci() -> None:
    script = (ROOT / "scripts" / "build.sh").read_text(encoding="utf-8")

    assert '"${LINROUTER_FORCE_SELF_INSTALLER:-}" == "1"' in script
    force_position = script.index('"${LINROUTER_FORCE_SELF_INSTALLER:-}" == "1"')
    compiler_lookup_position = script.index('command -v iscc', force_position)
    assert force_position < compiler_lookup_position
