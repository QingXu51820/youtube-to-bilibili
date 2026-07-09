"""
Credential status checker — inspect expiry/validity of Bilibili, YouTube OAuth,
and YouTube cookie credentials without running the full pipeline.

Usage:
    python main.py --check-auth          # standalone check
    from yt2bili.auth_checker import run_auth_check
    run_auth_check()                     # programmatic
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from yt2bili import config

# ── Helpers ────────────────────────────────────────────────────────────────

def _format_remaining(remaining: timedelta) -> str:
    """Format a timedelta as a human-readable duration string."""
    total_seconds = int(remaining.total_seconds())
    if total_seconds <= 0:
        return "已过期"

    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60

    parts = []
    if days > 0:
        parts.append(f"{days} 天")
    if hours > 0:
        parts.append(f"{hours} 小时")
    if minutes > 0 and days == 0:
        parts.append(f"{minutes} 分钟")
    if not parts and total_seconds < 60:
        parts.append(f"{total_seconds} 秒")
    return " ".join(parts)


def _parse_netscape_cookies(file_path: Path) -> list[dict[str, Any]]:
    """Parse a Netscape-format cookie file, return list of cookie dicts.

    Each dict has keys: domain, path, secure, expires (datetime or None),
    name, value.
    """
    cookies: list[dict[str, Any]] = []
    if not file_path.exists():
        return cookies

    for line in file_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        try:
            domain, flag, path, secure_str, expires_str, name, value = parts[0:7]
            expires = None
            try:
                exp_ts = int(expires_str)
                if exp_ts > 0:
                    expires = datetime.fromtimestamp(exp_ts, tz=timezone.utc)
            except (ValueError, OSError):
                pass
            cookies.append({
                "domain": domain,
                "path": path,
                "secure": secure_str.upper() == "TRUE",
                "expires": expires,
                "name": name,
                "value": value,
            })
        except ValueError:
            continue
    return cookies


def _is_youtube_cookie(domain: str) -> bool:
    """Check if a cookie domain belongs to YouTube or Google."""
    normalized = domain.lstrip(".").lower()
    return (
        normalized == "youtube.com"
        or normalized.endswith(".youtube.com")
        or normalized == "youtube-nocookie.com"
        or normalized.endswith(".youtube-nocookie.com")
        or normalized == "google.com"
        or normalized.endswith(".google.com")
    )


def _status_icon(status: str) -> str:
    """Return a status emoji for the given status string."""
    icons = {
        "valid": "✅",       # ✅
        "ok": "✅",
        "expired": "❌",     # ❌
        "missing": "⚠️",  # ⚠️
        "expiring_soon": "⚠️",
        "error": "❌",
    }
    return icons.get(status, "❓")  # ❓ fallback


# ── Individual checkers ────────────────────────────────────────────────────

async def check_bilibili_auth(
    profile_name: str = "",
    sessdata: str = "",
    bili_jct: str = "",
    buvid3: str = "",
    login_time_str: str = "",
) -> dict[str, Any]:
    """Check Bilibili credential validity via API + local heuristics.

    Args:
        profile_name: Profile label (for display only).
        sessdata, bili_jct, buvid3, login_time_str: Credential values.
            When all empty, falls back to config.BILI_* (legacy .env).

    Returns:
        dict with keys: status, days_since_login, login_time, detail, profile
    """
    result: dict[str, Any] = {
        "status": "missing",
        "days_since_login": None,
        "login_time": None,
        "detail": "",
        "profile": profile_name or "default",
    }

    # Fall back to .env if no explicit credential provided
    if not sessdata and not bili_jct:
        sessdata = config.BILI_SESSDATA
        bili_jct = config.BILI_BILI_JCT
        buvid3 = config.BILI_BUVID3
        login_time_str = login_time_str or config.BILI_LOGIN_TIME

    # Check if credentials are configured at all
    bogus = ("your_sessdata_here", "your_bili_jct_here", "")
    if (not sessdata
            or sessdata.lower() in bogus
            or not bili_jct
            or bili_jct.lower() in bogus):
        result["detail"] = "Bilibili 凭据未配置或为占位符，请运行 --login"
        return result

    # Parse login time
    login_time = None
    if login_time_str:
        try:
            login_time = datetime.fromisoformat(login_time_str)
            result["login_time"] = login_time.isoformat()
            result["days_since_login"] = (
                datetime.now(timezone.utc) - login_time
            ).days
        except (ValueError, TypeError):
            pass

    # Verify with Bilibili API
    try:
        from bilibili_api import Credential

        cred = Credential(
            sessdata=sessdata,
            bili_jct=bili_jct,
            buvid3=buvid3 or None,
        )

        is_valid = await cred.check_valid()
        if is_valid:
            result["status"] = "valid"
            if result["days_since_login"] is not None and result["days_since_login"] >= 25:
                result["detail"] = (
                    f"凭据有效，但已登录 {result['days_since_login']} 天，"
                    "B站会话通常约 30 天过期，建议近期重新登录"
                )
            else:
                ago = (
                    f"，{result['days_since_login']} 天前登录"
                    if result["days_since_login"] is not None
                    else ""
                )
                result["detail"] = f"凭据有效{ago}"
        else:
            result["status"] = "expired"
            result["detail"] = "请运行 python main.py --login 重新登录"
    except Exception as exc:
        result["status"] = "error"
        result["detail"] = f"无法验证凭据（网络错误或 API 异常）: {exc}"

    return result


def check_youtube_oauth() -> dict[str, Any]:
    """Check YouTube OAuth token validity from youtube_token.json.

    Returns:
        dict with keys: status, expires_at, remaining_text, has_refresh_token, detail
    """
    result: dict[str, Any] = {
        "status": "missing",
        "expires_at": None,
        "remaining_text": None,
        "has_refresh_token": False,
        "detail": "",
    }

    token_path = Path(config.PROJECT_ROOT) / config.YOUTUBE_TOKEN_FILE
    if not token_path.exists():
        result["detail"] = (
            f"YouTube OAuth token 文件不存在 ({config.YOUTUBE_TOKEN_FILE})，"
            "请运行 python main.py --monitor 进行授权"
        )
        return result

    try:
        data = json.loads(token_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        result["status"] = "error"
        result["detail"] = f"无法读取 token 文件: {exc}"
        return result

    result["has_refresh_token"] = bool(data.get("refresh_token"))

    expiry_str = data.get("expiry")
    if not expiry_str:
        result["status"] = "error"
        result["detail"] = "Token 文件中没有 expiry 字段，文件可能已损坏"
        return result

    try:
        expiry = datetime.fromisoformat(expiry_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        result["status"] = "error"
        result["detail"] = f"无法解析 expiry 时间: {expiry_str}"
        return result

    result["expires_at"] = expiry.isoformat()
    now = datetime.now(timezone.utc)
    remaining = expiry - now
    result["remaining_text"] = _format_remaining(remaining)

    if remaining.total_seconds() <= 0:
        if result["has_refresh_token"]:
            result["status"] = "expired"
            result["detail"] = (
                "Access Token 已过期，但 Refresh Token 可用，"
                "下次运行 --monitor 时将自动刷新"
            )
        else:
            result["status"] = "expired"
            result["detail"] = (
                "Access Token 已过期且没有 Refresh Token，"
                "请删除 youtube_token.json 后重新运行 --monitor 授权"
            )
    else:
        result["status"] = "valid"
        result["detail"] = (
            f"Access Token 还剩 {result['remaining_text']} 有效"
            + ("，Refresh Token 可用" if result["has_refresh_token"] else "")
        )

    return result


def check_youtube_cookies() -> dict[str, Any]:
    """Check YouTube cookie file (cookies.txt) validity.

    Status is determined by the overall cookie health, not just the single
    earliest-expiring cookie.  One expired cookie among many still-valid ones
    is fine — what matters is whether most key cookies are still alive.

    Returns:
        dict with keys: status, cookie_count, expired_count, valid_count,
                        earliest_expiry, latest_expiry, remaining_text, detail
    """
    result: dict[str, Any] = {
        "status": "missing",
        "cookie_count": 0,
        "expired_count": 0,
        "valid_count": 0,
        "earliest_expiry": None,
        "latest_expiry": None,
        "remaining_text": None,
        "detail": "",
    }

    cookie_path = Path(config.PROJECT_ROOT) / config.YOUTUBE_COOKIE_FILE
    if not cookie_path.exists() or cookie_path.stat().st_size == 0:
        result["detail"] = (
            f"Cookie 文件不存在或为空 ({config.YOUTUBE_COOKIE_FILE})，"
            "请运行 python main.py --refresh-youtube-cookies"
        )
        return result

    # Parse cookies
    all_cookies = _parse_netscape_cookies(cookie_path)
    yt_cookies = [c for c in all_cookies if _is_youtube_cookie(c["domain"])]
    result["cookie_count"] = len(yt_cookies)

    if not yt_cookies:
        result["status"] = "error"
        result["detail"] = (
            f"Cookie 文件中没有 YouTube/Google 域名的 Cookie "
            f"({len(all_cookies)} 条其他 Cookie)，"
            "请确保浏览器已登录 YouTube"
        )
        return result

    # Separate cookies with/without expiry
    cookies_with_expiry = [c for c in yt_cookies if c["expires"] is not None]
    session_cookies = [c for c in yt_cookies if c["expires"] is None]

    if not cookies_with_expiry:
        # All cookies are session cookies (no expiry) — use file mtime as heuristic
        mtime = datetime.fromtimestamp(cookie_path.stat().st_mtime, tz=timezone.utc)
        age = datetime.now(timezone.utc) - mtime
        result["status"] = "ok"
        result["valid_count"] = len(session_cookies)
        result["detail"] = (
            f"共 {len(yt_cookies)} 条 Cookie，均为会话 Cookie（无明确过期时间）。"
            f"Cookie 文件 {_format_remaining(age)} 前更新。"
            "建议每 7 天运行 --refresh-youtube-cookies 刷新"
        )
        return result

    now = datetime.now(timezone.utc)

    # Count expired vs valid
    expired = [c for c in cookies_with_expiry if c["expires"] <= now]
    valid = [c for c in cookies_with_expiry if c["expires"] > now]
    result["expired_count"] = len(expired)
    result["valid_count"] = len(valid) + len(session_cookies)

    # Key insight: the status depends on the LATEST (most durable) cookie,
    # not the earliest — that's what keeps us authenticated.
    latest = max(cookies_with_expiry, key=lambda c: c["expires"])
    latest_remaining = latest["expires"] - now
    result["latest_expiry"] = latest["expires"].isoformat()
    result["remaining_text"] = _format_remaining(latest_remaining)

    # Also record earliest for reference
    earliest = min(cookies_with_expiry, key=lambda c: c["expires"])
    result["earliest_expiry"] = earliest["expires"].isoformat()
    earliest_remaining = earliest["expires"] - now
    earliest_text = _format_remaining(earliest_remaining)

    # Determine status based on overall picture
    if result["valid_count"] == 0:
        # All cookies with expiry are dead; session cookies alone can't save us
        result["status"] = "expired"
        result["detail"] = (
            f"共 {len(yt_cookies)} 条 Cookie，全部 {len(cookies_with_expiry)} 条带过期时间的均已过期，"
            "请运行 python main.py --refresh-youtube-cookies"
        )
    elif latest_remaining.total_seconds() <= 0:
        # Shouldn't happen if valid_count > 0, but handle edge case
        result["status"] = "expired"
        result["detail"] = (
            f"共 {len(yt_cookies)} 条，仅 {result['valid_count']} 条仍有效（均为会话 Cookie），"
            "带过期时间的 Cookie 全部过期。请运行 python main.py --refresh-youtube-cookies"
        )
    elif latest_remaining.days <= 3:
        # Latest cookie expires within 3 days — warn regardless of overall health
        result["status"] = "expiring_soon"
        result["detail"] = (
            f"共 {len(yt_cookies)} 条 Cookie（{result['valid_count']} 有效，"
            f"{result['expired_count']} 已过期），"
            f"最持久的 Cookie ({latest['name']}) 仅剩 {result['remaining_text']}。"
            "建议尽快运行 --refresh-youtube-cookies"
        )
    elif result["expired_count"] > len(yt_cookies) // 2:
        # Most cookies are expired, even though some are still valid for a while
        result["status"] = "expiring_soon"
        result["detail"] = (
            f"共 {len(yt_cookies)} 条 Cookie，仅 {result['valid_count']} 有效 "
            f"（{result['expired_count']} 已过期），"
            f"最持久 ({latest['name']}) 还剩 {result['remaining_text']}。"
            "已过期 cookie 超过半数，建议运行 --refresh-youtube-cookies"
        )
    elif result["expired_count"] > 0:
        # A few expired but most are fine
        result["status"] = "ok"
        earliest_ago = _format_remaining(-earliest_remaining)  # positive: how long ago
        result["detail"] = (
            f"共 {len(yt_cookies)} 条 Cookie，{result['valid_count']} 有效，"
            f"{result['expired_count']} 已过期（最早: {earliest['name']} 已于 {earliest_ago}前过期）。"
            f"最持久 ({latest['name']}) 还剩 {result['remaining_text']}，状态良好"
        )
    else:
        # None expired
        result["status"] = "ok"
        result["detail"] = (
            f"共 {len(yt_cookies)} 条 Cookie，全部有效。"
            f"最持久 ({latest['name']}) 还剩 {result['remaining_text']}，"
            f"最早过期 ({earliest['name']}) 还剩 {earliest_text}"
        )

    return result


# ── Orchestrator ───────────────────────────────────────────────────────────

def run_auth_check() -> int:
    """Run all credential checks and print a formatted status report.

    Returns 0 if all credentials are fine, 1 if any issues found.
    """
    import asyncio

    print()
    print("=" * 60)
    print("  凭据状态检查")
    print("=" * 60)
    print()

    any_issues = False

    # ── Bilibili: check profiles (multi-account) or .env (legacy) ──
    from yt2bili import profile as profile_mod

    if profile_mod.is_multi_profile():
        profiles = profile_mod.load_profiles()
        for pname, prof in profiles.items():
            result = asyncio.run(check_bilibili_auth(
                profile_name=pname,
                sessdata=prof.bilibili.sessdata,
                bili_jct=prof.bilibili.bili_jct,
                buvid3=prof.bilibili.buvid3,
                login_time_str=prof.bilibili.login_time,
            ))
            _print_bilibili_result(result, label=f"Bilibili ({pname})")
            if result["status"] in ("expired", "missing", "error"):
                any_issues = True
    else:
        result = asyncio.run(check_bilibili_auth())
        _print_bilibili_result(result, label="Bilibili 凭据 (上传)")
        if result["status"] in ("expired", "missing", "error"):
            any_issues = True

    # ── YouTube OAuth / Cookies ──────────────────────────────
    yt_oauth_result = check_youtube_oauth()
    yt_cookie_result = check_youtube_cookies()

    print("  ┌─ YouTube OAuth (订阅监控)")
    icon = _status_icon(yt_oauth_result["status"])
    print(f"  │ {icon} 状态: {yt_oauth_result['status']}")
    if yt_oauth_result.get("remaining_text"):
        print(f"  │   剩余时间: {yt_oauth_result['remaining_text']}")
    if yt_oauth_result.get("expires_at"):
        print(f"  │   过期时间: {yt_oauth_result['expires_at']}")
    print(f"  │   Refresh Token: {'有' if yt_oauth_result['has_refresh_token'] else '无'}")
    print(f"  │   {yt_oauth_result['detail']}")
    print("  └─")
    print()

    if yt_oauth_result["status"] in ("expired", "missing", "error"):
        any_issues = True

    print("  ┌─ YouTube Cookies (下载)")
    icon = _status_icon(yt_cookie_result["status"])
    print(f"  │ {icon} 状态: {yt_cookie_result['status']}")
    print(f"  │   Cookie 数量: {yt_cookie_result['cookie_count']} (有效 {yt_cookie_result.get('valid_count', '?')}，过期 {yt_cookie_result.get('expired_count', '?')})")
    if yt_cookie_result.get("remaining_text"):
        print(f"  │   最持久 Cookie 剩余: {yt_cookie_result['remaining_text']}")
    if yt_cookie_result.get("latest_expiry"):
        print(f"  │   最晚过期时间: {yt_cookie_result['latest_expiry']}")
    if yt_cookie_result.get("earliest_expiry"):
        print(f"  │   最早过期时间: {yt_cookie_result['earliest_expiry']}")
    print(f"  │   {yt_cookie_result['detail']}")
    print("  └─")
    print()

    if yt_cookie_result["status"] in ("expired", "missing", "error"):
        any_issues = True

    # ── Summary ──
    print("=" * 60)
    if any_issues:
        print("  ⚠️  存在需要注意的凭据问题，请根据上述提示处理")
    else:
        print("  ✅ 所有凭据状态正常")
    print("=" * 60)
    print()

    return 1 if any_issues else 0


def _print_bilibili_result(result: dict[str, Any], label: str = "Bilibili 凭据") -> None:
    """Print a single Bilibili auth check result."""
    icon = _status_icon(result["status"])
    print(f"  ┌─ {label}")
    print(f"  │ {icon} 状态: {result['status']}")
    if result.get("days_since_login") is not None:
        print(f"  │   登录时间: {result['days_since_login']} 天前 ({result.get('login_time', '?')})")
    print(f"  │   {result['detail']}")
    print("  └─")
    print()
