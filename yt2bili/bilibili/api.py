"""B站 HTTP 接口的共用部分：常量、响应检查、错误码解析。

三条通道（字幕 / 合集 / 投稿）各自复制了 ``_AUTH_ERROR_CODES``，view 接口的 URL
出现在两个模块里，``_check_response`` 更是两份同体不同异常类型的实现 —— 而错误码
解析（``extract_code`` / ``is_gone_code``）本模块的调用方一直是从字幕模块借的。
"""

from __future__ import annotations

import re

import httpx

#: 凭据过期 / 无权限的 HTTP 状态码。
AUTH_ERROR_CODES = (401, 403)

#: 稿件信息（分 P、时长、标题）查询接口。
VIDEO_INFO_URL = "https://api.bilibili.com/x/web-interface/view"

#: 稿件已消失的错误码。网页端统一渲染"视频去哪了呢？"
#: 实测：62012（"稿件不可见"的另一种返回）在用 **UP主本人** 的凭据查询时会返回
#: code=0 —— 那是"仅自己可见"，稿件还在，不能当删除处理；所以只认下面两个码。
GONE_CODES = (-404, 62002)

_RELOGIN_HINT = (
    "B站登录凭据已过期（HTTP {status}），请重新扫码登录。\n"
    "运行: python main.py --login"
)


def check_response(resp: httpx.Response, label: str = "Bilibili API", *,
                   error_factory=None) -> dict:
    """检查 B站 响应：凭据过期 / 非 JSON / ``code != 0`` 都抛异常。

    Args:
        resp: httpx 响应。
        label: 出错信息里的接口名。
        error_factory: ``(code, message) -> Exception``，决定 ``code != 0`` 时的异常
            类型 —— 合集侧要带 ``code`` 属性做限流判定，字幕侧只要消息。
            缺省抛 :class:`RuntimeError`。凭据过期始终抛 ``RuntimeError``
            （调用方靠"重新扫码登录"这句话识别）。

    Returns:
        ``code == 0`` 时的响应体。

    错误消息里始终带 ``code=<数字>``：:func:`extract_code` 与合集侧的限流判定都靠它。
    """
    if resp.status_code in AUTH_ERROR_CODES:
        raise RuntimeError(_RELOGIN_HINT.format(status=resp.status_code))
    try:
        data = resp.json()
    except Exception as e:
        raise RuntimeError(f"{label} 返回非 JSON 响应: {e}")

    code = data.get("code", -1)
    if code != 0:
        msg = data.get("message", str(data))
        # 字幕接口会在 data 里逐行给出具体哪几句不合法（L{行号}: {原因}）
        err_data = data.get("data")
        if isinstance(err_data, list) and err_data:
            details = "; ".join(
                f"L{d.get('line', '?')}: {d.get('error_msg', str(d))}"
                for d in err_data[:10]
            )
            if len(err_data) > 10:
                details += f" ...(+{len(err_data) - 10} more)"
            msg = f"{msg} [{details}]"
        text = f"{label} 返回错误 (code={code}): {msg}"
        raise error_factory(code, text) if error_factory else RuntimeError(text)

    return data


def extract_code(message: str) -> int | None:
    """``check_response`` 消息里的 B站 错误码，取不到时返回 None。"""
    match = re.search(r"code=(-?\d+)", message)
    return int(match.group(1)) if match else None


def is_gone_code(code: int | None) -> bool:
    """该错误码是否表示稿件已消失。"""
    return code in GONE_CODES


def is_gone_error(exc: BaseException) -> bool:
    """该异常（由本包抛出）是否在说稿件已消失。"""
    return is_gone_code(extract_code(str(exc)))
