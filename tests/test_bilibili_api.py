"""自测：B站 响应检查与错误码解析（字幕/合集/投稿共用）。

回归背景：三处各写了一份 `_AUTH_ERROR_CODES`，`_check_response` 有两份同体不同
异常类型的实现，错误码解析则是从字幕模块借给合集清理用的。
"""

import sys
import unittest

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from yt2bili.bilibili import api


class FakeResponse:
    def __init__(self, payload=None, status_code=200, raise_on_json=None):
        self.payload = payload
        self.status_code = status_code
        self._raise = raise_on_json

    def json(self):
        if self._raise is not None:
            raise self._raise
        return self.payload


class CustomError(RuntimeError):
    def __init__(self, message, code=-1):
        super().__init__(message)
        self.code = code


class CheckResponseTests(unittest.TestCase):
    def test_auth_status_raises_with_relogin_hint(self):
        for status in api.AUTH_ERROR_CODES:
            with self.subTest(status=status):
                with self.assertRaises(RuntimeError) as ctx:
                    api.check_response(FakeResponse({}, status_code=status), "获取合集列表")
                self.assertIn("重新扫码登录", str(ctx.exception))
                self.assertIn(str(status), str(ctx.exception))

    def test_non_json_raises(self):
        resp = FakeResponse(raise_on_json=ValueError("nope"))
        with self.assertRaises(RuntimeError) as ctx:
            api.check_response(resp, "上传封面")
        self.assertIn("上传封面", str(ctx.exception))
        self.assertIn("非 JSON", str(ctx.exception))

    def test_ok_returns_body(self):
        self.assertEqual(
            api.check_response(FakeResponse({"code": 0, "data": {"x": 1}}), "封面上传"),
            {"code": 0, "data": {"x": 1}},
        )

    def test_error_factory_controls_the_exception_type(self):
        """合集侧靠 ``e.code`` 判限流与 -404；字幕侧只要消息。"""
        with self.assertRaises(CustomError) as ctx:
            api.check_response(
                FakeResponse({"code": 20113, "message": "手速太快啦～"}),
                "加入合集",
                error_factory=lambda code, message: CustomError(message, code=code),
            )
        self.assertEqual(ctx.exception.code, 20113)
        self.assertIn("手速太快啦", str(ctx.exception))
        self.assertIn("code=20113", str(ctx.exception))

    def test_default_error_is_runtime_error_with_code_in_message(self):
        with self.assertRaises(RuntimeError) as ctx:
            api.check_response(FakeResponse({"code": 62002, "message": "稿件不可见"}))
        self.assertIn("code=62002", str(ctx.exception))
        self.assertEqual(api.extract_code(str(ctx.exception)), 62002)

    def test_per_line_details_are_included(self):
        payload = {
            "code": 79014,
            "message": "字幕不合法",
            "data": [{"line": 3, "error_msg": "时间点超长"}],
        }
        with self.assertRaises(RuntimeError) as ctx:
            api.check_response(FakeResponse(payload), "submit_subtitle")
        self.assertIn("L3: 时间点超长", str(ctx.exception))


class CodeParsingTests(unittest.TestCase):
    def test_extract_code(self):
        self.assertEqual(api.extract_code("x 返回错误 (code=-404): 啥都木有"), -404)
        self.assertEqual(api.extract_code("x 返回错误 (code=62002): 稿件不可见"), 62002)
        self.assertIsNone(api.extract_code("网络错误"))

    def test_is_gone_code(self):
        for code in api.GONE_CODES:
            self.assertTrue(api.is_gone_code(code))
        # 62012 = "仅自己可见"：稿件还在，不能当删除
        self.assertFalse(api.is_gone_code(62012))
        self.assertFalse(api.is_gone_code(None))

    def test_is_gone_error(self):
        self.assertTrue(api.is_gone_error(RuntimeError("x (code=62002): 稿件不可见")))
        self.assertFalse(api.is_gone_error(RuntimeError("B站视频信息查询网络错误: x")))
        self.assertFalse(api.is_gone_error(RuntimeError("x (code=-412): 请求被拦截")))


if __name__ == "__main__":
    unittest.main()
