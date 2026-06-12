"""
Bilibili authentication via QR code terminal login.
On first run, displays a QR code in terminal for user to scan
with the B站 app, then saves credentials to .env automatically.
"""

import os
import sys
from pathlib import Path
import config


def get_credential():
    """Returns a bilibili_api Credential, triggering QR login if needed."""
    from bilibili_api import Credential
    from bilibili_api.login_v2 import QrCodeLogin, QrCodeLoginEvents, QrCodeLoginChannel

    # Check if credentials are already configured
    issues = config.validate()
    missing_bili = any("BILI_SESSDATA" in i or "BILI_BILI_JCT" in i for i in issues)

    if not missing_bili:
        # Use existing credentials from .env
        return Credential(
            sessdata=config.BILI_SESSDATA,
            bili_jct=config.BILI_BILI_JCT,
            buvid3=config.BILI_BUVID3 or None,
        )

    # ── First-time setup: QR code login ───────────────────────
    print()
    print("=" * 60)
    print("  首次使用 — B站 扫码登录")
    print("=" * 60)
    print()
    print("  请使用 Bilibili 手机客户端扫描下方二维码：")
    print()

    # Run async QR login
    import asyncio
    credential = asyncio.run(_qr_login_flow())

    # Save to .env
    _save_credential_to_env(credential)

    print()
    print("✅ 登录成功！凭据已自动保存到 .env 文件。")
    print("   下次运行将跳过扫码，直接使用已保存的凭据。")
    print()

    return credential


async def _qr_login_flow():
    """Run the QR code login flow, return Credential."""
    from bilibili_api.login_v2 import QrCodeLogin, QrCodeLoginEvents, QrCodeLoginChannel
    import asyncio
    import subprocess
    import tempfile

    qr_login = QrCodeLogin(platform=QrCodeLoginChannel.WEB)

    # Generate QR code
    await qr_login.generate_qrcode()

    # Save QR code as image and open it
    qr_pic = qr_login.get_qrcode_picture()
    qr_path = Path(tempfile.gettempdir()) / "bilibili_qrcode.png"
    qr_pic.to_file(str(qr_path))
    print(f"  二维码图片已保存: {qr_path}")
    print(f"  正在打开图片...")
    _open_image(qr_path)

    # Also try terminal QR as fallback
    try:
        qr_login.get_qrcode_terminal()
    except Exception:
        pass
    print()
    print("  ⏳ 等待扫码...", end="", flush=True)

    # Poll for scan/confirm (track state to avoid repeated messages)
    prev_state = None
    while True:
        await asyncio.sleep(2)
        try:
            state = await qr_login.check_state()
        except Exception as e:
            print(f"\n  ❌ 登录异常: {e}")
            raise

        if state == QrCodeLoginEvents.SCAN:
            print(".", end="", flush=True)  # heartbeat
            prev_state = state
            continue

        if state == prev_state:
            continue  # don't repeat messages

        if state == QrCodeLoginEvents.CONF:
            print("\n  📱 已扫描，请在手机上点击「确认登录」...", end="", flush=True)
        elif state == QrCodeLoginEvents.DONE:
            print("\n  ✅ 登录成功!")
            break
        elif state == QrCodeLoginEvents.TIMEOUT:
            print("\n  ⚠️ 二维码已过期，正在刷新...", end="", flush=True)
            await qr_login.generate_qrcode()
            qr_pic = qr_login.get_qrcode_picture()
            qr_pic.to_file(str(qr_path))
            _open_image(qr_path)
            print("  新二维码已打开，请重新扫码。")
            print("  ⏳ 等待扫码...", end="", flush=True)
        # SCAN state = still waiting, no user action yet — silent

        prev_state = state

    return qr_login.get_credential()


def _open_image(path: Path) -> None:
    """Open an image file with the system default viewer."""
    try:
        import subprocess
        import platform
        if platform.system() == "Windows":
            os.startfile(str(path))
        elif platform.system() == "Darwin":
            subprocess.run(["open", str(path)])
        else:
            subprocess.run(["xdg-open", str(path)])
    except Exception:
        pass  # silently ignore if we can't open it


def _save_credential_to_env(credential) -> None:
    """Save Bilibili credential to .env file, preserving other settings."""
    env_path = config.PROJECT_ROOT / ".env"

    # Read existing .env (or .env.example if .env doesn't exist)
    existing_lines = []
    if env_path.exists():
        existing_lines = env_path.read_text(encoding="utf-8").split("\n")
    else:
        example_path = config.PROJECT_ROOT / ".env.example"
        if example_path.exists():
            existing_lines = example_path.read_text(encoding="utf-8").split("\n")

    # Replace BILI_* lines, keep everything else
    new_lines = []
    seen_keys = set()
    bili_keys = {"BILI_SESSDATA", "BILI_BILI_JCT", "BILI_BUVID3"}

    for line in existing_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            new_lines.append(line)
            continue

        # Check if this line sets a BILI_ key
        key = stripped.split("=")[0].strip()
        if key in bili_keys:
            seen_keys.add(key)
            if key == "BILI_SESSDATA":
                new_lines.append(f"BILI_SESSDATA={credential.sessdata}")
            elif key == "BILI_BILI_JCT":
                new_lines.append(f"BILI_BILI_JCT={credential.bili_jct}")
            elif key == "BILI_BUVID3":
                new_lines.append(f"BILI_BUVID3={credential.buvid3 or ''}")
        else:
            new_lines.append(line)

    # Add any missing BILI_ keys
    if "BILI_SESSDATA" not in seen_keys:
        new_lines.append(f"BILI_SESSDATA={credential.sessdata}")
    if "BILI_BILI_JCT" not in seen_keys:
        new_lines.append(f"BILI_BILI_JCT={credential.bili_jct}")
    if "BILI_BUVID3" not in seen_keys and credential.buvid3:
        new_lines.append(f"BILI_BUVID3={credential.buvid3}")

    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

    # Reload config to pick up new values
    from dotenv import load_dotenv
    load_dotenv(env_path, override=True)
    # Also update the module attributes
    import os
    config.BILI_SESSDATA = os.getenv("BILI_SESSDATA", "")
    config.BILI_BILI_JCT = os.getenv("BILI_BILI_JCT", "")
    config.BILI_BUVID3 = os.getenv("BILI_BUVID3", "")


def login_interactive() -> bool:
    """
    Explicit login command — force re-login even if credentials exist.
    Returns True if login was successful.
    """
    print()
    print("=" * 60)
    print("  B站 重新登录")
    print("=" * 60)

    try:
        credential = get_credential()
        return credential is not None
    except KeyboardInterrupt:
        print("\n\n  ⚠️ 用户取消登录")
        return False
    except Exception as e:
        print(f"\n  ❌ 登录失败: {e}")
        return False
