import json
import os
from pathlib import Path

import pytest

from hermes2mcp.backend import HermesBackend

PYTHON = os.environ.get("HERMES2MCP_HERMES_PYTHON")
SOURCE = os.environ.get("HERMES2MCP_HERMES_PATH")
pytestmark = [pytest.mark.hermes, pytest.mark.skipif(not PYTHON, reason="real Hermes not selected")]


def native_backend(
    home,
    toolsets=("todo", "memory", "session_search", "delegation", "file", "terminal"),
    *,
    include_upstream=False,
):
    return HermesBackend(
        python=PYTHON,
        hermes_path=Path(SOURCE) if SOURCE else None,
        hermes_home=home,
        toolsets=list(toolsets) if toolsets is not None else None,
        include_upstream=include_upstream,
    )


@pytest.mark.asyncio
async def test_native_todo_tool_uses_its_real_store_and_isolates_http_sessions(tmp_path):
    # Native tool tests use a disposable profile, never the user's memories or session DB.
    async with native_backend(tmp_path / "profile") as backend:
        tools = await backend.definitions()
        assert "todo_list" in {d["function"]["name"] for d in tools}
        todos = [{"id": "proof", "content": "Native Hermes tool", "status": "pending"}]
        written = json.loads(await backend.call("todo_list", {"todos": todos}, "first"))
        assert written.get("todos") == todos, written
        assert json.loads(await backend.call("todo_list", {}, "first"))["todos"] == todos
        assert json.loads(await backend.call("todo_list", {}, "other"))["todos"] == []
        await backend.release("first")
        assert json.loads(await backend.call("todo_list", {}, "first"))["todos"] == []


@pytest.mark.asyncio
async def test_native_session_search_opens_a_real_disposable_session_database(tmp_path):
    async with native_backend(tmp_path / "profile") as backend:
        result = json.loads(
            await backend.call("session_search", {"query": "no matches", "limit": 1}, "reader")
        )
        assert "error" not in result, result
        assert (tmp_path / "profile" / "state.db").is_file()


@pytest.mark.asyncio
async def test_native_delegation_status_uses_original_agent_dispatch_without_an_llm_call(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "config.yaml").write_text(
        "model:\n  provider: custom\n  default: test-only-model\n"
        "  base_url: http://127.0.0.1:1/v1\n  context_length: 65536\n",
        encoding="utf-8",
    )
    async with native_backend(profile) as backend:
        result = json.loads(await backend.call("delegate_task", {"action": "list"}, "parent"))
        assert "error" not in result, result


@pytest.mark.asyncio
async def test_deleting_an_http_session_releases_its_native_todo_store(tmp_path):
    import asyncio

    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from test_http import serve

    from hermes2mcp.server import ServerSettings, create_app

    session_ids = set()

    async def capture_session(response):
        session_id = response.headers.get("mcp-session-id")
        if session_id:
            session_ids.add(session_id)

    backend = native_backend(tmp_path / "profile", toolsets=("todo",))
    app = create_app(backend, ServerSettings(token="native-test-token"))
    async with serve(app, startup_timeout=90) as url:
        async with httpx2.AsyncClient(
            headers={"Authorization": "Bearer native-test-token"},
            event_hooks={"response": [capture_session]},
        ) as http:
            async with streamable_http_client(url, http_client=http) as streams:
                async with ClientSession(*streams) as client:
                    await client.initialize()
                    result = await client.call_tool(
                        "todo_list",
                        {
                            "todos": [
                                {
                                    "id": "one",
                                    "content": "Delete-session proof",
                                    "status": "pending",
                                }
                            ],
                        },
                    )
                    assert not result.is_error, result
            assert len(session_ids) == 1
            session_id = session_ids.pop()
            response = await http.post(
                url,
                headers={"mcp-session-id": session_id},
                json={
                    "jsonrpc": "2.0",
                    "id": 20,
                    "method": "ping",
                },
            )
            assert response.status_code == 404
            for _ in range(50):
                stored = json.loads(await backend.call("todo_list", {}, session_id))["todos"]
                if stored == []:
                    break
                await asyncio.sleep(0.02)
            assert stored == [], "Deleted HTTP session retained its native todo state"
