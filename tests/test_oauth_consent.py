"""Pure-logic tests for the OAuth auto-consent robot (no browser, no network)."""

import io
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from yt2bili.youtube import oauth_consent as oc


def S(**kwargs) -> oc.PageSnapshot:
    return oc.PageSnapshot(**kwargs)


class FakeDriver:
    """Scripted ConsentDriver double: pops snapshots from a queue, records clicks."""

    def __init__(self, scenes):
        self._scenes = list(scenes)
        self._last = self._scenes[-1] if self._scenes else S()
        self.clicks = []  # (method, arg)
        self.goto_url = None
        self.closed = False

    def goto(self, url):
        self.goto_url = url

    def snapshot(self):
        if self._scenes:
            self._last = self._scenes.pop(0)
        return self._last

    def click_continue(self):
        self.clicks.append(("continue", None))

    def click_account(self, identifier):
        self.clicks.append(("account", identifier))

    def click_advanced(self):
        self.clicks.append(("advanced", None))

    def click_unsafe(self):
        self.clicks.append(("unsafe", None))

    def wait_loaded(self, timeout_ms=10_000):
        self.clicks.append(("wait_loaded", None))

    def close(self):
        self.closed = True


# Local alias so the tests read naturally
decide = oc.decide_action


class DecideActionTests(unittest.TestCase):
    """Pure decision function."""

    def test_localhost_redirect_is_done(self):
        self.assertEqual(decide(S(url="http://localhost:8322/")), ("done", ""))

    def test_localhost_requires_port_scheme(self):
        # "http://localhost.evil.com" must not be mistaken for the redirect
        self.assertEqual(
            decide(S(url="http://localhost.evil.com/x")), ("wait", "")
        )

    def test_unsafe_wins_over_advanced(self):
        snap = S(advanced_visible=True, unsafe_visible=True)
        self.assertEqual(decide(snap), ("unsafe", ""))

    def test_advanced(self):
        self.assertEqual(decide(S(advanced_visible=True)), ("advanced", ""))

    def test_account_chooser_picks_first(self):
        snap = S(account_identifiers=("a@gmail.com", "b@gmail.com"))
        self.assertEqual(decide(snap), ("account", "a@gmail.com"))

    def test_account_chooser_respects_target(self):
        snap = S(account_identifiers=("a@gmail.com", "b@gmail.com"))
        self.assertEqual(decide(snap, "B@GMAIL.com"), ("account", "b@gmail.com"))

    def test_account_chooser_ignores_unknown_target(self):
        snap = S(account_identifiers=("a@gmail.com",))
        self.assertEqual(decide(snap, "nobody@example.com"), ("account", "a@gmail.com"))

    def test_continue(self):
        self.assertEqual(decide(S(continue_visible=True)), ("continue", ""))

    def test_wait_when_nothing_clickable(self):
        self.assertEqual(decide(S(url="https://accounts.google.com/x")), ("wait", ""))


class ConsentRobotTests(unittest.TestCase):
    """Robot walking algorithm against the fake driver."""

    def _robot(self, scenes, **kwargs):
        kwargs.setdefault("poll_interval", 0)
        driver = FakeDriver(scenes)
        robot = oc.ConsentRobot(driver, **kwargs)
        return driver, robot

    def test_walks_full_consent_flow(self):
        scenes = [
            S(url="https://accounts.google.com/signin/oauth/consent",
              account_identifiers=("me@gmail.com",)),
            S(url="https://accounts.google.com/consentsummary", advanced_visible=True),
            S(url="https://accounts.google.com/consentsummary", advanced_visible=True,
              unsafe_visible=True),
            S(url="https://accounts.google.com/consentsummary", continue_visible=True),
            S(url="https://accounts.google.com/consentsummary", continue_visible=True),
            S(url="http://localhost:8322/?code=xyz"),
        ]
        driver, robot = self._robot(scenes)
        self.assertTrue(robot.run("https://auth.example/start"))
        self.assertEqual(driver.goto_url, "https://auth.example/start")
        self.assertEqual(
            driver.clicks,
            [
                ("account", "me@gmail.com"),
                ("advanced", None),
                ("unsafe", None),
                ("continue", None),
                ("continue", None),
                ("wait_loaded", None),
            ],
        )

    def test_times_out_when_page_never_finishes(self):
        driver, robot = self._robot(
            [S(url="https://accounts.google.com/login",
                login_form_visible=True)],
            timeout_seconds=0.05,
            poll_interval=0.01,
        )
        self.assertFalse(robot.run("https://auth.example/start"))

    def test_prints_login_hint_once(self):
        driver, robot = self._robot(
            [S(url="https://accounts.google.com/login",
                login_form_visible=True)],
            timeout_seconds=0.06,
            poll_interval=0.01,
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            robot.run("https://auth.example/start")
        output = buf.getvalue()
        self.assertEqual(output.count("登录一次"), 1)

    def test_redirect_without_any_clicks(self):
        driver, robot = self._robot([S(url="http://localhost:9999/?code=x")])
        self.assertTrue(robot.run("https://auth.example/start"))
        self.assertEqual(driver.clicks, [("wait_loaded", None)])


class AutoConsentTests(unittest.TestCase):
    """Top-level auto_consent() fallbacks."""

    def test_returns_false_when_driver_raises(self):
        with patch.object(oc, "PlaywrightDriver", side_effect=RuntimeError("no edge")):
            self.assertFalse(oc.auto_consent("https://auth.example/start"))

    def test_returns_false_when_playwright_missing(self):
        with patch.object(oc, "PlaywrightDriver", side_effect=ImportError("no pw")):
            self.assertFalse(oc.auto_consent("https://auth.example/start"))

    def test_success_closes_driver(self):
        fake = FakeDriver([S(url="http://localhost:8322/?code=x")])
        with patch.object(oc, "PlaywrightDriver", return_value=fake):
            self.assertTrue(oc.auto_consent("https://auth.example/start"))
        self.assertTrue(fake.closed)

    def test_config_defaults_flow_into_driver(self):
        fake = FakeDriver([S(url="http://localhost:8322/?code=x")])
        captured = {}

        def _fake_driver(profile_dir, channel, screenshot_dir=None):
            captured["profile_dir"] = profile_dir
            captured["channel"] = channel
            captured["screenshot_dir"] = screenshot_dir
            return fake

        with patch.object(oc, "PlaywrightDriver", side_effect=_fake_driver):
            oc.auto_consent("https://auth.example/start")
        self.assertIsInstance(captured["profile_dir"], Path)
        from yt2bili import config

        self.assertEqual(captured["channel"], config.YOUTUBE_OAUTH_BROWSER_CHANNEL)


def _wait_until(predicate, timeout=2.0):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class AutoConsentBrowserTests(unittest.TestCase):
    """webbrowser.open/get patching context manager (robot runs in a thread)."""

    def test_patches_open_and_restores_after(self):
        import webbrowser

        original = webbrowser.open
        robot = Mock(return_value=True)
        with patch.object(oc, "_playwright_installed", return_value=True), \
             patch.object(oc, "auto_consent", robot):
            with oc.auto_consent_browser():
                self.assertTrue(webbrowser.open("https://auth.example/start"))
            self.assertTrue(_wait_until(lambda: robot.called))
        robot.assert_called_once_with("https://auth.example/start", recording_file=None)
        self.assertIs(webbrowser.open, original)

    def test_passes_recording_file_through(self):
        import webbrowser

        rec_file = Path("config/youtube_token.recording.json")
        robot = Mock(return_value=True)
        with patch.object(oc, "_playwright_installed", return_value=True), \
             patch.object(oc, "auto_consent", robot):
            with oc.auto_consent_browser(recording_file=rec_file):
                webbrowser.open("https://auth.example/start")
            self.assertTrue(_wait_until(lambda: robot.called))
        robot.assert_called_once_with(
            "https://auth.example/start", recording_file=rec_file
        )

    def test_falls_back_to_manual_browser_when_robot_fails(self):
        import webbrowser

        with patch.object(oc, "_playwright_installed", return_value=True), \
             patch.object(oc, "auto_consent", return_value=False), \
             patch.object(webbrowser, "open") as original_mock:
            with oc.auto_consent_browser():
                webbrowser.open("https://auth.example/start")
            self.assertTrue(_wait_until(lambda: original_mock.called))
        original_mock.assert_called_once_with("https://auth.example/start")

    def test_robot_exception_still_opens_manual_browser(self):
        import webbrowser

        with patch.object(oc, "_playwright_installed", return_value=True), \
             patch.object(oc, "auto_consent", side_effect=RuntimeError("boom")), \
             patch.object(webbrowser, "open") as original_mock:
            with oc.auto_consent_browser():
                webbrowser.open("https://auth.example/start")
            self.assertTrue(_wait_until(lambda: original_mock.called))
        original_mock.assert_called_once_with("https://auth.example/start")

    def test_no_patch_when_playwright_missing(self):
        import webbrowser

        original_open = webbrowser.open
        original_get = webbrowser.get
        with patch.object(oc, "_playwright_installed", return_value=False):
            with oc.auto_consent_browser():
                self.assertIs(webbrowser.open, original_open)
                self.assertIs(webbrowser.get, original_get)
        self.assertIs(webbrowser.open, original_open)
        self.assertIs(webbrowser.get, original_get)

    def test_get_starts_robot_in_background(self):
        # Newer google-auth-oauthlib calls webbrowser.get().open().
        import webbrowser

        robot = Mock(return_value=True)
        real_controller = Mock()
        with patch.object(webbrowser, "get", return_value=real_controller), \
             patch.object(oc, "_playwright_installed", return_value=True), \
             patch.object(oc, "auto_consent", robot):
            with oc.auto_consent_browser():
                result = webbrowser.get().open(
                    "https://auth.example/start", new=1, autoraise=True
                )
            self.assertTrue(result)
            self.assertTrue(_wait_until(lambda: robot.called))
        robot.assert_called_once_with(
            "https://auth.example/start", recording_file=None
        )
        real_controller.open.assert_not_called()

    def test_get_falls_back_to_manual_browser_when_robot_fails(self):
        import webbrowser

        real_controller = Mock()
        real_controller.open.return_value = True
        with patch.object(webbrowser, "get", return_value=real_controller), \
             patch.object(webbrowser, "open") as original_mock, \
             patch.object(oc, "_playwright_installed", return_value=True), \
             patch.object(oc, "auto_consent", return_value=False):
            with oc.auto_consent_browser():
                result = webbrowser.get().open(
                    "https://auth.example/start", new=1, autoraise=True
                )
            self.assertTrue(result)
            self.assertTrue(_wait_until(lambda: original_mock.called))
        # The fallback goes through module-level webbrowser.open (the
        # controller of the get() proxy is not used for browsing).
        original_mock.assert_called_once_with("https://auth.example/start")
        real_controller.open.assert_not_called()

    def test_disabled_flag_skips_automation(self):
        import webbrowser

        original = webbrowser.open
        with patch.object(oc, "_playwright_installed", return_value=True), \
             patch.object(oc, "auto_consent") as robot:
            with oc.auto_consent_browser(enabled=False):
                self.assertIs(webbrowser.open, original)
        robot.assert_not_called()
        self.assertIs(webbrowser.open, original)


class OAuthConsentLockTests(unittest.TestCase):
    """Single-flight lock file logic (concurrent monitor processes)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.token = Path(self._tmp.name) / "youtube_token.json"
        self.lock = Path(str(self.token) + ".lock")

    def test_acquires_when_free_and_releases_after(self):
        with oc.oauth_consent_lock(self.token, timeout_seconds=1, poll_interval=0) as got:
            self.assertTrue(got)
            self.assertTrue(self.lock.exists())
            self.assertEqual(int(self.lock.read_text(encoding="utf-8")), os.getpid())
        self.assertFalse(self.lock.exists())

    def test_yields_false_when_token_appears_while_waiting(self):
        self.lock.write_text(str(os.getpid()), encoding="utf-8")  # held live
        release = threading.Timer(0.2, lambda: self.token.write_text("{}", encoding="utf-8"))
        release.start()
        try:
            with patch.object(oc, "_pid_alive", return_value=True):
                with oc.oauth_consent_lock(
                    self.token, timeout_seconds=5, poll_interval=0.01
                ) as got:
                    self.assertFalse(got)
        finally:
            release.join()
        # The other process's lock was left untouched
        self.assertEqual(int(self.lock.read_text(encoding="utf-8")), os.getpid())

    def test_takes_over_stale_lock(self):
        self.lock.write_text("999999", encoding="utf-8")  # dead PID
        with patch.object(oc, "_pid_alive", return_value=False):
            with oc.oauth_consent_lock(
                self.token, timeout_seconds=1, poll_interval=0
            ) as got:
                self.assertTrue(got)
                self.assertEqual(int(self.lock.read_text(encoding="utf-8")), os.getpid())
        self.assertFalse(self.lock.exists())

    def test_timeout_yields_true_when_lock_never_released(self):
        self.lock.write_text(str(os.getpid()), encoding="utf-8")  # held live
        with patch.object(oc, "_pid_alive", return_value=True):
            with oc.oauth_consent_lock(
                self.token, timeout_seconds=0.1, poll_interval=0.01
            ) as got:
                self.assertTrue(got)
        # Other process's lock was not deleted by the timed-out waiter
        self.assertTrue(self.lock.exists())

    def test_pid_alive_recognizes_current_process(self):
        # Real platform check (not mocked): Windows goes through the
        # OpenProcess API, POSIX through os.kill — both must work.
        self.assertTrue(oc._pid_alive(os.getpid()))

    def test_pid_alive_rejects_impossible_pid(self):
        self.assertFalse(oc._pid_alive(999999999))


if __name__ == "__main__":
    unittest.main()
