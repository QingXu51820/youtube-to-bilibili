"""Pure-logic tests for the OAuth record-replay recorder (no browser, no network)."""

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from yt2bili import config
from yt2bili.youtube import oauth_consent as oc
from yt2bili.youtube import oauth_recorder as rec


def S(**kwargs) -> oc.PageSnapshot:
    return oc.PageSnapshot(**kwargs)


def ev(kind="click", url="https://accounts.google.com/consent", **kwargs) -> dict:
    """A raw recorder event with sensible defaults for a Continue button."""
    base = dict(
        kind=kind,
        url=url,
        tag="button",
        role="button",
        id="",
        name="",
        aria="",
        type="",
        ident="",
        text="Continue",
        value="",
        x=10,
        y=20,
        vw=1280,
        vh=720,
    )
    base.update(kwargs)
    return base


class FakeRecorderDriver:
    """Scripted RecorderDriver double: queued scenes, drained events, click log."""

    def __init__(self, scenes, batches=None, step_results=None):
        self._scenes = list(scenes)
        self._last = self._scenes[-1] if self._scenes else S()
        self._batches = list(batches or [])
        self._step_results = step_results or {}
        self.closed = False
        self.goto_url = None
        self.recording_enabled = False
        self.drained = []
        self.executed = []  # (seq, kind) of every execute_step call

    def goto(self, url):
        self.goto_url = url

    def snapshot(self):
        if self._scenes:
            self._last = self._scenes.pop(0)
        return self._last

    def wait_loaded(self, timeout_ms=10_000):
        self.executed.append(("wait_loaded", None))

    def close(self):
        self.closed = True

    def enable_recording(self):
        self.recording_enabled = True

    def drain_steps(self):
        events = self._batches.pop(0) if self._batches else []
        self.drained.extend(events)
        return events

    def execute_step(self, step):
        self.executed.append((step.seq, step.kind))
        results = self._step_results.get(step.seq, True)
        if isinstance(results, list):
            return results.pop(0) if results else True
        return results


class FakeRobotDriver:
    """Scripted ConsentDriver double for the built-in robot fallback."""

    def __init__(self, scenes):
        self._scenes = list(scenes)
        self._last = self._scenes[-1] if self._scenes else S()
        self.closed = False
        self.clicks = []

    def goto(self, url):
        self.goto_url = url

    def snapshot(self):
        if self._scenes:
            self._last = self._scenes.pop(0)
        return self._last

    def click_continue(self):
        self.clicks.append("continue")

    def click_account(self, identifier):
        self.clicks.append(("account", identifier))

    def click_advanced(self):
        self.clicks.append("advanced")

    def click_unsafe(self):
        self.clicks.append("unsafe")

    def wait_loaded(self, timeout_ms=10_000):
        self.clicks.append("wait_loaded")

    def close(self):
        self.closed = True


CONSENT = "https://accounts.google.com/signin/oauth/consent"
LOCALHOST = "http://localhost:8322/?code=xyz"


class SerializationTests(unittest.TestCase):
    """JSON round-trips and malformed input."""

    def test_roundtrip_preserves_all_fields(self):
        steps = [
            rec.RecordedStep(
                seq=1,
                kind="fill_text",
                url="https://accounts.google.com/x?flow=1",
                tag="input",
                role="",
                identifier="me@gmail.com",
                aria_label="邮箱",
                id="identifierId",
                name="identifier",
                input_type="email",
                text="",
                value="me@gmail.com",
                x=0,
                y=0,
                viewport_w=1280,
                viewport_h=720,
            ),
            rec.RecordedStep(seq=2),
        ]
        self.assertEqual(rec.steps_from_json(rec.steps_to_json(steps)), steps)

    def test_non_json_raises_value_error(self):
        with self.assertRaises(ValueError):
            rec.steps_from_json("not json{{")

    def test_payload_without_steps_list_raises(self):
        with self.assertRaises(ValueError):
            rec.steps_from_json('{"version": 1, "steps": "nope"}')

    def test_missing_fields_default_safely(self):
        steps = rec.steps_from_json('{"steps": [{"kind": "click"}]}')
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0].kind, "click")
        self.assertFalse(steps[0].has_target())

    def test_events_to_steps_tolerates_bad_events(self):
        events = [ev(text="Continue"), "garbage", ev(text="Advanced")]
        steps = rec.events_to_steps(events)
        self.assertEqual([s.text for s in steps], ["Continue", "Advanced"])

    def test_zero_coordinates_survive_parsing(self):
        steps = rec.steps_from_json(
            '{"steps": [{"kind": "click", "x": 0, "y": 0, "vw": 1280, "vh": 720}]}'
        )
        self.assertEqual((steps[0].x, steps[0].y), (0.0, 0.0))
        self.assertTrue(steps[0].has_target())


class PlanStepsTests(unittest.TestCase):
    """Recording normalization."""

    def _step(self, **kwargs):
        return rec.RecordedStep(seq=1, **kwargs)

    def test_drops_unknown_kind(self):
        self.assertEqual(
            rec.plan_steps([self._step(kind="bogus", text="Continue")]), []
        )

    def test_drops_step_without_any_target(self):
        self.assertEqual(rec.plan_steps([self._step()]), [])

    def test_keeps_step_with_only_coords(self):
        steps = rec.plan_steps([self._step(x=10, y=20)])
        self.assertEqual(len(steps), 1)

    def test_collapses_consecutive_duplicates(self):
        a = self._step(kind="click", text="Continue", x=1, y=2)
        self.assertEqual(rec.plan_steps([a, a, a]), [a])

    def test_keeps_non_consecutive_duplicates(self):
        a = self._step(kind="click", text="Continue")
        b = self._step(kind="click", text="Advanced")
        planned = rec.plan_steps([a, b, a])
        self.assertEqual([s.text for s in planned], ["Continue", "Advanced", "Continue"])
        self.assertEqual([s.seq for s in planned], [1, 2, 3])

    def test_caps_length_and_resequences(self):
        steps = [
            self._step(kind="click", text=f"btn{i}", x=i, y=0) for i in range(10)
        ]
        planned = rec.plan_steps(steps, max_steps=3)
        self.assertEqual(len(planned), 3)
        self.assertEqual([s.seq for s in planned], [1, 2, 3])
        self.assertEqual(planned[2].text, "btn2")


class LocatorCandidatesTests(unittest.TestCase):
    """Candidate priority per step kind."""

    def test_click_orders_stable_first_coords_last(self):
        step = rec.RecordedStep(
            kind="click",
            identifier="a@b.c",
            role="button",
            text="Continue",
            aria_label="go",
            id="btn",
            name="sub",
            x=5,
            y=6,
        )
        self.assertEqual(
            [c[0] for c in rec.locator_candidates(step)],
            ["identifier", "role_text", "text_exact", "aria_label", "id", "name",
             "text", "coords"],
        )

    def test_press_enter_uses_text_and_coords(self):
        step = rec.RecordedStep(kind="press_enter", text="OK", x=3, y=4)
        self.assertEqual(
            [c[0] for c in rec.locator_candidates(step)],
            ["text_exact", "text", "coords"],
        )

    def test_fill_text_skips_text_based_strategies(self):
        step = rec.RecordedStep(
            kind="fill_text",
            role="textbox",
            text="me@gmail.com",
            name="identifier",
            input_type="email",
            x=1,
            y=2,
        )
        self.assertEqual(
            [c[0] for c in rec.locator_candidates(step)],
            ["name", "input_type", "coords"],
        )

    def test_no_coords_when_unrecorded(self):
        step = rec.RecordedStep(kind="click", text="Continue")
        self.assertNotIn("coords", [c[0] for c in rec.locator_candidates(step)])

    def test_empty_step_has_no_candidates(self):
        self.assertEqual(rec.locator_candidates(rec.RecordedStep()), [])


class UrlMatchTests(unittest.TestCase):
    """Page identity for gating steps."""

    def test_query_params_ignored(self):
        self.assertTrue(
            rec.url_matches("https://accounts.google.com/x?a=1", "https://accounts.google.com/x?b=2")
        )

    def test_different_host(self):
        self.assertFalse(
            rec.url_matches("https://myaccount.google.com/x", "https://accounts.google.com/x")
        )

    def test_different_path(self):
        self.assertFalse(
            rec.url_matches("https://accounts.google.com/x", "https://accounts.google.com/y")
        )

    def test_trailing_slash_ignored(self):
        self.assertTrue(
            rec.url_matches("https://accounts.google.com/x/", "https://accounts.google.com/x")
        )

    def test_empty_recorded_url_never_matches(self):
        self.assertFalse(rec.url_matches("https://accounts.google.com/x", ""))


class ClickPositionTests(unittest.TestCase):
    """Proportional coordinate mapping."""

    def _step(self, x, y, vw=1280, vh=720):
        return rec.RecordedStep(x=x, y=y, viewport_w=vw, viewport_h=vh)

    def test_maps_proportionally_to_new_viewport(self):
        self.assertEqual(rec.click_position(self._step(640, 360), (1920, 1080)), (960.0, 540.0))

    def test_unrecorded_coords_are_none(self):
        self.assertIsNone(rec.click_position(self._step(-1, -1), (1920, 1080)))

    def test_zero_viewport_is_none(self):
        self.assertIsNone(rec.click_position(self._step(10, 10), (0, 0)))

    def test_out_of_range_after_mapping_is_none(self):
        self.assertIsNone(rec.click_position(self._step(2000, 500), (1280, 720)))


class ReplayRobotTests(unittest.TestCase):
    """Tolerant step execution until the localhost redirect fires."""

    def _step(self, seq, url=CONSENT, kind="click", **kwargs):
        return rec.RecordedStep(seq=seq, kind=kind, url=url, text="Continue", **kwargs)

    def test_walks_steps_in_order(self):
        steps = [self._step(1), self._step(2, url="https://accounts.google.com/approve")]
        driver = FakeRecorderDriver(
            [
                S(url=CONSENT),
                S(url="https://accounts.google.com/approve"),
                S(url=LOCALHOST),
            ]
        )
        robot = rec.ReplayRobot(driver, steps, poll_interval=0)
        self.assertTrue(robot.run("https://auth.example/start"))
        self.assertEqual(driver.executed, [(1, "click"), (2, "click"), ("wait_loaded", None)])

    def test_retries_failed_step_until_it_succeeds(self):
        driver = FakeRecorderDriver(
            [S(url=CONSENT), S(url=CONSENT), S(url=LOCALHOST)],
            step_results={1: [False, True]},
        )
        robot = rec.ReplayRobot(driver, [self._step(1)], poll_interval=0)
        self.assertTrue(robot.run("https://auth.example/start"))
        self.assertEqual(driver.executed, [(1, "click"), (1, "click"), ("wait_loaded", None)])

    def test_skips_step_whose_page_never_appears(self):
        driver = FakeRecorderDriver(
            [S(url=CONSENT), S(url=CONSENT), S(url=LOCALHOST)]
        )
        robot = rec.ReplayRobot(
            driver,
            [self._step(1, url="https://accounts.google.com/somewhere/else")],
            poll_interval=0.01,
            step_timeout=0.01,
        )
        self.assertTrue(robot.run("https://auth.example/start"))
        self.assertEqual(driver.executed, [("wait_loaded", None)])

    def test_no_steps_just_waits_for_redirect(self):
        driver = FakeRecorderDriver([S(url=CONSENT), S(url=LOCALHOST)])
        robot = rec.ReplayRobot(driver, [], poll_interval=0)
        self.assertTrue(robot.run("https://auth.example/start"))
        self.assertEqual(driver.executed, [("wait_loaded", None)])

    def test_times_out_when_redirect_never_fires(self):
        driver = FakeRecorderDriver([S(url=CONSENT)])
        robot = rec.ReplayRobot(
            driver, [self._step(1)], timeout_seconds=0.05, poll_interval=0.01
        )
        self.assertFalse(robot.run("https://auth.example/start"))

    def test_bails_out_after_grace_when_flow_needs_more_than_recorded(self):
        # Steps all done but the redirect never comes: the flow shows screens
        # the recording doesn't cover — give up after the grace period so the
        # fallback chain takes over instead of stalling the consent timeout.
        driver = FakeRecorderDriver([S(url=CONSENT)])
        robot = rec.ReplayRobot(
            driver,
            [self._step(1)],
            timeout_seconds=5,
            poll_interval=0.01,
            grace_seconds=0.05,
        )
        self.assertFalse(robot.run("https://auth.example/start"))

    def test_grace_does_not_cut_off_successful_redirect(self):
        # Exhausted steps + a slow (but arriving) redirect must still succeed.
        driver = FakeRecorderDriver(
            [S(url=CONSENT), S(url=CONSENT), S(url=LOCALHOST)]
        )
        robot = rec.ReplayRobot(
            driver,
            [self._step(1)],
            poll_interval=0.01,
            grace_seconds=0.05,
        )
        self.assertTrue(robot.run("https://auth.example/start"))

    def test_hands_over_quickly_when_consent_page_appears(self):
        # Recorded steps are done but the flow shows a Continue button the
        # recording doesn't cover — hand over after the short delay, not
        # after the whole grace period.
        driver = FakeRecorderDriver(
            [
                S(url=CONSENT),
                S(url="https://accounts.google.com/consent", continue_visible=True),
            ]
        )
        robot = rec.ReplayRobot(
            driver,
            [self._step(1)],
            timeout_seconds=5,
            poll_interval=0.01,
            grace_seconds=5,
            handover_seconds=0.05,
        )
        self.assertFalse(robot.run("https://auth.example/start"))

    def test_handover_does_not_preempt_a_successful_redirect(self):
        # Right after the last step the old page can still look actionable;
        # a redirect arriving within the handover window must win.
        driver = FakeRecorderDriver(
            [
                S(url=CONSENT),
                S(url=CONSENT, continue_visible=True),
                S(url=LOCALHOST),
            ]
        )
        robot = rec.ReplayRobot(
            driver,
            [self._step(1)],
            poll_interval=0.01,
            grace_seconds=5,
            handover_seconds=0.05,
        )
        self.assertTrue(robot.run("https://auth.example/start"))


class RecorderRobotTests(unittest.TestCase):
    """Passive capture of the human's manual flow."""

    def test_captures_events_and_saves_recording(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        rec_file = Path(tmp.name) / "youtube_token.recording.json"
        driver = FakeRecorderDriver(
            [S(url=CONSENT), S(url=LOCALHOST)],
            batches=[
                [ev(text="Continue", x=5, y=5)],
                [ev(kind="press_enter", text="", id="identifierId", x=-1, y=-1)],
            ],
        )
        robot = rec.RecorderRobot(driver, rec_file, poll_interval=0)
        self.assertTrue(robot.run("https://auth.example/start"))
        self.assertTrue(driver.recording_enabled)
        self.assertEqual(driver.goto_url, "https://auth.example/start")
        steps = rec.load_recording(rec_file)
        self.assertEqual([s.kind for s in steps], ["click", "press_enter"])

    def test_prints_manual_hint(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        driver = FakeRecorderDriver([S(url=LOCALHOST)])
        buf = io.StringIO()
        with redirect_stdout(buf):
            rec.RecorderRobot(driver, Path(tmp.name) / "r.json", poll_interval=0).run(
                "https://auth.example/start"
            )
        self.assertIn("手动完成", buf.getvalue())

    def test_timeout_writes_nothing(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        rec_file = Path(tmp.name) / "r.json"
        driver = FakeRecorderDriver([S(url=CONSENT)])
        robot = rec.RecorderRobot(
            driver, rec_file, timeout_seconds=0.05, poll_interval=0.01
        )
        self.assertFalse(robot.run("https://auth.example/start"))
        self.assertFalse(rec_file.exists())

    def test_no_events_captured_leaves_no_recording(self):
        # Success without any captured steps must not write an empty file,
        # otherwise the next run would skip record mode forever.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        rec_file = Path(tmp.name) / "r.json"
        driver = FakeRecorderDriver([S(url=LOCALHOST)])
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertTrue(
                rec.RecorderRobot(driver, rec_file, poll_interval=0).run(
                    "https://auth.example/start"
                )
            )
        self.assertFalse(rec_file.exists())
        self.assertIn("未捕获到任何操作", buf.getvalue())


class AutoConsentOrchestrationTests(unittest.TestCase):
    """auto_consent() strategy chain: replay → record → robot."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.rec_file = Path(self._tmp.name) / "youtube_token.recording.json"
        self.robot_fake = FakeRobotDriver([S(url=LOCALHOST)])

    def _patch(self, recorder_driver=None):
        """Patch the recorder driver + robot driver for one auto_consent call."""
        return (
            patch.object(rec, "RecordingPlaywrightDriver", return_value=recorder_driver),
            patch.object(oc, "PlaywrightDriver", return_value=self.robot_fake),
            patch.object(config, "YOUTUBE_OAUTH_RECORD_ENABLED", True),
        )

    def test_replay_success_skips_robot(self):
        rec.save_recording(
            self.rec_file,
            [rec.RecordedStep(seq=1, kind="click", url=CONSENT, text="Continue", x=5, y=5)],
        )
        fake = FakeRecorderDriver([S(url=CONSENT), S(url=LOCALHOST)])
        p1, p2, p3 = self._patch(fake)
        with p1, p2 as robot_cls, p3:
            self.assertTrue(
                oc.auto_consent("https://auth.example/start", recording_file=self.rec_file)
            )
        robot_cls.assert_not_called()
        self.assertTrue(fake.closed)

    def test_replay_failure_falls_back_to_robot(self):
        rec.save_recording(
            self.rec_file,
            [rec.RecordedStep(seq=1, kind="click", url=CONSENT, text="Continue", x=5, y=5)],
        )
        fake = FakeRecorderDriver([S(url=CONSENT)], step_results={1: False})
        buf = io.StringIO()
        p1, p2, p3 = self._patch(fake)
        with p1, p2 as robot_cls, p3, redirect_stdout(buf):
            self.assertTrue(
                oc.auto_consent(
                    "https://auth.example/start",
                    recording_file=self.rec_file,
                    timeout_seconds=0.1,
                )
            )
        robot_cls.assert_called_once()
        self.assertIn("回放未完成", buf.getvalue())

    def test_no_recording_records_the_manual_flow(self):
        fake = FakeRecorderDriver(
            [S(url=CONSENT), S(url=LOCALHOST)],
            batches=[[ev(text="Continue", x=5, y=5)]],
        )
        p1, p2, p3 = self._patch(fake)
        with p1, p2 as robot_cls, p3:
            self.assertTrue(
                oc.auto_consent(
                    "https://auth.example/start",
                    recording_file=self.rec_file,
                    timeout_seconds=5,
                )
            )
        robot_cls.assert_not_called()
        self.assertEqual(len(rec.load_recording(self.rec_file)), 1)

    def test_record_failure_falls_back_to_robot(self):
        fake = FakeRecorderDriver([S(url=CONSENT)])
        buf = io.StringIO()
        p1, p2, p3 = self._patch(fake)
        with p1, p2 as robot_cls, p3, redirect_stdout(buf):
            self.assertTrue(
                oc.auto_consent(
                    "https://auth.example/start",
                    recording_file=self.rec_file,
                    timeout_seconds=0.1,
                )
            )
        robot_cls.assert_called_once()
        self.assertFalse(self.rec_file.exists())
        self.assertIn("录制未完成", buf.getvalue())

    def test_record_disabled_uses_robot_only(self):
        p1, p2 = (
            patch.object(rec, "RecordingPlaywrightDriver", side_effect=AssertionError("unused")),
            patch.object(oc, "PlaywrightDriver", return_value=self.robot_fake),
        )
        p3 = patch.object(config, "YOUTUBE_OAUTH_RECORD_ENABLED", False)
        with p1, p2 as robot_cls, p3:
            self.assertTrue(
                oc.auto_consent("https://auth.example/start", recording_file=self.rec_file)
            )
        robot_cls.assert_called_once()

    def test_corrupt_recording_is_quarantined_and_re_recorded(self):
        self.rec_file.write_text("not json{{", encoding="utf-8")
        fake = FakeRecorderDriver(
            [S(url=CONSENT), S(url=LOCALHOST)],
            batches=[[ev(text="Continue", x=5, y=5)]],
        )
        p1, p2, p3 = self._patch(fake)
        with p1, p2, p3:
            self.assertTrue(
                oc.auto_consent(
                    "https://auth.example/start",
                    recording_file=self.rec_file,
                    timeout_seconds=5,
                )
            )
        self.assertTrue(self.rec_file.exists())
        self.assertEqual(len(rec.load_recording(self.rec_file)), 1)
        self.assertTrue(self.rec_file.with_name(self.rec_file.name + ".corrupt").exists())

    def test_missing_recording_file_disables_replay_for_other_reasons(self):
        # Without recording_file, record-replay is skipped entirely.
        p1 = patch.object(rec, "RecordingPlaywrightDriver", side_effect=AssertionError("unused"))
        p2 = patch.object(oc, "PlaywrightDriver", return_value=self.robot_fake)
        with p1, p2 as robot_cls:
            self.assertTrue(oc.auto_consent("https://auth.example/start"))
        robot_cls.assert_called_once()


if __name__ == "__main__":
    unittest.main()
