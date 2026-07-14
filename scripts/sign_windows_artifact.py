"""Sign a Windows release artifact with signtool without exposing the PFX password.

The build wrapper supplies the artifact path. Signing configuration is intentionally
read from environment variables so the password is not part of source, shell
history, or the logged command line.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

SIGNTOOL_ENV = "LINROUTER_SIGNTOOL"
CERTIFICATE_ENV = "LINROUTER_SIGN_CERT_PATH"
TIMESTAMP_ENV = "LINROUTER_SIGN_TIMESTAMP_URL"
PASSWORD_ENV = "LINROUTER_SIGN_CERT_PASSWORD"


class SigningConfigError(ValueError):
    """Raised when explicit signing mode lacks a required input."""


class SigningError(RuntimeError):
    """Raised when signtool cannot sign the requested artifact."""


@dataclass(frozen=True)
class SigningConfig:
    signtool: Path
    certificate: Path
    timestamp_url: str
    password: str


def _find_windows_sdk_signtool() -> Path | None:
    roots = (
        Path(r"C:\Program Files (x86)\Windows Kits\10\bin"),
        Path(r"C:\Program Files\Windows Kits\10\bin"),
    )
    candidates: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        candidates.extend(root.glob(r"*\x64\signtool.exe"))
        candidates.extend(root.glob(r"*\x86\signtool.exe"))
    if not candidates:
        return None
    return sorted(candidates, key=lambda path: str(path), reverse=True)[0]


def resolve_signtool(explicit: str | None = None, env: dict[str, str] | None = None) -> Path:
    values = os.environ if env is None else env
    configured = explicit or values.get(SIGNTOOL_ENV)
    if configured:
        configured_path = Path(configured).expanduser()
        if configured_path.is_file():
            return configured_path
        from_path = shutil.which(configured)
        if from_path:
            return Path(from_path)
        raise SigningConfigError(
            f"signtool 不存在或不可执行：{configured}；请设置 {SIGNTOOL_ENV} 为 signtool.exe 路径"
        )

    from_path = shutil.which("signtool.exe") or shutil.which("signtool")
    if from_path:
        return Path(from_path)
    sdk_path = _find_windows_sdk_signtool()
    if sdk_path:
        return sdk_path
    raise SigningConfigError(
        "未找到 signtool.exe；请安装 Windows SDK，或设置 "
        f"{SIGNTOOL_ENV} 为 signtool.exe 的完整路径"
    )


def resolve_signing_config(
    env: dict[str, str] | None = None,
    signtool: str | None = None,
) -> SigningConfig:
    values = os.environ if env is None else env

    certificate_raw = values.get(CERTIFICATE_ENV, "").strip()
    if not certificate_raw:
        raise SigningConfigError(
            f"已启用 Windows 签名，但缺少证书路径；请设置 {CERTIFICATE_ENV}（PFX/P12，含私钥）"
        )
    certificate = Path(certificate_raw).expanduser()
    if not certificate.is_file():
        raise SigningConfigError(f"代码签名证书不存在：{certificate}")

    timestamp_url = values.get(TIMESTAMP_ENV, "").strip()
    parsed = urlparse(timestamp_url)
    if not timestamp_url or parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SigningConfigError(
            f"已启用 Windows 签名，但缺少有效时间戳 URL；请设置 {TIMESTAMP_ENV}（http/https）"
        )

    password = values.get(PASSWORD_ENV, "")
    if not password:
        raise SigningConfigError(
            f"已启用 Windows 签名，但缺少 PFX 密码；请通过进程环境变量设置 {PASSWORD_ENV}，"
            "不要把密码写进命令行、源码或日志"
        )

    return SigningConfig(
        signtool=resolve_signtool(explicit=signtool, env=values),
        certificate=certificate,
        timestamp_url=timestamp_url,
        password=password,
    )


def build_signtool_command(config: SigningConfig, artifact: Path) -> list[str]:
    """Build the real command; callers must only print redact_command()."""
    return [
        str(config.signtool),
        "sign",
        "/fd",
        "sha256",
        "/tr",
        config.timestamp_url,
        "/td",
        "sha256",
        "/f",
        str(config.certificate),
        "/p",
        config.password,
        str(artifact),
    ]


def redact_command(command: list[str], password: str) -> str:
    """Return a log-safe representation of a signtool command."""
    safe = ["<redacted>" if password and item == password else item for item in command]
    return " ".join(f'"{item}"' if " " in item else item for item in safe)


def redact_output(output: str, password: str) -> str:
    if password:
        return output.replace(password, "<redacted>")
    return output


def validate_signing_config(
    env: dict[str, str] | None = None,
    signtool: str | None = None,
) -> SigningConfig:
    """Validate all required inputs before any build artifact is produced."""
    return resolve_signing_config(env=env, signtool=signtool)


def sign_artifact(artifact: Path, config: SigningConfig) -> None:
    if not artifact.is_file():
        raise SigningError(f"待签名产物不存在：{artifact}")

    command = build_signtool_command(config, artifact)
    print(f"签名产物：{artifact}")
    print(f"签名命令（已脱敏）：{redact_command(command, config.password)}")
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    stdout = redact_output(completed.stdout or "", config.password).strip()
    stderr = redact_output(completed.stderr or "", config.password).strip()
    if stdout:
        print(stdout)
    if stderr:
        print(stderr, file=sys.stderr)
    if completed.returncode != 0:
        detail = stderr or stdout or "signtool 未返回错误详情"
        raise SigningError(f"signtool 签名失败（退出码 {completed.returncode}）：{detail}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sign a Lin Router Windows release artifact")
    parser.add_argument("artifact", nargs="?", help="待签名的 exe 路径")
    parser.add_argument("--validate-only", action="store_true", help="仅校验签名配置，不执行签名")
    parser.add_argument("--signtool", help="可选：signtool.exe 完整路径；也可使用环境变量")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.validate_only and not args.artifact:
        print("未指定待签名的 exe；或使用 --validate-only 仅校验配置", file=sys.stderr)
        return 2
    try:
        config = validate_signing_config(signtool=args.signtool)
        if args.validate_only:
            print("Windows 签名配置校验通过（未执行真实签名）")
            return 0
        sign_artifact(Path(args.artifact).expanduser().resolve(), config)
    except (SigningConfigError, SigningError) as exc:
        print(f"Windows 签名失败：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
