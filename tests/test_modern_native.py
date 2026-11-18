"""Run the original native tools with the current, non-legacy MCP client."""

import asyncio
import json
from pathlib import Path

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from test_http import serve
from test_native import PYTHON, SOURCE, native_backend

from hermes2mcp.backend import HermesBackend
from hermes2mcp.server import ServerSettings, create_app

pytestmark = [pytest.mark.hermes, pytest.mark.skipif(not PYTHON, reason="real Hermes not selected")]


class ObservedNativeBackend(HermesBackend):
    """Record original outcomes and cleanup without replacing native execution."""

    def __init__(self, home, toolsets=("todo",)):
        super().__init__(
            python=PYTHON,
            hermes_path=Path(SOURCE) if SOURCE else None,
            hermes_home=home,
            toolsets=list(toolsets) if toolsets is not None else None,
        )
        self.outcomes = []
        self.released = []
        self.release_observed = asyncio.Event()

    async def call(self, name, arguments, session_id):
        result = await super().call(name, arguments, session_id)
        self.outcomes.append((name, arguments, session_id, result))
        return result

    async def release(self, session_id):
        await super().release(session_id)
        self.released.append(session_id)
        self.release_observed.set()

    async def wait_for_releases(self, count):
        async with asyncio.timeout(30):
            while len(self.released) < count:
                self.release_observed.clear()
                await self.release_observed.wait()


@pytest.mark.asyncio
async def test_current_protocol_client_executes_original_native_read_file(tmp_path):
    sample = tmp_path / "modern-client.txt"
    sample.write_text("Modern MCP native read: café ☃\n", encoding="utf-8")
    backend = native_backend(tmp_path / "profile", toolsets=("file",))
    async with serve(create_app(backend), startup_timeout=90) as url:
        async with Client(url, mode="2026-07-28", read_timeout_seconds=20) as client:
            assert client.protocol_version == "2026-07-28"
            tools = await client.list_tools()
            assert "read_file" in {tool.name for tool in tools.tools}
            result = await client.call_tool("read_file", {"path": str(sample)})
            assert not result.is_error, result
            assert "Modern MCP native read: café ☃" in result.content[0].text
    assert not backend.running


@pytest.mark.asyncio
async def test_default_sdk_client_discovers_modern_and_unkeyed_native_todos_reset(tmp_path):
    backend = native_backend(tmp_path / "profile", toolsets=("todo",))
    todos = [{"id": "ephemeral", "content": "Must not leak", "status": "pending"}]
    async with serve(create_app(backend), startup_timeout=90) as url:
        # This is the actual Client(url) auto-discovery path, not a pinned protocol.
        async with Client(url, read_timeout_seconds=30) as client:
            assert client.protocol_version == "2026-07-28"
            assert "todo_list" in {tool.name for tool in (await client.list_tools()).tools}
            written = await client.call_tool("todo_list", {"todos": todos})
            assert not written.is_error, written
            assert json.loads(written.content[0].text)["todos"] == todos
            for _ in range(3):
                read = await client.call_tool("todo_list", {})
                assert not read.is_error, read
                assert json.loads(read.content[0].text)["todos"] == []
        async with Client(url, read_timeout_seconds=30) as other:
            assert other.protocol_version == "2026-07-28"
            read = await other.call_tool("todo_list", {})
            assert json.loads(read.content[0].text)["todos"] == []
    assert not backend.running


@pytest.mark.asyncio
async def test_authenticated_modern_context_retains_original_native_todos(tmp_path):
    backend = native_backend(tmp_path / "profile", toolsets=("todo",))
    todos = [{"id": "retained", "content": "Native context proof", "status": "pending"}]
    app = create_app(backend, ServerSettings(token="modern-test-token"))
    async with serve(app, startup_timeout=90) as url:
        async with httpx2.AsyncClient(
            headers={
                "Authorization": "Bearer modern-test-token",
                "X-Hermes-Context": "ABCDEF0123456789abcdef0123456789",
            },
            timeout=30,
        ) as http:
            async with Client(
                streamable_http_client(url, http_client=http), read_timeout_seconds=30
            ) as client:
                assert client.protocol_version == "2026-07-28"
                written = await client.call_tool("todo_list", {"todos": todos})
                assert not written.is_error, written
                assert json.loads(written.content[0].text)["todos"] == todos
                stored = await client.call_tool("todo_list", {})
                assert not stored.is_error, stored
                assert json.loads(stored.content[0].text)["todos"] == todos
    assert not backend.running


@pytest.mark.asyncio
async def test_explicit_modern_context_release_resets_original_native_todos(tmp_path):
    backend = native_backend(tmp_path / "profile", toolsets=("todo",))
    todos = [{"id": "delete", "content": "Explicit release proof", "status": "pending"}]
    app = create_app(backend, ServerSettings(token="modern-test-token"))
    key = "ABCDEF0123456789abcdef0123456789"
    async with serve(app, startup_timeout=90) as url:
        async with httpx2.AsyncClient(
            headers={"Authorization": "Bearer modern-test-token", "X-Hermes-Context": key},
            timeout=30,
        ) as http:
            async with Client(
                streamable_http_client(url, http_client=http), read_timeout_seconds=30
            ) as first:
                assert first.protocol_version == "2026-07-28"
                written = await first.call_tool("todo_list", {"todos": todos})
                assert json.loads(written.content[0].text)["todos"] == todos
            # Modern SDK closure sends no DELETE: the key remains deliberately reusable.
            async with Client(
                streamable_http_client(url, http_client=http), read_timeout_seconds=30
            ) as resumed:
                stored = await resumed.call_tool("todo_list", {})
                assert json.loads(stored.content[0].text)["todos"] == todos
                released = await http.delete(url + "/context")
                assert released.status_code == 202, released.text
                absent = await http.delete(url + "/context")
                assert absent.status_code == 204, absent.text
                read = await resumed.call_tool("todo_list", {})
                assert not read.is_error, read
                assert json.loads(read.content[0].text)["todos"] == []
    assert not backend.running


@pytest.mark.asyncio
async def test_modern_full_catalog_and_sequential_concurrent_native_reads_are_lossless(tmp_path):
    samples = []
    for number in range(7):
        sample = tmp_path / f"native-modern-{number}.txt"
        sample.write_text(f"Original modern native read {number}: café ☃\n", encoding="utf-8")
        samples.append(sample)
    backend = ObservedNativeBackend(tmp_path / "profile", toolsets=None)
    app = create_app(backend, ServerSettings(token="modern-test-token"))
    async with serve(app, startup_timeout=90) as url:
        native = {d["function"]["name"]: d["function"] for d in await backend.definitions()}
        assert {"read_file", "todo_list", "memory", "terminal", "execute_code"} <= native.keys()
        async with httpx2.AsyncClient(
            headers={"Authorization": "Bearer modern-test-token"}, timeout=60
        ) as http:
            async with Client(
                streamable_http_client(url, http_client=http), read_timeout_seconds=60
            ) as client:
                assert client.protocol_version == "2026-07-28"
                exported = {
                    tool.name: tool.model_dump(by_alias=True, exclude_none=True)
                    for tool in (await client.list_tools()).tools
                }
                assert exported.keys() == native.keys()
                for name, definition in native.items():
                    assert exported[name].get("description") == definition.get("description")
                    assert exported[name]["inputSchema"] == definition["parameters"]
                results = []
                for sample in samples[:3]:
                    results.append(await client.call_tool("read_file", {"path": str(sample)}))
                results.extend(
                    await asyncio.gather(
                        *(
                            client.call_tool("read_file", {"path": str(sample)})
                            for sample in samples[3:]
                        )
                    )
                )
                original = {
                    arguments["path"]: result
                    for name, arguments, session_id, result in backend.outcomes
                    if name == "read_file"
                }
                assert len(original) == len(samples)
                for number, (sample, result) in enumerate(zip(samples, results, strict=True)):
                    assert not result.is_error, result
                    assert result.content[0].text == original[str(sample)]
                    assert f"Original modern native read {number}: café ☃" in result.content[0].text
                sessions = [session_id for _, _, session_id, _ in backend.outcomes]
                assert len(set(sessions)) == len(samples)
                assert all(session.startswith("modern-request-") for session in sessions)
                await backend.wait_for_releases(len(samples))
                assert set(backend.released) == set(sessions)
                assert len(backend.released) == len(sessions)
    assert not backend.running


@pytest.mark.asyncio
async def test_case_distinct_modern_keys_never_alias_original_native_todo_stores(tmp_path):
    backend = ObservedNativeBackend(tmp_path / "profile")
    key = "ABCDEF0123456789abcdef0123456789"
    upper_todos = [{"id": "upper", "content": "Upper key state", "status": "pending"}]
    lower_todos = [{"id": "lower", "content": "Lower key state", "status": "completed"}]
    app = create_app(backend, ServerSettings(token="modern-test-token"))
    async with serve(app, startup_timeout=90) as url:
        auth = {"Authorization": "Bearer modern-test-token"}
        async with (
            httpx2.AsyncClient(headers={**auth, "X-Hermes-Context": key}, timeout=60) as upper_http,
            httpx2.AsyncClient(
                headers={**auth, "X-Hermes-Context": key.lower()}, timeout=60
            ) as lower_http,
        ):
            async with (
                Client(
                    streamable_http_client(url, http_client=upper_http), read_timeout_seconds=60
                ) as upper,
                Client(
                    streamable_http_client(url, http_client=lower_http), read_timeout_seconds=60
                ) as lower,
            ):
                assert upper.protocol_version == lower.protocol_version == "2026-07-28"
                assert (
                    json.loads(
                        (await upper.call_tool("todo_list", {"todos": upper_todos})).content[0].text
                    )["todos"]
                    == upper_todos
                )
                empty = await lower.call_tool("todo_list", {})
                assert json.loads(empty.content[0].text)["todos"] == []
                assert (
                    json.loads(
                        (await lower.call_tool("todo_list", {"todos": lower_todos})).content[0].text
                    )["todos"]
                    == lower_todos
                )
                for _ in range(3):
                    upper_read, lower_read = await asyncio.gather(
                        upper.call_tool("todo_list", {}), lower.call_tool("todo_list", {})
                    )
                    assert json.loads(upper_read.content[0].text)["todos"] == upper_todos
                    assert json.loads(lower_read.content[0].text)["todos"] == lower_todos
                sessions = {session for _, _, session, _ in backend.outcomes}
                assert len(sessions) == 2
                assert backend.released == []
    assert set(backend.released) == sessions
    assert not backend.running


@pytest.mark.asyncio
async def test_idle_http_reaper_cleans_original_native_todos_without_a_followup_request(tmp_path):
    backend = ObservedNativeBackend(tmp_path / "profile")
    todos = [{"id": "idle", "content": "Expire native state", "status": "pending"}]
    settings = ServerSettings(token="modern-test-token", max_contexts=1, context_idle_timeout=0.15)
    async with serve(create_app(backend, settings), startup_timeout=90) as url:
        async with httpx2.AsyncClient(
            headers={
                "Authorization": "Bearer modern-test-token",
                "X-Hermes-Context": "ABCDEF0123456789abcdef0123456789",
            },
            timeout=60,
        ) as http:
            async with Client(
                streamable_http_client(url, http_client=http), read_timeout_seconds=60
            ) as client:
                written = await client.call_tool("todo_list", {"todos": todos})
                assert json.loads(written.content[0].text)["todos"] == todos
                old_session = backend.outcomes[-1][2]
                await backend.wait_for_releases(1)
                assert backend.released == [old_session]
                read = await client.call_tool("todo_list", {})
                assert not read.is_error, read
                assert json.loads(read.content[0].text)["todos"] == []
                assert backend.outcomes[-1][2] != old_session
    assert not backend.running
