"""重复判断共用能力：实验与分析两侧共用同一份实现，写法统一。

- Outcome：所有动作的统一返回结构（成功/失败 + 说明 + 记录）。
- OnceGuard.run_once：同一 scope+key 只真正执行一次；
  并发调用串行化，重复调用拿到同一份成功结果，失败结果不缓存、可重试。
- require_operator：动作级权限边界，缺少操作人身份一律拒绝。

各业务服务在此之上分别保证自己的状态机、事务与版本边界。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Outcome:
    """动作执行的统一结果：ok=False 时 entry 为空，message 必须说明缺口。"""

    ok: bool
    message: str
    entry: dict[str, Any] | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class OnceGuard:
    """重复判断与并发串行化的共用能力。"""

    def __init__(self) -> None:
        self._meta_lock = threading.Lock()
        self._locks: dict[tuple[str, str], threading.Lock] = {}
        self._results: dict[tuple[str, str], Outcome] = {}

    def reset(self) -> None:
        with self._meta_lock:
            self._locks.clear()
            self._results.clear()

    def _lock_for(self, token: tuple[str, str]) -> threading.Lock:
        with self._meta_lock:
            return self._locks.setdefault(token, threading.Lock())

    def run_once(self, scope: str, key: str, fn: Callable[[], Outcome]) -> Outcome:
        """同一 scope+key 的成功结果只有一份。

        并发下先到者执行、后来者拿到同一份结果；fn 抛异常或返回失败时
        不写入缓存，调用方修复缺口后可以再次尝试。
        """
        token = (scope, str(key))
        lock = self._lock_for(token)
        with lock:
            cached = self._results.get(token)
            if cached is not None:
                return cached
            outcome = fn()
            if outcome.ok:
                self._results[token] = outcome
            return outcome


def require_operator(values: dict[str, Any]) -> str | None:
    """动作权限边界：返回操作人；缺失时返回 None，由调用方拒绝。"""
    operator = str(values.get("operator") or "").strip()
    return operator or None


once_guard = OnceGuard()


def reset_once_guard() -> None:
    once_guard.reset()
