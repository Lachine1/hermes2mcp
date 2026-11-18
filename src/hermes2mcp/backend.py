"""Run unmodified Hermes tools in their own prepared Python environment."""

from __future__ import annotations

import asyncio
import functools
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any


class BackendError(RuntimeError):
    """The native Hermes worker could not complete a request."""


class HermesBackend:
    """One long-lived native runtime; serialized calls never block the HTTP event loop."""

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
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hermes2mcp")
        self._lock = asyncio.Lock()
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
            await self.definitions()
        except BaseException:
            await self.close()
            raise

    def _exchange(self, payload: dict[str, Any]) -> Any:
        process = self._process
        if process is None or process.poll() is not None:
            raise BackendError("Hermes worker is not running. Check --hermes-python.")
        assert process.stdin is not None and process.stdout is not None
        try:
            process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            process.stdin.flush()
            line = process.stdout.readline()
        except (OSError, ValueError) as exc:
            raise BackendError("Lost connection to the Hermes worker.") from exc
        if not line:
            raise BackendError("Hermes worker exited. Use Hermes's prepared Python interpreter.")
        try:
            response = json.loads(line)
        except ValueError as exc:
            raise BackendError("Invalid private worker response.") from exc
        if not response.get("ok"):
            raise BackendError(response.get("error", "Native Hermes request failed."))
        return response["result"]

    async def _request(self, payload: dict[str, Any]) -> Any:
        async with self._lock:
            return await asyncio.get_running_loop().run_in_executor(
                self._executor,
                functools.partial(self._exchange, payload),
            )

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
        if self.running:
            await self._request({"op": "release", "session_id": session_id})

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        process = self._process
        if process is not None:
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            try:
                await asyncio.to_thread(process.wait, timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    await asyncio.to_thread(process.wait, timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    await asyncio.to_thread(process.wait)
            if process.stdout is not None:
                process.stdout.close()
        self._executor.shutdown(wait=False, cancel_futures=True)
