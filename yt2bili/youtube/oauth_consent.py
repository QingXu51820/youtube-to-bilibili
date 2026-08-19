"""
Zero-click Google OAuth consent automation.

``get_youtube_service()`` calls ``InstalledAppFlow.run_local_server()``, which
opens the consent URL in the default browser and blocks until the browser
redirects to ``http://localhost:<port>``.  Normally a human has to click
through Google's screens (account chooser → Continue → Continue, plus
"Advanced → Go to … (unsafe)" for unverified apps).

This module replaces that step with Playwright-driven automation.  When a
``recording_file`` is given, ``auto_consent()`` first replays the human's own
recorded flow (see ``oauth_recorder``) and records it on first use; only
then does it fall back to the built-in robot, which clicks whatever the
consent flow shows with hand-coded rules — as long as the browser profile
already holds a Google login session.  The very first run asks the user to
log in once inside that window; the session then persists in the profile
directory, so every later authorization is fully automatic.

The walking logic talks to a minimal ``ConsentDriver`` protocol rather than
to Playwright directly, so it can be unit-tested with a fake driver (see
``tests/test_oauth_consent.py``) — matching the repo's no-network test policy.
"""

from __future__ import annotations

import os
import re
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol, Tuple

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── Pure decision logic (no I/O, unit-testable) ───────────────────

# The auth URL carries hl=en, but match zh labels too for safety.
_CONTINUE_NAME = re.compile(r"^(Continue|继续)$")
_ADVANCED_TEXT = re.compile(r"^Advanced$")
_UNSAFE_TEXT = re.compile(r"Go to .*\(unsafe\)", re.IGNORECASE)

# kind: done | account | continue | advanced | unsafe | wait
Action = Tuple[str, str]


@dataclass
class PageSnapshot:
    """What the robot can see right now (pure data, easy to fake in tests)."""

    url: str = ""
    account_identifiers: tuple = ()
    continue_visible: bool = False
    advanced_visible: bool = False
    unsafe_visible: bool = False
    login_form_visible: bool = False


def decide_action(snapshot: PageSnapshot, target_account: str = "") -> Action:
    """Pick the next click from a page snapshot (pure function, no I/O)."""
    if snapshot.url.startswith(("http://localhost:", "http://127.0.0.1:")):
        # Redirect reached the local OAuth server — the code was delivered.
        return ("done", "")
    if snapshot.unsafe_visible:
        return ("unsafe", "")
    if snapshot.advanced_visible:
        return ("advanced", "")
    if snapshot.account_identifiers:
        pick = None
        if target_account:
            target = target_account.lower()
            pick = next(
                (i for i in snapshot.account_identifiers if i.lower() == target),
                None,
            )
        return ("account", pick or snapshot.account_identifiers[0])
    if snapshot.continue_visible:
        return ("continue", "")
    return ("wait", "")


# ── Driver interface ──────────────────────────────────────────────


class ConsentDriver(Protocol):
    """Minimal browser abstraction the robot drives (Playwright impl below)."""

    def goto(self, url: str) -> None: ...

    def snapshot(self) -> PageSnapshot: ...

    def click_continue(self) -> None: ...

    def click_account(self, identifier: str) -> None: ...

    def click_advanced(self) -> None: ...

    def click_unsafe(self) -> None: ...

    def wait_loaded(self, timeout_ms: int = 10_000) -> None: ...

    def close(self) -> None: ...


# ── Robot ─────────────────────────────────────────────────────────


class ConsentRobot:
    """Stateless clicker; owns only timing, polling, and the login hint."""

    def __init__(
        self,
        driver: ConsentDriver,
        target_account: str = "",
        timeout_seconds: float = 600.0,
        poll_interval: float = 1.0,
    ):
        self._driver = driver
        self._target_account = target_account
        self._timeout_seconds = timeout_seconds
        self._poll_interval = poll_interval

    def run(self, authorization_url: str) -> bool:
        """Click through the consent flow until the localhost redirect fires.

        Returns True once the page reached the localhost redirect (the code
        was delivered to the caller's local server), False on timeout.
        """
        self._driver.goto(authorization_url)
        deadline = time.monotonic() + self._timeout_seconds
        hint_printed = False
        while time.monotonic() < deadline:
            snapshot = self._driver.snapshot()
            kind, arg = decide_action(snapshot, self._target_account)
            if kind == "done":
                # The URL already shows localhost, but the redirect request
                # may still be in flight — closing the browser now would
                # abort it before the local server receives the code.
                self._driver.wait_loaded()
                time.sleep(0.5)  # grace for the response to fully land
                return True
            if kind == "account":
                self._driver.click_account(arg)
            elif kind == "continue":
                self._driver.click_continue()
            elif kind == "advanced":
                self._driver.click_advanced()
            elif kind == "unsafe":
                self._driver.click_unsafe()
            else:
                if snapshot.login_form_visible and not hint_printed:
                    print(
                        "[OAuth] 检测到 Google 登录页 —— 请在弹出的浏览器窗口中登录一次。\n"
                        "        登录状态会保存在浏览器 profile 中，之后将全程自动。"
                    )
                    hint_printed = True
                time.sleep(self._poll_interval)
        return False


# ── Playwright driver ─────────────────────────────────────────────


class PlaywrightDriver:
    """Playwright adapter over the ConsentDriver protocol (real browser)."""

    # A crashed previous run can leave these behind; a new Edge instance
    # launched on the same profile dir would forward to the dead instance
    # and Playwright would wait forever for a handshake. Best-effort cleanup.
    _PROFILE_LOCK_FILES = ("SingletonLock", "SingletonCookie", "SingletonSocket")

    def __init__(
        self,
        profile_dir: Path,
        channel: str = "msedge",
        screenshot_dir: Optional[Path] = None,
    ):
        from playwright.sync_api import sync_playwright

        profile_dir.mkdir(parents=True, exist_ok=True)
        for name in self._PROFILE_LOCK_FILES:
            try:
                (profile_dir / name).unlink(missing_ok=True)
            except OSError:
                pass

        # Optional step recording: a screenshot + one line in steps.txt for
        # each distinct page state, so a failed flow can be debugged later.
        self._shot_dir = screenshot_dir
        self._shot_log = None
        self._shot_counter = 0
        self._last_state_key = None
        if self._shot_dir is not None:
            self._shot_dir.mkdir(parents=True, exist_ok=True)
            self._shot_log = self._shot_dir / "steps.txt"

        self._pw = sync_playwright().start()
        self._context = None
        self._page = None

        # Deduplicate while keeping order; skip the bundled "chromium" channel
        # because it would require `playwright install` (we only use the
        # system-installed Edge/Chrome).
        channels = [channel] if channel else ["msedge", "chrome"]
        seen, ordered = set(), []
        for candidate in channels:
            if candidate not in seen:
                seen.add(candidate)
                ordered.append(candidate)

        last_error: Optional[Exception] = None
        for candidate in ordered:
            try:
                self._context = self._pw.chromium.launch_persistent_context(
                    user_data_dir=str(profile_dir),
                    channel=candidate,
                    headless=False,
                    timeout=60_000,
                    args=["--disable-blink-features=AutomationControlled"],
                )
                break
            except Exception as exc:  # browser not installed etc.
                last_error = exc
        if self._context is None:
            raise RuntimeError(
                f"playwright 无法启动浏览器 (tried: {ordered}): {last_error}"
            )

        self._page = (
            self._context.pages[0] if self._context.pages else self._context.new_page()
        )

    def goto(self, url: str) -> None:
        self._page.goto(url, wait_until="domcontentloaded", timeout=60_000)

    def snapshot(self) -> PageSnapshot:
        """Read the current page state; never raises.

        A navigation can destroy the execution context between polls
        ("Execution context was destroyed"); that's a normal mid-navigation
        state, not an error — degrade to an empty snapshot and let the next
        poll re-read the new page.
        """
        page = self._page
        try:
            identifiers = page.locator("div[data-identifier]").evaluate_all(
                "els => els.map(e => e.getAttribute('data-identifier') || '')"
            )
            snap = PageSnapshot(
                url=page.url,
                account_identifiers=tuple(i for i in identifiers if i),
                continue_visible=page.get_by_role(
                    "button", name=_CONTINUE_NAME
                ).is_visible(),
                advanced_visible=page.get_by_text(_ADVANCED_TEXT).is_visible(),
                unsafe_visible=page.get_by_text(_UNSAFE_TEXT).is_visible(),
                login_form_visible=(
                    page.locator("input[type=email]").count() > 0
                    or page.locator("input[type=password]").count() > 0
                ),
            )
        except Exception:
            try:
                snap = PageSnapshot(url=page.url)
            except Exception:
                snap = PageSnapshot()
        if self._shot_dir is not None:
            self._record(snap)
        return snap

    def _record(self, snap: PageSnapshot) -> None:
        """Save a screenshot + state line when the page state changes."""
        self._shot_counter += 1
        key = (
            snap.url,
            snap.continue_visible,
            snap.advanced_visible,
            snap.unsafe_visible,
            snap.login_form_visible,
            snap.account_identifiers,
        )
        if key == self._last_state_key and self._shot_counter % 10 != 0:
            return
        self._last_state_key = key
        try:
            self._page.screenshot(
                path=str(self._shot_dir / f"step_{self._shot_counter:04d}.png")
            )
            with open(self._shot_log, "a", encoding="utf-8") as fh:
                fh.write(
                    f"{self._shot_counter}\t{snap.url}\tids={','.join(snap.account_identifiers)}"
                    f"\tcontinue={int(snap.continue_visible)}"
                    f"\tadvanced={int(snap.advanced_visible)}"
                    f"\tunsafe={int(snap.unsafe_visible)}"
                    f"\tlogin={int(snap.login_form_visible)}\n"
                )
        except Exception:
            pass

    def click_continue(self) -> None:
        self._safe_click(self._page.get_by_role("button", name=_CONTINUE_NAME).first)

    def click_account(self, identifier: str) -> None:
        self._safe_click(self._page.locator(f'div[data-identifier="{identifier}"]').first)

    def click_advanced(self) -> None:
        self._safe_click(self._page.get_by_text(_ADVANCED_TEXT).first)

    def click_unsafe(self) -> None:
        self._safe_click(self._page.get_by_text(_UNSAFE_TEXT).first)

    @staticmethod
    def _safe_click(locator) -> None:
        """Click without stalling the robot — the next poll re-reads the page."""
        try:
            locator.click(timeout=3_000)
        except Exception:
            pass

    def wait_loaded(self, timeout_ms: int = 10_000) -> None:
        """Wait for the localhost success page to finish loading.

        The redirect request may still be in flight when the URL flips to
        localhost; closing the browser before the page finishes loading would
        abort the request and the local server would never receive the code.
        """
        try:
            self._page.wait_for_load_state("load", timeout=timeout_ms)
        except Exception:
            pass  # best-effort — the response is normally already served

    def close(self) -> None:
        for closer in (lambda: self._context.close(), lambda: self._pw.stop()):
            try:
                closer()
            except Exception:
                pass


# ── Public entry points ───────────────────────────────────────────


def _drive(driver_factory, run_fn, start_msg: str) -> bool:
    """Construct a driver, run one flow, always close the driver.

    Returns False on any error/timeout — the caller falls back further.
    """
    driver = None
    try:
        driver = driver_factory()
        print(start_msg, flush=True)
        return bool(run_fn(driver))
    except ImportError:
        print(
            "[OAuth] 未安装 playwright，自动授权不可用，回退到手动授权。\n"
            "        安装: pip install playwright（无需 playwright install，"
            "直接使用本机 Edge/Chrome）",
            flush=True,
        )
        return False
    except Exception as exc:
        print(f"[OAuth] 自动授权失败: {exc}\n[OAuth] 回退到手动授权…", flush=True)
        return False
    finally:
        if driver is not None:
            driver.close()


def _quarantine(path: Path) -> None:
    """Rename a corrupt recording file aside so record mode can start fresh."""
    try:
        path.replace(path.with_name(path.name + ".corrupt"))
    except OSError:
        pass


def auto_consent(
    authorization_url: str,
    profile_dir: Optional[Path] = None,
    channel: str = "",
    account_email: str = "",
    timeout_seconds: float = 0.0,
    recording_file: Optional[Path] = None,
) -> bool:
    """Complete the consent URL in an automated browser.

    Strategy chain (first success wins):
      1. Replay a previously recorded flow (record-once-replay-always).
      2. Record mode: no recording yet — the human clicks through once while
         their steps are captured for next time.
      3. Built-in robot: hand-coded click rules for the standard flow.

    Returns True when the browser reached the localhost redirect (the
    caller's local server has received the code).  Returns False on any
    error/timeout — the caller should fall back to a manual browser.
    """
    from yt2bili import config

    profile_dir = Path(profile_dir or config.YOUTUBE_OAUTH_BROWSER_PROFILE)
    channel = channel or config.YOUTUBE_OAUTH_BROWSER_CHANNEL
    account_email = account_email or config.YOUTUBE_OAUTH_ACCOUNT_EMAIL
    timeout_seconds = timeout_seconds or config.YOUTUBE_OAUTH_TIMEOUT_SECONDS

    screenshot_dir = None
    if config.YOUTUBE_OAUTH_SCREENSHOTS:
        screenshot_dir = Path(config.YOUTUBE_OAUTH_SCREENSHOT_DIR)

    # ── 1+2: record-replay — the human's own flow drives automation ──
    rec_file = Path(recording_file) if recording_file else None
    if rec_file is not None and config.YOUTUBE_OAUTH_RECORD_ENABLED:
        from yt2bili.youtube import oauth_recorder as recorder

        driver_factory = lambda: recorder.RecordingPlaywrightDriver(
            profile_dir=profile_dir, channel=channel, screenshot_dir=screenshot_dir
        )
        if rec_file.exists():
            try:
                steps = recorder.load_recording(rec_file)
            except Exception as exc:
                print(f"[OAuth] 录制文件损坏，将重新录制 ({exc})", flush=True)
                _quarantine(rec_file)
                steps = []
            if steps:
                if _drive(
                    driver_factory,
                    lambda d: recorder.ReplayRobot(
                        d, steps, timeout_seconds=timeout_seconds
                    ).run(authorization_url),
                    f"[OAuth] 正在自动回放已录制的授权操作 ({len(steps)} 步)…",
                ):
                    return True
                print("[OAuth] 回放未完成，改用内置自动点击…", flush=True)
        if not rec_file.exists():
            if _drive(
                driver_factory,
                lambda d: recorder.RecorderRobot(
                    d, rec_file, timeout_seconds=timeout_seconds
                ).run(authorization_url),
                "[OAuth] 首次授权：请在浏览器中手动完成，操作将被录制并用于下次自动回放…",
            ):
                return True
            print("[OAuth] 录制未完成，改用内置自动点击…", flush=True)

    # ── 3: built-in robot (standard flow, hand-coded rules) ──
    return _drive(
        lambda: PlaywrightDriver(
            profile_dir=profile_dir, channel=channel, screenshot_dir=screenshot_dir
        ),
        lambda d: ConsentRobot(
            d, target_account=account_email, timeout_seconds=timeout_seconds
        ).run(authorization_url),
        "[OAuth] 已打开浏览器，正在自动完成 Google 授权页面点击…",
    )


# ── Single-flight lock (one consent flow across concurrent processes) ──


class _ConsentLockBusy(Exception):
    """The lock file is held by another live process."""


def _pid_alive(pid: int) -> bool:
    """Whether a process with the given PID exists.

    ``os.kill(pid, 0)`` is a POSIX idiom; on Windows it raises a
    SystemError/OSError instead of a meaningful ESRCH, so use the
    OpenProcess API there instead.
    """
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
        )
        if handle:
            kernel32.CloseHandle(handle)
            return True
        # ERROR_ACCESS_DENIED (5): the process exists but is protected.
        return ctypes.get_last_error() == 5
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _acquire_lockfile(lock_path: Path) -> None:
    """Atomically create the lock file; raise ``_ConsentLockBusy`` if held live."""
    try:
        with open(lock_path, "x", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
        return
    except FileExistsError:
        pass
    try:
        pid = int(lock_path.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        pid = 0
    if pid and _pid_alive(pid):
        raise _ConsentLockBusy
    lock_path.unlink(missing_ok=True)  # stale — take over
    _acquire_lockfile(lock_path)


@contextmanager
def oauth_consent_lock(
    token_file: Path, timeout_seconds: float = 0.0, poll_interval: float = 2.0
):
    """Single-flight guard: only one process runs the consent flow at a time.

    Concurrent monitors (e.g. multiple ``--profile`` processes) all need the
    same ``youtube_token.json``.  The lock holder runs the consent flow;
    everyone else polls the token file and skips consent once it appears.

    Yields True when this process should run the flow, False when the token
    file appeared while waiting (another process authorized).
    """
    from yt2bili import config

    if timeout_seconds <= 0:
        timeout_seconds = config.YOUTUBE_OAUTH_TIMEOUT_SECONDS + 60
    lock_path = Path(str(token_file) + ".lock")
    deadline = time.monotonic() + timeout_seconds
    acquired = False
    try:
        while True:
            if token_file.exists():
                yield False
                return
            try:
                _acquire_lockfile(lock_path)
                acquired = True
                yield True
                return
            except _ConsentLockBusy:
                pass
            if time.monotonic() >= deadline:
                print("[OAuth] 等待其他进程授权超时，本进程自行发起授权…", flush=True)
                yield True
                return
            time.sleep(poll_interval)
    finally:
        if acquired:
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass


def _playwright_installed() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


@contextmanager
def auto_consent_browser(enabled: bool = True, recording_file: Optional[Path] = None):
    """Patch ``webbrowser`` so ``run_local_server()`` opens the robot.

    Usage::

        with auto_consent_browser():
            creds = flow.run_local_server(port=0)

    Older google-auth-oauthlib versions call the module-level
    ``webbrowser.open()``; newer ones call ``webbrowser.get().open()``.
    Both entry points are patched.  ``recording_file`` enables
    record-once-replay-always mode (see ``oauth_recorder``).

    The robot runs in a background thread and ``open`` returns
    immediately.  ``run_local_server()`` only starts serving the
    localhost redirect request *after* its ``webbrowser`` call returns —
    a robot that polled the page synchronously would deadlock with it:
    the browser's redirect request waits for the server, while the robot
    waits for the page to load (an endless spinner).  With the robot in a
    thread, the server serves the redirect as soon as the browser sends
    it and the robot merely watches the flow complete.  If the robot
    fails or times out, the original ``webbrowser.open`` fires so a human
    can still click through the consent page manually.
    """
    import threading
    import webbrowser

    original_open = webbrowser.open
    original_get = webbrowser.get

    def _start_robot(url: str) -> bool:
        """Launch the robot in the background; returns immediately."""

        def _work() -> None:
            try:
                ok = auto_consent(url, recording_file=recording_file)
            except Exception:
                ok = False
            if not ok:
                # Robot unavailable/failed — open the manual browser so the
                # still-blocked handle_request() can receive the redirect.
                try:
                    original_open(url)
                except Exception:
                    pass

        threading.Thread(target=_work, daemon=True).start()
        return True

    class _RobotBrowser:
        """Proxy returned for ``webbrowser.get()`` — starts the robot instead."""

        def __init__(self, inner):
            self._inner = inner

        def open(self, url, *args, **kwargs):
            return _start_robot(url)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    if enabled and _playwright_installed():

        def _robot_get(name=None, *args, **kwargs):
            return _RobotBrowser(original_get(name, *args, **kwargs))

        webbrowser.get = _robot_get
        webbrowser.open = _start_robot
    elif enabled:
        print(
            "[OAuth] 未安装 playwright（pip install playwright），本次授权需手动点击浏览器。"
        )

    try:
        yield
    finally:
        webbrowser.open = original_open
        webbrowser.get = original_get
