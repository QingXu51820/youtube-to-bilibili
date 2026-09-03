"""自测：json3 逐词字幕重分段（句子边界、说话人切换、标记、CJK 拼接）。"""

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from yt2bili.subtitles import downloader
from yt2bili.subtitles import resegment
from yt2bili.subtitles.parser import parse_srt
from yt2bili.subtitles.resegment import resegment_file, resegment_json3


# ── json3 fixture helpers ─────────────────────────────────────────────

def _seg(text, offset=None):
    d = {"utf8": text}
    if offset is not None:
        d["tOffsetMs"] = offset
    return d


def _event(t_start_ms, d_duration_ms, segs):
    return {"tStartMs": t_start_ms, "dDurationMs": d_duration_ms, "segs": segs}


def _json3(*events):
    return json.dumps({"events": list(events)})


def _texts(cues):
    return [c.text for c in cues]


# ── 纯逻辑：resegment_json3 ───────────────────────────────────────────

class ResegmentJson3Tests(unittest.TestCase):
    def test_words_joined_across_caption_windows(self):
        # 真实 json3 中相邻窗口的词在时间上连续（下一个窗口的首词紧跟
        # 上一个窗口的末词之后）
        j = _json3(
            _event(0, 2000, [_seg("It's"), _seg(" time", 280)]),
            _event(600, 2000, [_seg(" for", 0), _seg(" version", 400)]),
        )
        cues = resegment_json3(j)
        self.assertEqual(len(cues), 1)
        self.assertEqual(cues[0].text, "It's time for version")
        self.assertAlmostEqual(cues[0].start, 0.0)
        self.assertGreater(cues[0].end, cues[0].start)

    def test_end_punctuation_and_capital_splits(self):
        # 回归："…tier list." + "We're…" 必须切成两条（flush 不能吞掉下一句）
        j = _json3(
            _event(0, 3000, [
                _seg("compared"), _seg(" to", 200), _seg(" last", 400),
                _seg(" tier", 600), _seg(" list.", 800),
            ]),
            _event(3000, 2000, [_seg("We're"), _seg(" going", 200)]),
        )
        cues = resegment_json3(j)
        self.assertEqual(len(cues), 2)
        self.assertTrue(cues[0].text.endswith("tier list."))
        self.assertTrue(cues[1].text.startswith("We're going"))

    def test_mr_abbreviation_not_split(self):
        j = _json3(
            _event(0, 3000, [
                _seg("Mr."), _seg(" Beast", 200), _seg(" is", 400),
                _seg(" great.", 600),
            ])
        )
        cues = resegment_json3(j)
        self.assertEqual(len(cues), 1)
        self.assertEqual(cues[0].text, "Mr. Beast is great.")

    def test_speaker_change_flushes(self):
        j = _json3(
            _event(0, 2000, [_seg("hello."), _seg(">> ", 400), _seg("World", 600)])
        )
        cues = resegment_json3(j)
        self.assertEqual(_texts(cues), ["hello.", "World"])

    def test_speaker_change_marker_standalone(self):
        j = _json3(
            _event(0, 3000, [
                _seg("a"), _seg(" b", 200), _seg(">> [Laughter]", 400), _seg(" c", 600),
            ])
        )
        cues = resegment_json3(j)
        self.assertEqual(_texts(cues), ["a b", "[Laughter]", "c"])

    def test_music_marker_standalone(self):
        j = _json3(
            _event(0, 3000, [_seg("a"), _seg(" b", 200), _seg("[Music]", 400), _seg(" c", 600)])
        )
        cues = resegment_json3(j)
        self.assertEqual(_texts(cues), ["a b", "[Music]", "c"])

    def test_marker_inverted_timestamps(self):
        # marker 的显示偏移晚于下一个词 → 时间倒挂，交换后 start <= end
        j = _json3(
            _event(1000, 5000, [_seg("[Music]", 4000), _seg("word", 0)])
        )
        cues = resegment_json3(j)
        self.assertEqual(cues[0].text, "[Music]")
        self.assertLessEqual(cues[0].start, cues[0].end)

    def test_pause_greater_than_800ms_splits(self):
        j = _json3(
            _event(0, 500, [_seg("Hello")]),
            _event(2000, 500, [_seg("World")]),
        )
        cues = resegment_json3(j)
        self.assertEqual(_texts(cues), ["Hello", "World"])

    def test_max_duration_cap_splits(self):
        segs = [_seg(f"w{i}", i * 1000) for i in range(15)]  # 14s 跨度
        cues = resegment_json3(_json3(_event(0, 20000, segs)))
        self.assertGreaterEqual(len(cues), 2)

    def test_comma_split_over_max_chars(self):
        # >140 字符，flush 时按逗号拆分，且所有词不丢失
        words = []
        for i in range(1, 31):
            w = f"word{i}," if i in (10, 20) else f"word{i}"
            words.append(w)
        segs = [_seg(t, i * 100) for i, t in enumerate(words)]
        j = _json3(
            _event(0, 10000, segs),
            _event(10000, 2000, [_seg("done."), _seg(" Next", 200), _seg(" thing", 400)]),
        )
        cues = resegment_json3(j)
        joined = " ".join(_texts(cues))
        self.assertEqual(len(joined.split()), 33)  # 30 + done. + Next + thing
        self.assertGreaterEqual(len(cues), 3)
        for c in cues:
            self.assertLessEqual(len(c.text), resegment.MAX_CHARS)

    def test_events_without_segs_skipped(self):
        j = _json3(
            _event(0, 2000, []),
            _event(2000, 2000, [_seg("\n")]),
        )
        self.assertEqual(resegment_json3(j), [])

    def test_newline_segs_ignored(self):
        # 每窗口一次的换行 seg 不构成句子边界
        j = _json3(
            _event(0, 2000, [_seg("a"), _seg("\n", 100), _seg(" b", 200)])
        )
        cues = resegment_json3(j)
        self.assertEqual(_texts(cues), ["a b"])

    def test_window_end_word_duration_estimated(self):
        # 窗口显示时长 10s，但词只占 0.6s——末词时长必须用中位间隔估算
        j = _json3(
            _event(0, 10000, [_seg("a"), _seg(" b", 200), _seg(" c", 400)])
        )
        cues = resegment_json3(j)
        self.assertEqual(cues[0].text, "a b c")
        self.assertLess(cues[0].end, 9.0)

    def test_cjk_words_joined_without_spaces(self):
        j = _json3(
            _event(0, 2000, [_seg("こんにちは"), _seg("世界", 400)])
        )
        cues = resegment_json3(j)
        self.assertEqual(cues[0].text, "こんにちは世界")

    def test_empty_events_returns_empty_list(self):
        self.assertEqual(resegment_json3('{"events": []}'), [])

    def test_corrupt_json_raises(self):
        with self.assertRaises(json.JSONDecodeError):
            resegment_json3("not json")


# ── 文件级：resegment_file ────────────────────────────────────────────

class ResegmentFileTests(unittest.TestCase):
    def _sample_json3_path(self, tmp: Path) -> Path:
        j = _json3(
            _event(0, 2000, [_seg("hello"), _seg(" world.", 400)])
        )
        p = tmp / "abc123.en.json3"
        p.write_text(j, encoding="utf-8")
        return p

    def test_resegment_file_writes_and_returns_path(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._sample_json3_path(Path(d))
            out = Path(d) / "abc123.en.srt"
            ret = resegment_file(p, out)
            self.assertEqual(ret, str(out.resolve()))
            cues = parse_srt(out)
            self.assertEqual([c.text for c in cues], ["hello world."])

    def test_resegment_file_default_output_naming(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._sample_json3_path(Path(d))
            ret = resegment_file(p)
            self.assertTrue(Path(ret).exists())
            self.assertTrue(Path(ret).name.endswith(".reseg.srt"))


# ── downloader 助手 ───────────────────────────────────────────────────

class DownloaderHelperTests(unittest.TestCase):
    def test_find_subtitle_file_prefers_json3(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "abc.en.json3").write_text("{}", encoding="utf-8")
            Path(d, "abc.en.srt").write_text("", encoding="utf-8")
            got = downloader._find_subtitle_file(d, "abc", "en")
            self.assertTrue(got.endswith("abc.en.json3"))

    def test_find_subtitle_file_srt_only(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "abc.en.srt").write_text("", encoding="utf-8")
            got = downloader._find_subtitle_file(d, "abc", "en")
            self.assertTrue(got.endswith("abc.en.srt"))
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(downloader._find_subtitle_file(d, "abc", "en"))

    def test_resegment_json3_to_srt_success_deletes_json3(self):
        with tempfile.TemporaryDirectory() as d:
            jp = Path(d, "abc.en.json3")
            jp.write_text(
                _json3(_event(0, 2000, [_seg("hello"), _seg(" world.", 400)])),
                encoding="utf-8",
            )
            out = Path(d, "abc.en.srt")
            ret = downloader._resegment_json3_to_srt(jp, out)
            self.assertEqual(ret, str(out.resolve()))
            self.assertFalse(jp.exists())  # json3 已删除
            self.assertEqual([c.text for c in parse_srt(out)], ["hello world."])

    def test_resegment_json3_to_srt_corrupt_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            jp = Path(d, "abc.en.json3")
            jp.write_text("not json", encoding="utf-8")
            buf = StringIO()
            with redirect_stdout(buf):
                ret = downloader._resegment_json3_to_srt(jp, Path(d, "abc.en.srt"))
            self.assertIsNone(ret)
            self.assertTrue(jp.exists())  # 失败时保留 json3 便于排查
            self.assertIn("重分段失败", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
