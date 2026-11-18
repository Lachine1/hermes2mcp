"""Run unmodified Hermes tools in their own prepared Python environment."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Any


class BackendError(RuntimeError):
    """The native Hermes worker could not complete a request."""


class HermesBackend:
    """One native runtime with correlated, non-blocking private RPC requests."""

    def __init__(
        self,
        *,
        python: str = sys.executable,
        hermes_path: Path | None = None,
        hermes_home: Path | None = None,
        include_upstream: bool = False,
        toolsets: list[str] | None = None,
    ):
        self.python = python
        self.hermes_path = hermes_path
        self.hermes_home = hermes_home
        self.include_upstream = include_upstream
        self.toolsets = toolsets
        self.self_urls: tuple[str, ...] = ()
        self._process: subprocess.Popen[str] | None = None
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._reader: asyncio.Task[None] | None = None
        self._reader_error: BackendError | None = None
        self._write_lock = threading.Lock()
        self._start_lock = asyncio.Lock()
        self._closed = False

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    async def __aenter__(self) -> HermesBackend:
        await self.start()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def start(self) -> None:
        async with self._start_lock:
            if self._closed:
                raise BackendError("A closed Hermes backend cannot be restarted.")
            if self.running:
                return
            command = [self.python, "-u", str(Path(__file__).with_name("worker.py"))]
            if self.hermes_path is not None:
                command += ["--hermes-path", str(self.hermes_path)]
            if self.include_upstream:
                command += ["--include-upstream"]
            for url in self.self_urls:
                command += ["--self-url", url]
            if self.toolsets is not None:
                for toolset in self.toolsets:
                    command += ["--toolset", toolset]
            environment = dict(os.environ)
            if self.hermes_home is not None:
                environment["HERMES_HOME"] = str(self.hermes_home)
            environment.pop("HERMES2MCP_TOKEN", None)
            environment["PYTHONIOENCODING"] = "utf-8"
            try:
                self._process = await asyncio.to_thread(
                    subprocess.Popen,
                    command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=None,
                    text=True,
                    encoding="utf-8",
                    bufsize=1,
                    env=environment,
                )
                self._reader = asyncio.create_task(self._read_responses())
                await self.definitions()
            except BaseException:
                await self.close()
                raise

    def _fail_pending(self, error: BackendError) -> None:
        self._reader_error = error
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(error)

    async def _read_responses(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        try:
            while line := await asyncio.to_thread(self._process.stdout.readline):
                try:
                    response = json.loads(line)
                except ValueError as exc:
                    raise BackendError("Invalid private worker response.") from exc
                if not isinstance(response, dict):
                    raise BackendError("Invalid private worker response.")
                identity = response.get("id")
                if identity is None:
                    raise BackendError(response.get("error", "Invalid private worker response."))
                future = self._pending.get(identity)
                if future is None or future.done():
                    continue  # A cancelled caller cannot consume another caller's response.
                if response.get("ok"):
                    future.set_result(response["result"])
                else:
                    future.set_exception(
                        BackendError(response.get("error", "Native Hermes request failed."))
                    )
            raise BackendError("Hermes worker exited. Use Hermes's prepared Python interpreter.")
        except (BackendError, OSError, ValueError, KeyError) as exc:
            error = (
                exc
                if isinstance(exc, BackendError)
                else BackendError("Lost connection to the Hermes worker.")
            )
            self._fail_pending(error)

    def _send(self, payload: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.poll() is not None:
            raise BackendError("Hermes worker is not running. Check --hermes-python.")
        assert process.stdin is not None
        with self._write_lock:
            try:
                process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
                process.stdin.flush()
            except (OSError, ValueError) as exc:
                raise BackendError("Lost connection to the Hermes worker.") from exc

    async def _request(self, payload: dict[str, Any]) -> Any:
        if self._closed:
            raise BackendError("Hermes backend is closed.")
        if self._reader_error is not None:
            raise self._reader_error
        identity = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self._pending[identity] = future
        write = asyncio.create_task(asyncio.to_thread(self._send, {**payload, "id": identity}))
        try:
            await asyncio.shield(write)
            return await future
        except asyncio.CancelledError:
            if payload["op"] == "call" and self.running and not self._closed:
                try:
                    await asyncio.shield(write)  # Cancellation cannot overtake its request packet.
                    await asyncio.shield(self._request({"op": "cancel", "target_id": identity}))
                except BackendError:
                    pass
            raise
        finally:
            self._pending.pop(identity, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    async def definitions(self) -> list[dict[str, Any]]:
        return await self._request({"op": "list"})

    async def call(self, name: str, arguments: dict[str, Any], session_id: str) -> Any:
        return await self._request(
            {
                "op": "call",
                "name": name,
                "arguments": arguments,
                "session_id": session_id,
            }
        )

    async def release(self, session_id: str) -> None:
        if self.running and not self._closed:
            await self._request({"op": "release", "session_id": session_id})

    def _close_input(self) -> None:
        if self._process is not None and self._process.stdin is not None:
            with self._write_lock:
                try:
                    self._process.stdin.close()
                except (OSError, ValueError):
                    pass

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._fail_pending(BackendError("Hermes backend is closed."))
        process = self._process
        if process is not None:
            await asyncio.to_thread(self._close_input)
            try:
                await asyncio.to_thread(process.wait, timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    await asyncio.to_thread(process.wait, timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    await asyncio.to_thread(process.wait)
            if self._reader is not None:
                await self._reader
            if process.stdout is not None:
                process.stdout.close()
