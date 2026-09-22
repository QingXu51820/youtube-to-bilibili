"""Windows 友好的原子写入（供并发运行的监控进程共享文件使用）。

``临时文件 + os.replace`` 是原子写入的标准写法，但在 Windows 上还差两件事：

* ``os.replace``（``MoveFileEx``）在**目标文件被任何其他进程打开**时失败
  （``WinError 32`` / ``WinError 5``）。CPython 的 ``open()`` 不传
  ``FILE_SHARE_DELETE``，所以「另一个监控进程正好在读 token」就足以让写回失败，
  异常一路冒到顶层，把长跑进程打死。
* 固定的 ``<name>.tmp`` 文件名意味着两个进程会互相搬走对方的临时文件。

``atomic_write_text`` 同时解决这两点：每次写入使用带进程号的独立临时文件名，
目标被占用时短暂重试 rename；读者看到的永远是旧内容或新内容，不会读到半截文件。
"""

from __future__ import annotations

import itertools
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

#: rename/unlink 的重试次数与间隔 —— 对方通常只是读一下（毫秒级）。
DEFAULT_ATTEMPTS = 10
DEFAULT_RETRY_DELAY = 0.2

#: Windows: 5=拒绝访问, 32=文件正被其他进程使用, 33=文件被锁定。
_RETRYABLE_WINERRORS = frozenset({5, 32, 33})

_counter = itertools.count()

#: 进程内按路径注册的互斥锁（见 :func:`path_lock`）。
_path_locks: dict[str, threading.Lock] = {}
_path_locks_guard = threading.Lock()


def _is_sharing_violation(exc: OSError) -> bool:
    """目标文件被其他进程占用（Windows 分享冲突）时为 True。

    Windows 在「目标被打开」时报 ``PermissionError(WinError 32)``，偶尔是
    ``WinError 5``；POSIX 上 rename 的 ``PermissionError`` 是目录权限问题，
    重试几次无害。
    """
    if isinstance(exc, PermissionError):
        return True
    return getattr(exc, "winerror", None) in _RETRYABLE_WINERRORS


def _temp_path(path: Path) -> Path:
    """同目录下的**唯一**临时文件名（进程号 + 计数，避免并发进程互相顶掉）。"""
    return path.with_name(f"{path.name}.{os.getpid()}-{next(_counter)}.tmp")


def atomic_write_text(
    path: Path | str,
    text: str,
    *,
    encoding: str = "utf-8",
    attempts: int = DEFAULT_ATTEMPTS,
    retry_delay: float = DEFAULT_RETRY_DELAY,
) -> None:
    """原子写入 ``text``，容忍 Windows 上目标文件被其他进程短暂占用。

    重试用尽后抛出最后一个 ``OSError``（目标文件保持原内容，临时文件已清理）。
    是否把它当作致命错误由调用方决定 —— 例如刷新后的 token 已经在内存里可用，
    写回失败无需中断监控。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _temp_path(path)
    tmp.write_text(text, encoding=encoding)
    try:
        _replace_with_retry(tmp, path, attempts=attempts, retry_delay=retry_delay)
    except BaseException:
        remove_best_effort(tmp)
        raise


def _replace_with_retry(
    tmp: Path, path: Path, *, attempts: int, retry_delay: float
) -> None:
    total = max(1, attempts)
    last_error: OSError | None = None
    for attempt in range(total):
        try:
            os.replace(tmp, path)
            return
        except OSError as exc:
            if not _is_sharing_violation(exc):
                raise
            last_error = exc
            if attempt + 1 < total and retry_delay > 0:
                time.sleep(retry_delay)
    assert last_error is not None
    raise last_error


def remove_best_effort(
    path: Path | str,
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    retry_delay: float = DEFAULT_RETRY_DELAY,
) -> bool:
    """删除文件但绝不抛异常（被其他进程占用时重试几次）。

    返回 True 表示文件已不存在（删掉了，或本来就没有）。
    """
    path = Path(path)
    total = max(1, attempts)
    for attempt in range(total):
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return True
        except OSError as exc:
            if not _is_sharing_violation(exc) or attempt + 1 >= total:
                return False
            if retry_delay > 0:
                time.sleep(retry_delay)
    return False


def read_text_with_retry(
    path: Path | str,
    *,
    encoding: str = "utf-8",
    attempts: int = DEFAULT_ATTEMPTS,
    retry_delay: float = DEFAULT_RETRY_DELAY,
) -> str:
    """读取文本，容忍其他进程正在替换该文件（Windows 分享冲突）。

    另一个进程 ``os.replace`` 到该路径的瞬间，本进程的 ``open`` 会报
    ``WinError 32``/``5``（``PermissionError``）。重试几次即可跨过这个毫秒级
    窗口；文件不存在（``FileNotFoundError``）等真实错误照常抛出。
    """
    path = Path(path)
    total = max(1, attempts)
    last_error: OSError | None = None
    for attempt in range(total):
        try:
            return path.read_text(encoding=encoding)
        except FileNotFoundError:
            raise
        except OSError as exc:
            if not _is_sharing_violation(exc):
                raise
            last_error = exc
            if attempt + 1 < total and retry_delay > 0:
                time.sleep(retry_delay)
    assert last_error is not None
    raise last_error


def path_lock(path: Path | str) -> threading.Lock:
    """该路径对应的进程内互斥锁（同一路径始终拿到同一把）。

    同一进程里有多个写者会读-改-写同一份状态文件（例如翻译 worker、pipeline 线程和
    延迟上传 sweep 都写 ``pending_subtitles.json``）：进程内的竞争靠这把锁串行化，
    跨进程的竞争仍靠 :func:`atomic_write_text` 的原子替换 —— 两者缺一不可。

    路径按绝对路径 + 大小写归一化后作键，所以相对写法与绝对写法拿到的是同一把锁。
    """
    key = os.path.normcase(os.path.abspath(os.fspath(path)))
    with _path_locks_guard:
        lock = _path_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _path_locks[key] = lock
        return lock


def write_json(path: Path | str, data: Any, *, indent: int = 2) -> None:
    """原子写入 JSON，格式与仓内既有状态文件一致（不转义中文 + 缩进 + 结尾换行）。"""
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=indent) + "\n")


def read_json(
    path: Path | str,
    default: Any = None,
    *,
    expect: type | tuple[type, ...] | None = None,
    validate: Callable[[Any], str | None] | None = None,
    backup_corrupt: bool = False,
    label: str = "[state]",
) -> Any:
    """读取 JSON 状态文件，容忍文件缺失、BOM、空文件与内容损坏。

    Args:
        path: 文件路径。
        default: 文件缺失/为空/损坏时返回的值。
        expect: 顶层类型（如 ``dict``、``list``）；类型不符按损坏处理。``None`` 不校验。
        validate: 更细的结构校验 —— 返回 ``None`` 表示通过，返回字符串则作为损坏原因
            （例如 ``lambda d: None if isinstance(d.get("videos"), dict) else "videos 字段格式错误"``）。
        backup_corrupt: 损坏时先把文件改名成 ``<name>.bak-<时间戳>`` 再返回 ``default``
            —— 队列/状态文件用它保住现场；纯缓存文件不需要，静默取默认值即可。
        label: 备份提示的日志前缀。

    Returns:
        解析出的对象，或 ``default``。

    空文件（内容只有空白）按"没有数据"处理：写了一半就退出的临时状态不该制造备份。
    """
    target = Path(path)
    try:
        text = read_text_with_retry(target, encoding="utf-8-sig")
    except (FileNotFoundError, OSError):
        return default

    if not text.strip():
        return default

    reason = ""
    try:
        data = json.loads(text)
    except ValueError as exc:
        reason = f"不是有效 JSON: {exc}"
    else:
        if expect is not None and not isinstance(data, expect):
            names = getattr(expect, "__name__", None) or "/".join(
                getattr(t, "__name__", str(t)) for t in expect
            )
            reason = f"顶层类型不是 {names}"
        elif validate is not None:
            reason = validate(data) or ""

    if not reason:
        return data

    if backup_corrupt:
        _backup_corrupt(target, reason, label)
    return default


def _backup_corrupt(path: Path, reason: str, label: str) -> None:
    """把损坏的状态文件改名留档，并打印一行提示（改名失败则原地保留）。

    损坏不能悄悄把数据丢掉：文件先备份再让调用方重建空状态，操作者事后还能看出
    哪里出了问题。
    """
    backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    try:
        path.replace(backup)
    except OSError:
        print(f"{label} [WARN] 状态文件损坏（{reason}），未能备份，按空状态继续", flush=True)
        return
    print(
        f"{label} [WARN] 状态文件损坏（{reason}），已备份到 {backup.name} 并重建",
        flush=True,
    )
