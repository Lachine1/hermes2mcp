"""Application-owned native contexts; these are not MCP transport sessions."""

from __future__ import annotations

import asyncio
import math
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass


class ContextLimitError(RuntimeError):
    """A new retained context would exceed the application's bound."""


def validate_context_settings(limit: int, idle_timeout: float) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("max_contexts must be a positive integer")
    try:
        valid_timeout = (
            not isinstance(idle_timeout, bool)
            and isinstance(idle_timeout, (int, float))
            and math.isfinite(idle_timeout)
            and idle_timeout > 0
        )
    except OverflowError:
        valid_timeout = False
    if not valid_timeout:
        raise ValueError("context_idle_timeout must be a positive, finite number of seconds")


@dataclass
class NativeContext:
    session_id: str
    idle_since: float
    active: int = 0


class NativeContexts:
    """Case-sensitive keys with fresh native identities for every lease generation."""

    def __init__(
        self,
        schedule_cleanup: Callable[[str], asyncio.Task[None]],
        *,
        limit: int = 128,
        idle_timeout: float = 1800,
        clock: Callable[[], float] = time.monotonic,
    ):
        validate_context_settings(limit, idle_timeout)
        self.schedule_cleanup = schedule_cleanup
        self.limit = limit
        self.idle_timeout = idle_timeout
        self.clock = clock
        self.entries: dict[str, NativeContext] = {}
        self._retired: dict[str, NativeContext] = {}

    def acquire(self, key: str) -> str:
        self.expire_idle()
        entry = self.entries.get(key)
        if entry is None:
            if len(self.entries) >= self.limit:
                raise ContextLimitError("Native context limit reached.")
            entry = NativeContext(
                "modern-context-" + key + "-" + secrets.token_hex(16), self.clock()
            )
            self.entries[key] = entry
        entry.active += 1
        return entry.session_id

    def complete(self, key: str, session_id: str) -> None:
        entry = self.entries.get(key)
        if entry is None or entry.session_id != session_id:
            entry = self._retired.get(session_id)
        if entry is not None and entry.active:
            entry.active -= 1
            if not entry.active:
                if self._retired.pop(session_id, None) is entry:
                    self.schedule_cleanup(session_id)
                else:
                    entry.idle_since = self.clock()

    def release(self, key: str) -> bool:
        entry = self.entries.pop(key, None)
        if entry is None:
            return False
        if entry.active:
            self._retired[entry.session_id] = entry
        else:
            self.schedule_cleanup(entry.session_id)
        return True

    def expire_idle(self) -> None:
        now = self.clock()
        for key, entry in list(self.entries.items()):
            if not entry.active and now - entry.idle_since >= self.idle_timeout:
                self.release(key)

    def release_all(self) -> None:
        for key in list(self.entries):
            self.release(key)

    async def reap_idle(self) -> None:
        while True:
            await asyncio.sleep(min(self.idle_timeout, 60))
            self.expire_idle()
