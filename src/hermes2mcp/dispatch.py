"""Private native-work scheduling; uses only the Python standard library."""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any


class NativeDispatch:
    """Preserve context-local order without reserving threads for queued contexts."""

    def __init__(self, runtime: Any, reply: Callable[[dict[str, Any]], None]):
        self.runtime = runtime
        self.reply = reply
        self._queues: dict[str, deque[dict[str, Any]]] = {}
        self._active: dict[str, list[dict[str, Any]]] = {}
        self._running = 0
        self._condition = threading.Condition()
        self._closing = False
        self._executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="hermes-native")
        self._dispatcher = threading.Thread(target=self._pump, name="hermes-dispatch", daemon=True)
        self._dispatcher.start()

    def submit(self, request: dict[str, Any]) -> None:
        with self._condition:
            if self._closing:
                raise RuntimeError("Native dispatcher is closing.")
            self._queues.setdefault(request["session_id"], deque()).append(request)
            self._condition.notify_all()

    def _pump(self) -> None:
        while True:
            with self._condition:
                while True:
                    if self._closing and not self._queues and not self._active:
                        return
                    eligible = next(
                        (
                            key
                            for key, queue in self._queues.items()
                            if queue and self._can_start(key, queue[0])
                        ),
                        None,
                    )
                    if eligible is not None and self._running < 8:
                        break
                    self._condition.wait()
                request = self._queues[eligible].popleft()
                self._active.setdefault(eligible, []).append(request)
                self._running += 1
            future = self._executor.submit(self._execute, request)
            future.add_done_callback(
                lambda done, key=eligible, item=request: self._complete(key, item, done)
            )

    def _can_start(self, key: str, request: dict[str, Any]) -> bool:
        active = self._active.get(key, [])
        if not active:
            return True
        if request["op"] != "call" or any(item["op"] != "call" for item in active):
            return False
        return self.runtime.parallel_safe(key, [*active, request])

    def cancel(self, identity: str) -> bool:
        """Remove only work that has not started; running effects cannot be rolled back."""
        with self._condition:
            for key, queue in list(self._queues.items()):
                for request in queue:
                    if request["id"] == identity:
                        queue.remove(request)
                        if not queue and key not in self._active:
                            del self._queues[key]
                        self._condition.notify_all()
                        return True
            return False

    def _execute(self, request: dict[str, Any]) -> Any:
        if request["op"] == "release":
            self.runtime.release(request["session_id"])
            return None
        return self.runtime.call(request["name"], request["arguments"], request["session_id"])

    def _complete(self, key: str, request: dict[str, Any], future: Future[Any]) -> None:
        try:
            response = {"id": request["id"], "ok": True, "result": future.result()}
        except Exception as exc:
            response = {
                "id": request["id"],
                "ok": False,
                "error": "Native Hermes request failed (" + type(exc).__name__ + ").",
            }
        try:
            self.reply(response)
        finally:
            with self._condition:
                self._active[key].remove(request)
                self._running -= 1
                if not self._active[key]:
                    del self._active[key]
                if not self._queues[key] and key not in self._active:
                    del self._queues[key]
                self._condition.notify_all()

    def close(self) -> None:
        with self._condition:
            self._closing = True
            self._condition.notify_all()
        self._dispatcher.join()
        self._executor.shutdown(wait=True)
