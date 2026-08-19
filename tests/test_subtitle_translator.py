"""自测：字幕批量翻译（格式、解析、重试、小批兜底）。"""

import io
import sys
import types
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from yt2bili.subtitles.parser import Cue
from yt2bili.subtitles import translator as st


def make_cue(index, text, start=0.0, end=1.0):
    return Cue(index=index, start=start, end=end, text=text)


def fake_client(reply=None, error=None, call_log=None):
    """OpenAI-compatible fake client. error 可以是异常或异常列表（依次抛出）。"""

    def _create(**kwargs):
        if call_log is not None:
            call_log.append(kwargs)
        if error is not None:
            if isinstance(error, list):
                exc = error.pop(0) if error else None
            else:
                exc = error
            if exc is not None:
                raise exc
        message = types.SimpleNamespace(content=reply)
        choice = types.SimpleNamespace(message=message)
        return types.SimpleNamespace(choices=[choice])

    return types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=_create))
    )


class FormatBatchTests(unittest.TestCase):
    def test_folds_multiline_cues_to_single_line(self):
        """回归：多行 cue 折叠为空格，防止破坏 NUMBER|TEXT 格式丢行。"""
        batch = [
            make_cue(1, "第一行\n第二行"),
            make_cue(2, "正常单行"),
        ]
        out = st._format_batch(batch)
        self.assertEqual(out, "1|第一行 第二行\n2|正常单行")

    def test_indexes_preserved(self):
        batch = [make_cue(7, "text"), make_cue(8, "more")]
        self.assertEqual(st._format_batch(batch), "7|text\n8|more")


class ParseBatchResponseTests(unittest.TestCase):
    def test_parses_valid_lines(self):
        raw = "1|你好\n2|世界\n3|再见"
        result = st._parse_batch_response(raw, 3)
        self.assertEqual(result, [(1, "你好"), (2, "世界"), (3, "再见")])

    def test_missing_lines_warn_and_partial_result(self):
        raw = "1|你好\n3|再见"
        err = io.StringIO()
        with redirect_stderr(err):
            result = st._parse_batch_response(raw, 3)
        self.assertEqual(result, [(1, "你好"), (3, "再见")])
        self.assertIn("返回 2 条，预期 3 条", err.getvalue())
        self.assertIn("缺失 #2", err.getvalue())

    def test_garbage_lines_skipped(self):
        raw = "some preamble\n1|你好\n\nnot a line\n2|世界"
        result = st._parse_batch_response(raw, 2)
        self.assertEqual(result, [(1, "你好"), (2, "世界")])

    def test_non_int_index_skipped(self):
        raw = "x|bad\n1|ok"
        result = st._parse_batch_response(raw, 1)
        self.assertEqual(result, [(1, "ok")])

    def test_extra_lines_warned(self):
        raw = "1|a\n2|b\n3|c"
        err = io.StringIO()
        with redirect_stderr(err):
            result = st._parse_batch_response(raw, 2)
        self.assertIn("多余 #3", err.getvalue())
        self.assertEqual(len(result), 3)

    def test_markdown_code_fence_stripped(self):
        """容错：整个响应被 ``` 代码块包裹时照常解析。"""
        raw = "```\n1|你好\n2|世界\n```"
        result = st._parse_batch_response(raw, 2)
        self.assertEqual(result, [(1, "你好"), (2, "世界")])

    def test_alt_separators_accepted(self):
        """容错：模型用 . : ) 、 等分隔符替代 | 时仍能解析。"""
        raw = "1. 你好\n2: 世界\n3) 再见\n4、谢谢"
        result = st._parse_batch_response(raw, 4)
        self.assertEqual(result, [(1, "你好"), (2, "世界"), (3, "再见"), (4, "谢谢")])

    def test_unnumbered_lines_mapped_by_order(self):
        """兜底：无编号但行数一致 → 按行序映射到 1..N。"""
        raw = "你好\n世界\n再见"
        result = st._parse_batch_response(raw, 3)
        self.assertEqual(result, [(1, "你好"), (2, "世界"), (3, "再见")])

    def test_zero_parse_logs_raw_response(self):
        """诊断：一条都解析不出时打印原始响应，避免盲猜模型返回。"""
        raw = "抱歉，我无法翻译这些内容"
        err = io.StringIO()
        with redirect_stderr(err):
            result = st._parse_batch_response(raw, 5)
        self.assertEqual(result, [])
        self.assertIn("原始响应全文", err.getvalue())
        self.assertIn("抱歉", err.getvalue())

    def test_zero_parse_reports_finish_reason(self):
        """诊断：0 解析时带上 finish_reason（length=输出被截断）。"""
        err = io.StringIO()
        with redirect_stderr(err):
            st._parse_batch_response("没有编号的内容", 5, finish_reason="length")
        self.assertIn("finish_reason=length", err.getvalue())


class NeedsRetranslateTests(unittest.TestCase):
    def test_empty_text_flagged(self):
        """回归：模型回显编号但译文为空 → 必须重译。"""
        self.assertTrue(st._needs_retranslate(make_cue(1, "")))
        self.assertTrue(st._needs_retranslate(make_cue(1, "   ")))

    def test_oversized_flagged(self):
        self.assertTrue(st._needs_retranslate(make_cue(1, "x" * 201)))

    def test_english_text_flagged(self):
        self.assertTrue(st._needs_retranslate(make_cue(1, "Hello world this is English")))

    def test_chinese_text_ok(self):
        self.assertFalse(st._needs_retranslate(make_cue(1, "你好世界")))

    def test_mixed_numbers_ok(self):
        self.assertFalse(st._needs_retranslate(make_cue(1, "点击 12345 次")))


class RetranslateSmallBatchTests(unittest.TestCase):
    def test_success_path(self):
        client = fake_client("1|重新翻译的结果")
        with patch.object(st.config, "DEEPSEEK_THINKING", "disabled"):
            result = st._retranslate_small_batch(client, [make_cue(1, "原文本")], 1, 1)
        self.assertEqual(result[0].text, "重新翻译的结果")
        self.assertEqual(result[0].index, 1)
        self.assertEqual(result[0].start, 0.0)

    def test_api_failure_retries_then_keeps_original(self):
        client = fake_client(error=[RuntimeError("boom"), RuntimeError("boom")])
        with patch.object(st.config, "DEEPSEEK_THINKING", "disabled"), \
             patch.object(st.time, "sleep") as sleep:
            result = st._retranslate_small_batch(client, [make_cue(1, "原文保留")], 1, 1)
        self.assertEqual(result[0].text, "原文保留")
        sleep.assert_called_once_with(2)  # 两次尝试之间只睡一次 2s

    def test_glossary_applied_to_input(self):
        with patch("yt2bili.subtitles.translator._apply_glossary") as apply_gl:
            apply_gl.return_value = "已替换文本"
            client = fake_client("1|译")
            with patch.object(st.config, "DEEPSEEK_THINKING", "disabled"):
                st._retranslate_small_batch(client, [make_cue(1, "Abomination")], 1, 1)
        apply_gl.assert_called_with("Abomination")


class TranslateBatchTests(unittest.TestCase):
    def setUp(self):
        self.sleep_patcher = patch.object(st.time, "sleep")
        self.sleep_mock = self.sleep_patcher.start()
        self.addCleanup(self.sleep_patcher.stop)

    def test_success_batch(self):
        client = fake_client("1|你好\n2|世界")
        with patch.object(st.config, "DEEPSEEK_THINKING", "disabled"):
            result = st._translate_batch(client, [make_cue(1, "hi"), make_cue(2, "world")], 1, 1)
        self.assertEqual([c.text for c in result], ["你好", "世界"])
        self.assertEqual([c.index for c in result], [1, 2])

    def test_missing_cue_keeps_original(self):
        client = fake_client("1|你好")  # 只回了 1 条
        with patch.object(st.config, "DEEPSEEK_THINKING", "disabled"):
            result = st._translate_batch(client, [make_cue(1, "hi"), make_cue(2, "world")], 1, 1)
        self.assertEqual(result[1].text, "world")  # 未返回 → 保留原文

    def test_transient_failure_retries_then_succeeds(self):
        """回归：API 一次性失败不应让整批保留原文 —— 3 次重试（3s/6s 退避）。"""
        client = fake_client(error=[TimeoutError("t"), None], reply="1|你好\n2|世界")
        with patch.object(st.config, "DEEPSEEK_THINKING", "disabled"):
            result = st._translate_batch(client, [make_cue(1, "hi"), make_cue(2, "world")], 1, 1)
        self.assertEqual([c.text for c in result], ["你好", "世界"])
        self.sleep_mock.assert_has_calls([
            unittest.mock.call(3),  # 第一次失败 → 3s
        ])

    def test_persistent_failure_falls_back_to_small_batches(self):
        """回归：API 持续失败 → 5 条小批兜底，最终保留原文而不是全丢。"""
        client = fake_client(error=[RuntimeError("x"), RuntimeError("x"), RuntimeError("x")])
        with patch.object(st.config, "DEEPSEEK_THINKING", "disabled"):
            result = st._translate_batch(client, [make_cue(i, f"text{i}") for i in range(1, 12)], 1, 1)
        # 11 条 → 3 次整批重试失败 → 拆 3 组小批（5/5/1），每组小批 2 次尝试失败 → 保留原文
        self.assertEqual(len(result), 11)
        self.assertEqual([c.text for c in result], [f"text{i}" for i in range(1, 12)])

    def test_validation_retry_fixes_untranslated(self):
        """结果中含未翻译行 → _validate_and_retry 逐个重译。"""
        def fake_retranslate(client, group, batch_num, total):
            return [
                Cue(index=c.index, start=c.start, end=c.end, text=f"重译{c.index}")
                for c in group
            ]
        client = fake_client("1|English text here\n2|你好")
        with patch.object(st.config, "DEEPSEEK_THINKING", "disabled"), \
             patch.object(st, "_retranslate_small_batch", side_effect=fake_retranslate):
            result = st._translate_batch(client, [make_cue(1, "hi"), make_cue(2, "你好")], 1, 1)
        self.assertEqual(result[0].text, "重译1")

    def test_always_sends_thinking_param(self):
        """回归：thinking 必须显式下发 —— deepseek-v4 默认思考会烧光 max_tokens。"""
        call_log = []
        client = fake_client("1|译", call_log=call_log)
        with patch.object(st.config, "DEEPSEEK_THINKING", "disabled"):
            st._translate_batch(client, [make_cue(1, "hi")], 1, 1)
        self.assertEqual(call_log[0]["extra_body"], {"thinking": {"type": "disabled"}})

    def test_reasoning_truncation_escalates_budget(self):
        """回归：思考阶段烧光预算（length+空正文+有思考内容）→ 3x 预算重试成功。"""
        call_log = []
        resp1 = types.SimpleNamespace(choices=[types.SimpleNamespace(
            message=types.SimpleNamespace(content="", reasoning_content="thinking..."),
            finish_reason="length",
        )])
        resp2 = types.SimpleNamespace(choices=[types.SimpleNamespace(
            message=types.SimpleNamespace(content="1|你好\n2|世界"),
            finish_reason="stop",
        )])
        replies = iter([resp1, resp2])

        def _create(**kwargs):
            call_log.append(kwargs)
            return next(replies)

        client = types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=_create))
        )
        with patch.object(st.config, "DEEPSEEK_THINKING", "disabled"):
            result = st._translate_batch(client, [make_cue(1, "hi"), make_cue(2, "world")], 1, 1)
        self.assertEqual([c.text for c in result], ["你好", "世界"])
        self.assertEqual(len(call_log), 2)
        self.assertEqual(call_log[1]["max_tokens"], call_log[0]["max_tokens"] * 3)


class ReasoningTruncationGuardTests(unittest.TestCase):
    """_retry_when_reasoning_truncated 的触发条件。"""

    def _resp(self, content, finish_reason="", reasoning=""):
        return types.SimpleNamespace(choices=[types.SimpleNamespace(
            message=types.SimpleNamespace(content=content, reasoning_content=reasoning),
            finish_reason=finish_reason,
        )])

    def test_escalates_when_budget_burned_on_reasoning(self):
        call_log = []
        resp1 = self._resp("", "length", "deep thinking...")
        replies = iter([self._resp("1|译")])

        def _create(**kwargs):
            call_log.append(kwargs)
            return next(replies)

        client = types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=_create))
        )
        new = st._retry_when_reasoning_truncated(
            client, resp1, "sys", "prompt", 4096, 0.3, "批次 1/1"
        )
        self.assertEqual(st._response_text(new)[0], "1|译")
        self.assertEqual(call_log[0]["max_tokens"], 4096 * 3)

    def test_no_escalate_when_content_present(self):
        resp = self._resp("1|译", "length", "thinking...")  # 正文非空 → 不动
        client = fake_client("unused")
        self.assertIs(
            st._retry_when_reasoning_truncated(client, resp, "sys", "p", 100, 0.3, "x"),
            resp,
        )

    def test_no_escalate_without_reasoning(self):
        resp = self._resp("", "length")  # 无思考内容 → 不动
        client = fake_client("unused")
        self.assertIs(
            st._retry_when_reasoning_truncated(client, resp, "sys", "p", 100, 0.3, "x"),
            resp,
        )


class TranslateCuesTests(unittest.TestCase):
    def test_empty_cues_raises(self):
        with self.assertRaises(RuntimeError):
            st.translate_cues([])

    def test_missing_api_key_raises(self):
        with patch.object(st.config, "DEEPSEEK_API_KEY", ""):
            with self.assertRaises(RuntimeError):
                st.translate_cues([make_cue(1, "hi")])

    def test_batch_splitting_and_ordering(self):
        client = fake_client("1|译一\n2|译二\n3|译三")
        with patch.object(st.config, "DEEPSEEK_THINKING", "disabled"), \
             patch.object(st, "_build_client", return_value=client), \
             patch.object(st.config, "DEEPSEEK_API_KEY", "k"):
            result = st.translate_cues(
                [make_cue(i, f"text{i}") for i in range(1, 4)],
                batch_size=2,
            )
        self.assertEqual([c.index for c in result], [1, 2, 3])
        self.assertEqual([c.text for c in result], ["译一", "译二", "译三"])


if __name__ == "__main__":
    unittest.main()
