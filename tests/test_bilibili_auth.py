"""自测：Bilibili 登录（get_credential 凭证解析、QR 登录兜底、.env 保存）。"""

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from yt2bili import config
from yt2bili.bilibili import auth


class GetCredentialTests(unittest.TestCase):
    """get_credential：.env / profile 凭证优先，缺失时走 QR 登录。"""

    def _fake_credential(self):
        return SimpleNamespace(sessdata="sess", bili_jct="jct", buvid3="b3")

    def test_legacy_env_credentials(self):
        with patch("bilibili_api.Credential") as mock_cred, \
             patch("yt2bili.profile.is_multi_profile", return_value=False), \
             patch("yt2bili.profile.get_active_profile_name", return_value="default"), \
             patch.object(config, "BILI_SESSDATA", "envs"), \
             patch.object(config, "BILI_BILI_JCT", "envj"), \
             patch.object(config, "BILI_BUVID3", "envb"), \
             patch.object(config, "validate", return_value=[]):
            cred = auth.get_credential()
        self.assertIs(cred, mock_cred.return_value)
        kwargs = mock_cred.call_args.kwargs
        self.assertEqual(kwargs["sessdata"], "envs")
        self.assertEqual(kwargs["bili_jct"], "envj")

    def test_active_profile_replaces_default(self):
        """default + 激活了其他 profile → 用活动 profile 的凭据。"""
        with patch("bilibili_api.Credential") as mock_cred, \
             patch("yt2bili.profile.is_multi_profile", return_value=True), \
             patch("yt2bili.profile.get_active_profile_name", return_value="snap"), \
             patch("yt2bili.profile.resolve_profile") as mock_resolve:
            mock_resolve.return_value = SimpleNamespace(
                bilibili=SimpleNamespace(sessdata="ps", bili_jct="pj", buvid3="pb"))
            auth.get_credential()  # profile_name 默认 "default"
        self.assertEqual(mock_resolve.call_args.args[0], "snap")
        self.assertEqual(mock_cred.call_args.kwargs["sessdata"], "ps")

    def test_profile_credentials(self):
        with patch("bilibili_api.Credential") as mock_cred, \
             patch("yt2bili.profile.is_multi_profile", return_value=True), \
             patch("yt2bili.profile.get_active_profile_name", return_value="default"), \
             patch("yt2bili.profile.resolve_profile") as mock_resolve:
            mock_resolve.return_value = SimpleNamespace(
                bilibili=SimpleNamespace(sessdata="ps", bili_jct="pj", buvid3=None))
            cred = auth.get_credential(profile_name="snap")
        self.assertIs(cred, mock_cred.return_value)
        self.assertEqual(mock_cred.call_args.kwargs["buvid3"], None)

    def test_missing_credentials_triggers_qr_login(self):
        with patch("bilibili_api.Credential") as mock_cred, \
             patch("yt2bili.profile.is_multi_profile", return_value=False), \
             patch("yt2bili.profile.get_active_profile_name", return_value="default"), \
             patch.object(config, "validate",
                          return_value=["Missing required config: BILI_SESSDATA"]), \
             patch.object(auth, "_qr_login_flow",
                          return_value=self._fake_credential()) as mock_qr, \
             patch.object(auth, "_save_credential") as mock_save:
            cred = auth.get_credential()
        mock_qr.assert_called_once()
        mock_save.assert_called_once()
        self.assertEqual(cred.sessdata, "sess")

    def test_profile_missing_credentials_triggers_qr_login(self):
        with patch("bilibili_api.Credential"), \
             patch("yt2bili.profile.is_multi_profile", return_value=True), \
             patch("yt2bili.profile.get_active_profile_name", return_value="default"), \
             patch("yt2bili.profile.resolve_profile", return_value=None), \
             patch.object(auth, "_qr_login_flow", return_value=self._fake_credential()), \
             patch.object(auth, "_save_credential") as mock_save:
            auth.get_credential(profile_name="snap")
        # 保存路径应带上 profile 名
        self.assertEqual(mock_save.call_args.args[1], "snap")


class SaveCredentialToEnvTests(unittest.TestCase):
    """_save_credential_to_env：替换 BILI_* 行、保留其他设置、原子写入。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env_path = Path(self.tmp.name) / "config" / ".env"
        self.env_path.parent.mkdir(parents=True)

    def _patch_root_and_save(self, credential, existing=""):
        self.env_path.write_text(existing, encoding="utf-8")
        with patch.object(config, "PROJECT_ROOT", Path(self.tmp.name)), \
             patch("dotenv.load_dotenv"), \
             patch.object(config, "BILI_SESSDATA", ""), \
             patch.object(config, "BILI_BILI_JCT", ""), \
             patch.object(config, "BILI_BUVID3", ""), \
             patch.object(config, "BILI_LOGIN_TIME", ""):
            auth._save_credential_to_env(credential)
        return self.env_path.read_text(encoding="utf-8")

    def test_replaces_existing_bili_lines_keeps_others(self):
        content = self._patch_root_and_save(
            SimpleNamespace(sessdata="s1", bili_jct="j1", buvid3="b1"),
            existing=(
                "# comment\n"
                "BILI_SESSDATA=old\n"
                "BILI_BILI_JCT=oldj\n"
                "DEFAULT_TID=172\n"
            ),
        )
        self.assertIn("BILI_SESSDATA=s1", content)
        self.assertIn("BILI_BILI_JCT=j1", content)
        self.assertIn("DEFAULT_TID=172", content)
        self.assertNotIn("old", content)
        self.assertIn("BILI_LOGIN_TIME=", content)

    def test_appends_missing_keys(self):
        content = self._patch_root_and_save(
            SimpleNamespace(sessdata="s1", bili_jct="j1", buvid3=""),
            existing="DEFAULT_TID=172\n",
        )
        self.assertIn("BILI_SESSDATA=s1", content)
        self.assertIn("BILI_BILI_JCT=j1", content)
        self.assertNotIn("BILI_BUVID3=", content)  # buvid3 为空则不写

    def test_no_tmp_left_behind(self):
        self._patch_root_and_save(SimpleNamespace(sessdata="s", bili_jct="j", buvid3="b"))
        leftovers = list(self.env_path.parent.glob("*.tmp"))
        self.assertEqual(leftovers, [])


class SaveCredentialTests(unittest.TestCase):
    """_save_credential：多账号写 profiles.json，单账号写 .env。"""

    def test_profile_path_uses_save_profile(self):
        cred = SimpleNamespace(sessdata="s", bili_jct="j", buvid3="b")
        with patch("yt2bili.profile.is_multi_profile", return_value=True), \
             patch("yt2bili.profile.resolve_profile", return_value=None), \
             patch("yt2bili.profile.save_profile") as mock_save:
            auth._save_credential(cred, profile_name="snap")
        saved = mock_save.call_args.args[0]
        self.assertEqual(saved.name, "snap")
        self.assertEqual(saved.bilibili.sessdata, "s")
        self.assertIn("T", saved.bilibili.login_time)  # ISO 时间戳

    def test_legacy_path_uses_env(self):
        cred = SimpleNamespace(sessdata="s", bili_jct="j", buvid3="b")
        with patch("yt2bili.profile.is_multi_profile", return_value=False), \
             patch.object(auth, "_save_credential_to_env") as mock_env:
            auth._save_credential(cred, profile_name="default")
        mock_env.assert_called_once_with(cred)


class LoginInteractiveTests(unittest.TestCase):
    """login_interactive：登录成功 / 用户取消 / 失败。"""

    def test_success_returns_true(self):
        with patch.object(auth, "get_credential", return_value=object()):
            self.assertTrue(auth.login_interactive())

    def test_keyboard_interrupt_returns_false(self):
        with patch.object(auth, "get_credential", side_effect=KeyboardInterrupt):
            self.assertFalse(auth.login_interactive())

    def test_exception_returns_false(self):
        with patch.object(auth, "get_credential", side_effect=RuntimeError("boom")):
            self.assertFalse(auth.login_interactive())


if __name__ == "__main__":
    unittest.main()
