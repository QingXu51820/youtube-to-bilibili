"""
Record-and-replay for the Google OAuth consent flow.

The first authorization runs in *record mode*: a browser window opens, the
human clicks through Google's pages manually (login, account chooser,
consent), and every click/keystroke is captured by an injected script.  The
recording is saved next to the token file (``youtube_token.recording.json``).

Later authorizations run in *replay mode*: the same window opens again and
the recorded actions are executed automatically — no human needed, even on
screens the hand-coded robot doesn't know about (extra consent pages,
security checkpoints, regional variations).  A stale or changed flow simply
times out and falls back to the built-in robot / manual browser.

Everything below the driver line is pure logic driven through a minimal
``RecorderDriver`` protocol, so it can be unit-tested with a fake driver
(see ``tests/test_oauth_recorder.py``) — matching the repo's no-network
test policy.  Password input values are never recorded.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional, Protocol, Tuple

from yt2bili.youtube.oauth_consent import PageSnapshot, PlaywrightDriver

MAX_RECORDED_STEPS = 60
STEP_KINDS = ("click", "press_enter", "fill_text")

# Injected into every page: capture-phase listeners that serialize user
# interactions into a buffer drained by the Python side via drain_steps().
# The buffer survives same-page DOM churn; each navigation re-injects it
# (add_init_script runs on every new document).
_RECORDER_JS = """
(() => {
  if (window.__yt2bili_oauth_rec) return;
  const buf = [];
  window.__yt2bili_oauth_rec = buf;
  const describe = (el) => {
    if (!el || typeof el.tagName !== "string") return null;
    let holder = el;
    while (holder && holder.getAttribute && !holder.getAttribute("data-identifier")) {
      holder = holder.parentElement;
    }
    const text = ((el.innerText || el.textContent) || "")
      .replace(/\\s+/g, " ").trim().slice(0, 80);
    return {
      tag: el.tagName,
      role: el.getAttribute("role") || "",
      id: el.id || "",
      name: el.getAttribute("name") || "",
      aria: el.getAttribute("aria-label") || "",
      type: el.getAttribute("type") || "",
      ident: holder && holder.getAttribute
        ? (holder.getAttribute("data-identifier") || "")
        : "",
      text: text,
    };
  };
  const push = (kind, el, extra) => {
    const d = describe(el);
    if (!d) return;
    if (buf.length >= 200) buf.shift();
    buf.push(Object.assign(
      { kind, url: location.href, vw: window.innerWidth, vh: window.innerHeight },
      d, extra
    ));
  };
  document.addEventListener("click", (e) => {
    const el = e.target instanceof Element
      ? (e.target.closest(
          "a,button,[role=button],[role=link],[role=option],[role=checkbox],input,label,div[data-identifier]"
        ) || e.target)
      : null;
    if (!el) return;
    push("click", el, { x: Math.round(e.clientX), y: Math.round(e.clientY) });
  }, true);
  document.addEventListener("change", (e) => {
    const el = e.target;
    if (!el || el.tagName !== "INPUT" || el.type === "password") return;
    const value = (el.value || "").slice(0, 200);
    if (!value) return;
    push("fill_text", el, { value, x: -1, y: -1 });
  }, true);
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    const el = e.target;
    if (!el || (el.tagName !== "INPUT" && el.tagName !== "TEXTAREA")) return;
    push("press_enter", el, { x: -1, y: -1 });
  }, true);
})();
"""


# ── Pure data & logic (no I/O, unit-testable) ─────────────────────


@dataclass
class RecordedStep:
    """One recorded user action (pure data, easy to fake in tests)."""

    seq: int = 0
    kind: str = "click"  # click | press_enter | fill_text
    url: str = ""
    tag: str = ""
    role: str = ""
    identifier: str = ""
    aria_label: str = ""
    id: str = ""
    name: str = ""
    input_type: str = ""
    text: str = ""
    value: str = ""
    x: float = -1.0
    y: float = -1.0
    viewport_w: int = 0
    viewport_h: int = 0

    @classmethod
    def from_event(cls, event: dict, seq: int) -> "RecordedStep":
        """Build a step from a raw recorder event (missing keys tolerated)."""
        return cls(
            seq=seq,
            kind=event.get("kind") or "click",
            url=event.get("url") or "",
            tag=event.get("tag") or "",
            role=event.get("role") or "",
            identifier=event.get("ident") or "",
            aria_label=event.get("aria") or "",
            id=event.get("id") or "",
            name=event.get("name") or "",
            input_type=event.get("type") or "",
            text=event.get("text") or "",
            value=event.get("value") or "",
            x=_num(event, "x", -1.0),
            y=_num(event, "y", -1.0),
            viewport_w=int(_num(event, "vw", 0)),
            viewport_h=int(_num(event, "vh", 0)),
        )

    def has_target(self) -> bool:
        """Whether the step carries anything usable to find the element."""
        return bool(
            self.identifier
            or (self.role and self.text)
            or self.text
            or self.aria_label
            or self.id
            or self.name
            or (self.x >= 0 and self.y >= 0)
        )

    def dedupe_key(self) -> tuple:
        """Identity for collapsing consecutive duplicate events."""
        return (
            self.kind,
            _url_key(self.url),
            self.identifier,
            self.role,
            self.text,
            self.aria_label,
            self.id,
            self.name,
            self.input_type,
            self.value,
            round(self.x),
            round(self.y),
        )


def _num(event: dict, key: str, default: float) -> float:
    """Coerce an event field to float; missing/empty/garbage → default."""
    raw = event.get(key)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _url_key(url: str) -> str:
    """Host + path identity, ignoring query params (they vary per flow)."""
    try:
        from urllib.parse import urlsplit

        parts = urlsplit(url)
    except ValueError:
        return ""
    return f"{parts.scheme}://{parts.netloc.lower()}{parts.path.rstrip('/')}"


def url_matches(current: str, recorded: str) -> bool:
    """Whether the current page is the page the step was recorded on."""
    if not current or not recorded:
        return False
    return _url_key(current) == _url_key(recorded)


def css_quote(value: str) -> str:
    """Escape a value for use inside a double-quoted CSS attribute selector."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def click_position(
    step: RecordedStep, viewport: Tuple[int, int]
) -> Optional[Tuple[float, float]]:
    """Map recorded coordinates onto the current viewport, proportionally."""
    if step.x < 0 or step.y < 0:
        return None
    vw, vh = viewport
    if step.viewport_w <= 0 or step.viewport_h <= 0 or vw <= 0 or vh <= 0:
        return None
    x = step.x * vw / step.viewport_w
    y = step.y * vh / step.viewport_h
    if not (0 <= x <= vw and 0 <= y <= vh):
        return None
    return (x, y)


def locator_candidates(step: RecordedStep) -> list:
    """Ordered (strategy, value) pairs to find the recorded element again.

    Most stable first (data-identifier survives DOM churn), raw coordinates
    last (layout-dependent).  Text-based strategies are skipped for
    fill_text — an input's text is its value, not its content.
    """
    candidates: list = []
    if step.identifier:
        candidates.append(("identifier", step.identifier))
    if step.kind in ("click", "press_enter"):
        if step.role and step.text:
            candidates.append(("role_text", (step.role, step.text)))
        if step.text:
            candidates.append(("text_exact", step.text))
    if step.aria_label:
        candidates.append(("aria_label", step.aria_label))
    if step.id:
        candidates.append(("id", step.id))
    if step.name:
        candidates.append(("name", step.name))
    if step.kind in ("click", "press_enter"):
        if step.text:
            candidates.append(("text", step.text))
    else:  # fill_text
        if step.input_type:
            candidates.append(("input_type", step.input_type))
    if step.x >= 0 and step.y >= 0:
        candidates.append(("coords", (step.x, step.y)))
    return candidates


def plan_steps(
    steps: list[RecordedStep], max_steps: int = MAX_RECORDED_STEPS
) -> list[RecordedStep]:
    """Normalize a raw recording: drop useless steps, collapse consecutive
    duplicates, cap the length, and re-number the survivors."""
    kept: list[RecordedStep] = []
    last_key = None
    for step in steps:
        if step.kind not in STEP_KINDS or not step.has_target():
            continue
        key = step.dedupe_key()
        if key == last_key:
            continue
        kept.append(step)
        last_key = key
        if len(kept) >= max_steps:
            break
    return [replace(step, seq=i + 1) for i, step in enumerate(kept)]


# Short JSON keys keep the recording file small and match the raw
# event shape produced by the injected script.
_JSON_FIELDS = (
    ("seq", "seq"),
    ("kind", "kind"),
    ("url", "url"),
    ("tag", "tag"),
    ("role", "role"),
    ("identifier", "ident"),
    ("aria_label", "aria"),
    ("id", "id"),
    ("name", "name"),
    ("input_type", "type"),
    ("text", "text"),
    ("value", "value"),
    ("x", "x"),
    ("y", "y"),
    ("viewport_w", "vw"),
    ("viewport_h", "vh"),
)


def steps_to_json(steps: list[RecordedStep]) -> str:
    payload = [
        {short: getattr(step, field) for field, short in _JSON_FIELDS}
        for step in steps
    ]
    return json.dumps({"version": 1, "steps": payload}, ensure_ascii=False, indent=2)


def events_to_steps(events: list[dict]) -> list[RecordedStep]:
    """Convert raw recorder events (dicts) into steps, tolerating bad ones."""
    steps = []
    for i, event in enumerate(events):
        try:
            steps.append(RecordedStep.from_event(event, i + 1))
        except Exception:
            continue
    return steps


def steps_from_json(text: str) -> list[RecordedStep]:
    """Parse a recording file.  Raises ValueError on malformed content."""
    data = json.loads(text)
    if isinstance(data, dict):
        raw = data.get("steps")
        if not isinstance(raw, list):
            raise ValueError("recording payload has no 'steps' list")
    elif isinstance(data, list):
        raw = data
    else:
        raise ValueError("recording payload must be an object or list")
    return events_to_steps(raw)


def save_recording(path: Path, steps: list[RecordedStep]) -> None:
    """Atomically write the recording (temp file + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(steps_to_json(steps), encoding="utf-8")
    os.replace(tmp, path)


def load_recording(path: Path) -> list[RecordedStep]:
    """Read a recording file; raises ValueError/OSError on corrupt content."""
    return steps_from_json(path.read_text(encoding="utf-8"))


def _is_localhost(url: str) -> bool:
    return url.startswith(("http://localhost:", "http://127.0.0.1:"))


def _actionable(snap: PageSnapshot) -> bool:
    """The page shows consent elements the recording doesn't cover."""
    return bool(
        snap.account_identifiers
        or snap.continue_visible
        or snap.advanced_visible
        or snap.unsafe_visible
    )


# ── Driver interface ──────────────────────────────────────────────


class RecorderDriver(Protocol):
    """Minimal browser abstraction for record/replay (Playwright impl below)."""

    def goto(self, url: str) -> None: ...

    def snapshot(self) -> PageSnapshot: ...

    def wait_loaded(self, timeout_ms: int = 10_000) -> None: ...

    def close(self) -> None: ...

    def enable_recording(self) -> None: ...

    def drain_steps(self) -> list[dict]: ...

    def execute_step(self, step: RecordedStep) -> bool: ...


# ── Robots ────────────────────────────────────────────────────────


class RecorderRobot:
    """Passive watcher: the human drives, the robot records, the token lands."""

    def __init__(
        self,
        driver: RecorderDriver,
        recording_file: Path,
        timeout_seconds: float = 600.0,
        poll_interval: float = 0.3,
    ):
        self._driver = driver
        self._recording_file = recording_file
        self._timeout_seconds = timeout_seconds
        self._poll_interval = poll_interval

    def run(self, authorization_url: str) -> bool:
        """Watch the human complete the flow, then save the recording.

        Returns True once the page reached the localhost redirect (the code
        was delivered and the recording saved), False on timeout — in which
        case nothing is written.
        """
        self._driver.enable_recording()
        self._driver.goto(authorization_url)
        print(
            "[OAuth] 已打开浏览器，请手动完成 Google 授权（登录/选择账号/同意）。\n"
            f"        你的操作将被录制到 {self._recording_file}，"
            "之后 token 失效时会自动回放，无需再次操作。",
            flush=True,
        )
        events: list[dict] = []
        deadline = time.monotonic() + self._timeout_seconds
        while time.monotonic() < deadline:
            events.extend(self._driver.drain_steps())
            snap = self._driver.snapshot()
            if _is_localhost(snap.url):
                self._driver.wait_loaded()
                time.sleep(0.5)  # grace for the response to fully land
                steps = plan_steps(events_to_steps(events))
                if steps:
                    save_recording(self._recording_file, steps)
                    print(
                        f"[OAuth] 已录制 {len(steps)} 步操作，"
                        f"保存到 {self._recording_file}",
                        flush=True,
                    )
                else:
                    # An empty recording would block record mode forever —
                    # leave the file unwritten so the next run records again.
                    print(
                        "[OAuth] 未捕获到任何操作，不保存录制文件"
                        "（下次仍会进入录制模式）。",
                        flush=True,
                    )
                return True
            time.sleep(self._poll_interval)
        return False


class ReplayRobot:
    """Executes recorded steps until the localhost redirect fires."""

    def __init__(
        self,
        driver: RecorderDriver,
        steps: list[RecordedStep],
        timeout_seconds: float = 600.0,
        poll_interval: float = 0.5,
        step_timeout: float = 15.0,
        grace_seconds: float = 60.0,
        handover_seconds: float = 5.0,
    ):
        self._driver = driver
        self._steps = list(steps)
        self._timeout_seconds = timeout_seconds
        self._poll_interval = poll_interval
        self._step_timeout = step_timeout
        self._grace_seconds = grace_seconds
        self._handover_seconds = handover_seconds

    def run(self, authorization_url: str) -> bool:
        """Replay the recording, skipping steps whose element never appears.

        Returns True once the page reached the localhost redirect, False on
        timeout.  Each step is only attempted while the page matches the URL
        it was recorded on, so the replay cannot click into a different flow.
        Once every recorded step is done, the robot waits for the redirect:
        if the page shows consent elements the recording doesn't cover
        (Continue/Advanced/account chooser) for ``handover_seconds``, it
        gives up immediately so the caller's fallback chain takes over
        (built-in robot → manual browser); otherwise it waits
        ``grace_seconds`` in total before giving up.
        """
        self._driver.goto(authorization_url)
        print(f"[OAuth] 回放 {len(self._steps)} 步已录制的授权操作…", flush=True)
        deadline = time.monotonic() + self._timeout_seconds
        index = 0
        step_deadline = time.monotonic() + self._step_timeout
        exhausted_at = None  # when the last step executed/skipped
        while time.monotonic() < deadline:
            snap = self._driver.snapshot()
            if _is_localhost(snap.url):
                self._driver.wait_loaded()
                time.sleep(0.5)
                return True
            if index < len(self._steps):
                step = self._steps[index]
                if url_matches(snap.url, step.url) and self._driver.execute_step(step):
                    index += 1
                    step_deadline = time.monotonic() + self._step_timeout
                elif time.monotonic() >= step_deadline:
                    index += 1  # element never appeared — skip and move on
                    step_deadline = time.monotonic() + self._step_timeout
            elif exhausted_at is None:
                exhausted_at = time.monotonic()
            else:
                waited = time.monotonic() - exhausted_at
                if waited >= self._grace_seconds:
                    return False
                if waited >= self._handover_seconds and _actionable(snap):
                    return False
            time.sleep(self._poll_interval)
        return False


# ── Playwright driver ─────────────────────────────────────────────


class RecordingPlaywrightDriver(PlaywrightDriver):
    """PlaywrightDriver extended with event capture and step execution."""

    def enable_recording(self) -> None:
        self._context.add_init_script(_RECORDER_JS)

    def drain_steps(self) -> list[dict]:
        try:
            return self._page.evaluate(
                "() => (window.__yt2bili_oauth_rec || []).splice(0)"
            )
        except Exception:
            return []  # navigation in flight — next poll picks the events up

    def execute_step(self, step: RecordedStep) -> bool:
        page = self._page
        for strategy, value in locator_candidates(step):
            try:
                if strategy == "coords":
                    pos = click_position(step, self._viewport_size())
                    if pos is None:
                        continue
                    return self._act_at_coords(step, pos)
                locator = self._locator_for(strategy, value)
                if locator.count() < 1 or not locator.first.is_visible():
                    continue
                element = locator.first
                if step.kind == "click":
                    element.click(timeout=3_000)
                    return True
                if step.kind == "press_enter":
                    element.press("Enter", timeout=3_000)
                    return True
                element.fill(step.value, timeout=3_000)
                return True
            except Exception:
                continue  # candidate didn't work — try the next one
        return False

    def _act_at_coords(self, step: RecordedStep, pos: Tuple[float, float]) -> bool:
        """Last-resort replay at raw coordinates (proportional to viewport)."""
        x, y = pos
        self._page.mouse.click(x, y)
        if step.kind == "press_enter":
            self._page.keyboard.press("Enter")
        elif step.kind == "fill_text":
            self._page.keyboard.insert_text(step.value)
        return True

    def _locator_for(self, strategy: str, value):
        page = self._page
        if strategy == "identifier":
            return page.locator(f'div[data-identifier="{css_quote(value)}"]')
        if strategy == "role_text":
            role, text = value
            return page.get_by_role(role, name=text)
        if strategy == "text_exact":
            return page.get_by_text(value, exact=True)
        if strategy == "text":
            return page.get_by_text(value)
        if strategy == "aria_label":
            return page.get_by_label(value)
        if strategy == "id":
            return page.locator(f"#{css_quote(value)}")
        if strategy == "name":
            return page.locator(f'[name="{css_quote(value)}"]')
        if strategy == "input_type":
            return page.locator(f'input[type="{css_quote(value)}"]')
        raise ValueError(f"unknown locator strategy: {strategy}")

    def _viewport_size(self) -> Tuple[int, int]:
        try:
            return tuple(
                self._page.evaluate("() => [window.innerWidth, window.innerHeight]")
            )
        except Exception:
            return (0, 0)
