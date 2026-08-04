"""自测：凭据状态检查（Bilibili API 校验、YouTube OAuth/Cookie 检查、汇总报告）。"""

import asyncio
import io
import json
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from yt2bili import auth_checker as ac
from yt2bili import config


def _cookie_line(domain=".youtube.com", name="SID", value="v",
                 expires_ts=None, path="/", secure="TRUE"):
    if expires_ts is None:
        expires_ts = 0
    return f"{domain}\tTRUE\t{path}\t{secure}\t{expires_ts}\t{name}\t{value}"


class FormatRemainingTests(unittest.TestCase):
    def test_days_hours(self):
        self.assertEqual(ac._format_remaining(timedelta(days=5, hours=3)), "5 天 3 小时")

    def test_minutes_only_without_days(self):
        self.assertEqual(ac._format_remaining(timedelta(minutes=45)), "45 分钟")

    def test_seconds(self):
        self.assertEqual(ac._format_remaining(timedelta(seconds=30)), "30 秒")

    def test_expired(self):
        self.assertEqual(ac._format_remaining(timedelta(seconds=-5)), "已过期")


class ParseNetscapeCookiesTests(unittest.TestCase):
    def test_parses_full_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "cookies.txt"
            p.write_text(
                "# comment line\n"
                f"{_cookie_line(expires_ts=1760000000)}\n"
                "short_line_without_tabs\n",
                encoding="utf-8",
            )
            cookies = ac._parse_netscape_cookies(p)
        self.assertEqual(len(cookies), 1)
        self.assertEqual(cookies[0]["name"], "SID")
        self.assertEqual(cookies[0]["domain"], ".youtube.com")
        self.assertTrue(cookies[0]["secure"])
        self.assertEqual(
            cookies[0]["expires"],
            datetime.fromtimestamp(1760000000, tz=timezone.utc),
        )

    def test_expires_zero_means_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "cookies.txt"
            p.write_text(_cookie_line(expires_ts=0) + "\n", encoding="utf-8")
            cookies = ac._parse_netscape_cookies(p)
        self.assertIsNone(cookies[0]["expires"])

    def test_missing_file_returns_empty(self):
        self.assertEqual(ac._parse_netscape_cookies(Path("no/such.txt")), [])

    def test_secure_false_parsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "cookies.txt"
            p.write_text(_cookie_line(secure="FALSE") + "\n", encoding="utf-8")
            cookies = ac._parse_netscape_cookies(p)
        self.assertFalse(cookies[0]["secure"])


class IsYoutubeCookieTests(unittest.TestCase):
    def test_youtube_domains(self):
        for d in ("youtube.com", ".youtube.com", "www.youtube.com",
                  "youtube-nocookie.com", ".google.com", "accounts.google.com"):
            self.assertTrue(ac._is_youtube_cookie(d), d)

    def test_other_domains(self):
        for d in ("example.com", "bilibili.com", ".baidu.com"):
            self.assertFalse(ac._is_youtube_cookie(d), d)


class CheckBilibiliAuthTests(unittest.TestCase):
    def test_missing_credentials(self):
        # 真实 .env 里可能已配置凭据 — 必须清空才能测 missing 分支
        with patch.object(config, "BILI_SESSDATA", ""), \
             patch.object(config, "BILI_BILI_JCT", ""):
            result = asyncio.run(ac.check_bilibili_auth())
        self.assertEqual(result["status"], "missing")
        self.assertIn("未配置", result["detail"])

    def test_placeholder_credentials(self):
        result = asyncio.run(ac.check_bilibili_auth(
            sessdata="your_sessdata_here", bili_jct="your_bili_jct_here"))
        self.assertEqual(result["status"], "missing")

    def test_env_fallback_and_login_time(self):
        with patch.object(config, "BILI_SESSDATA", ""), \
             patch.object(config, "BILI_BILI_JCT", ""):
            result = asyncio.run(ac.check_bilibili_auth())
        self.assertEqual(result["status"], "missing")

    def test_valid_credential(self):
        with patch("bilibili_api.Credential") as mock_cred:
            mock_cred.return_value.check_valid = AsyncMock(return_value=True)
            result = asyncio.run(ac.check_bilibili_auth(
                sessdata="s", bili_jct="j"))
        self.assertEqual(result["status"], "valid")
        self.assertIn("凭据有效", result["detail"])
        # Credential 构造参数
        kwargs = mock_cred.call_args.kwargs
        self.assertEqual(kwargs["sessdata"], "s")

    def test_valid_but_login_time_old(self):
        old_login = (datetime.now(timezone.utc) - timedelta(days=28)).isoformat()
        with patch("bilibili_api.Credential") as mock_cred:
            mock_cred.return_value.check_valid = AsyncMock(return_value=True)
            result = asyncio.run(ac.check_bilibili_auth(
                sessdata="s", bili_jct="j", login_time_str=old_login))
        self.assertEqual(result["status"], "valid")
        self.assertEqual(result["days_since_login"], 28)
        self.assertIn("建议近期重新登录", result["detail"])

    def test_invalid_credential(self):
        with patch("bilibili_api.Credential") as mock_cred:
            mock_cred.return_value.check_valid = AsyncMock(return_value=False)
            result = asyncio.run(ac.check_bilibili_auth(sessdata="s", bili_jct="j"))
        self.assertEqual(result["status"], "expired")

    def test_api_exception_reported_as_error(self):
        with patch("bilibili_api.Credential") as mock_cred:
            mock_cred.return_value.check_valid = AsyncMock(side_effect=RuntimeError("down"))
            result = asyncio.run(ac.check_bilibili_auth(sessdata="s", bili_jct="j"))
        self.assertEqual(result["status"], "error")
        self.assertIn("down", result["detail"])


class CheckYoutubeOAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.token_file = Path(self.tmp.name) / "token.json"

    def _write(self, data):
        self.token_file.write_text(json.dumps(data), encoding="utf-8")

    def test_missing_file(self):
        with patch.object(config, "YOUTUBE_TOKEN_FILE", str(self.token_file)):
            result = ac.check_youtube_oauth()
        self.assertEqual(result["status"], "missing")

    def test_corrupt_json(self):
        self.token_file.write_text("{broken", encoding="utf-8")
        with patch.object(config, "YOUTUBE_TOKEN_FILE", str(self.token_file)):
            result = ac.check_youtube_oauth()
        self.assertEqual(result["status"], "error")

    def test_no_expiry_field(self):
        self._write({"refresh_token": "rt"})
        with patch.object(config, "YOUTUBE_TOKEN_FILE", str(self.token_file)):
            result = ac.check_youtube_oauth()
        self.assertEqual(result["status"], "error")

    def test_valid_with_refresh(self):
        # 留 1 小时余量，避免 now 到 expiry 差几微秒导致 "4 天 23 小时"
        expiry = (datetime.now(timezone.utc) + timedelta(days=5, hours=1)).isoformat()
        self._write({"refresh_token": "rt", "expiry": expiry})
        with patch.object(config, "YOUTUBE_TOKEN_FILE", str(self.token_file)):
            result = ac.check_youtube_oauth()
        self.assertEqual(result["status"], "valid")
        self.assertTrue(result["has_refresh_token"])
        self.assertIn("5 天", result["remaining_text"])

    def test_expired_with_refresh(self):
        expiry = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        self._write({"refresh_token": "rt", "expiry": expiry})
        with patch.object(config, "YOUTUBE_TOKEN_FILE", str(self.token_file)):
            result = ac.check_youtube_oauth()
        self.assertEqual(result["status"], "expired")
        self.assertIn("自动刷新", result["detail"])

    def test_expired_without_refresh(self):
        expiry = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        self._write({"expiry": expiry})
        with patch.object(config, "YOUTUBE_TOKEN_FILE", str(self.token_file)):
            result = ac.check_youtube_oauth()
        self.assertEqual(result["status"], "expired")
        self.assertIn("删除 youtube_token.json", result["detail"])

    def test_z_suffix_expiry_parsed(self):
        expiry = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat().replace(
            "+00:00", "Z")
        self._write({"expiry": expiry})
        with patch.object(config, "YOUTUBE_TOKEN_FILE", str(self.token_file)):
            result = ac.check_youtube_oauth()
        self.assertEqual(result["status"], "valid")


class CheckYoutubeCookiesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cookie_file = Path(self.tmp.name) / "cookies.txt"

    def _write(self, lines):
        content = ("\n".join(lines) + "\n") if lines else ""
        self.cookie_file.write_text(content, encoding="utf-8")

    def _days(self, n):
        return int(time.time()) + n * 86400

    def test_missing_file(self):
        with patch.object(config, "YOUTUBE_COOKIE_FILE", str(self.cookie_file)):
            result = ac.check_youtube_cookies()
        self.assertEqual(result["status"], "missing")

    def test_empty_file(self):
        self._write([])
        with patch.object(config, "YOUTUBE_COOKIE_FILE", str(self.cookie_file)):
            result = ac.check_youtube_cookies()
        self.assertEqual(result["status"], "missing")

    def test_no_youtube_cookies(self):
        self._write([_cookie_line(domain="example.com", expires_ts=self._days(10))])
        with patch.object(config, "YOUTUBE_COOKIE_FILE", str(self.cookie_file)):
            result = ac.check_youtube_cookies()
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["cookie_count"], 0)

    def test_all_session_cookies_ok(self):
        self._write([_cookie_line(expires_ts=0), _cookie_line(name="LOGIN_INFO", expires_ts=0)])
        with patch.object(config, "YOUTUBE_COOKIE_FILE", str(self.cookie_file)):
            result = ac.check_youtube_cookies()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["valid_count"], 2)

    def test_all_expired(self):
        self._write([_cookie_line(expires_ts=self._days(-5)),
                     _cookie_line(name="LOGIN_INFO", expires_ts=self._days(-2))])
        with patch.object(config, "YOUTUBE_COOKIE_FILE", str(self.cookie_file)):
            result = ac.check_youtube_cookies()
        self.assertEqual(result["status"], "expired")
        self.assertEqual(result["expired_count"], 2)

    def test_expiring_soon_within_3_days(self):
        """最持久的 Cookie 也只剩 3 天内 → 预警。"""
        self._write([_cookie_line(name="SID", expires_ts=self._days(2)),
                     _cookie_line(name="HSID", expires_ts=self._days(1))])
        with patch.object(config, "YOUTUBE_COOKIE_FILE", str(self.cookie_file)):
            result = ac.check_youtube_cookies()
        self.assertEqual(result["status"], "expiring_soon")

    def test_mostly_expired_warns(self):
        self._write([_cookie_line(name="SID", expires_ts=self._days(-1)),
                     _cookie_line(name="HSID", expires_ts=self._days(-2)),
                     _cookie_line(name="LOGIN_INFO", expires_ts=self._days(30))])
        with patch.object(config, "YOUTUBE_COOKIE_FILE", str(self.cookie_file)):
            result = ac.check_youtube_cookies()
        self.assertEqual(result["status"], "expiring_soon")
        self.assertEqual(result["valid_count"], 1)

    def test_few_expired_but_mostly_ok(self):
        self._write([_cookie_line(name="SID", expires_ts=self._days(-1)),
                     _cookie_line(name="LOGIN_INFO", expires_ts=self._days(30)),
                     _cookie_line(name="HSID", expires_ts=self._days(30))])
        with patch.object(config, "YOUTUBE_COOKIE_FILE", str(self.cookie_file)):
            result = ac.check_youtube_cookies()
        self.assertEqual(result["status"], "ok")

    def test_all_valid(self):
        self._write([_cookie_line(name="SID", expires_ts=self._days(30)),
                     _cookie_line(name="LOGIN_INFO", expires_ts=self._days(60))])
        with patch.object(config, "YOUTUBE_COOKIE_FILE", str(self.cookie_file)):
            result = ac.check_youtube_cookies()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["expired_count"], 0)


class RunAuthCheckTests(unittest.TestCase):
    """run_auth_check：汇总各检查项并返回退出码。"""

    def _run(self, statuses: dict):
        def make_result(status):
            return {"status": status, "detail": "", "days_since_login": None,
                    "login_time": None, "has_refresh_token": False,
                    "remaining_text": None, "expires_at": None,
                    "cookie_count": 0, "valid_count": 0, "expired_count": 0,
                    "latest_expiry": None, "earliest_expiry": None}
        # check_bilibili_auth 是 async，asyncio.run 需要 coroutine → AsyncMock
        with patch.object(ac, "check_bilibili_auth",
                          AsyncMock(return_value=make_result(statuses.get("bili", "valid")))), \
             patch.object(ac, "check_youtube_oauth",
                          return_value=make_result(statuses.get("oauth", "valid"))), \
             patch.object(ac, "check_youtube_cookies",
                          return_value=make_result(statuses.get("cookies", "valid"))), \
             patch("yt2bili.profile.is_multi_profile", return_value=False), \
             patch("yt2bili.profile.load_profiles", return_value={}):
            buf = io.StringIO()
            with redirect_stdout(buf):
                return ac.run_auth_check()

    def test_all_valid_returns_zero(self):
        self.assertEqual(self._run({}), 0)

    def test_any_expired_returns_one(self):
        self.assertEqual(self._run({"cookies": "expired"}), 1)
        self.assertEqual(self._run({"oauth": "missing"}), 1)
        self.assertEqual(self._run({"bili": "error"}), 1)


if __name__ == "__main__":
    unittest.main()
