"""Whole native catalog and byte-exact native file results through real authenticated HTTP."""

import asyncio
import json
import os
import subprocess

import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from test_http import serve
from test_native import PYTHON, SOURCE, native_backend

from hermes2mcp.server import ServerSettings, create_app

pytestmark = [pytest.mark.hermes, pytest.mark.skipif(not PYTHON, reason="real Hermes not selected")]


@pytest.mark.asyncio
async def test_complete_native_catalog_and_original_file_result_over_authenticated_http(tmp_path):
    sample = tmp_path / "sample.txt"
    sample.write_text("Native file result: café ☃\nSecond line.\n", encoding="utf-8")
    profile = tmp_path / "profile"
    backend = native_backend(profile, toolsets=None)
    app = create_app(backend, ServerSettings(token="local-http-verification-token"))
    async with serve(app, startup_timeout=90) as url:
        native = {d["function"]["name"]: d["function"] for d in await backend.definitions()}
        assert {"read_file", "terminal", "memory", "todo_list", "execute_code"} <= native.keys()
        async with httpx2.AsyncClient(
            headers={
                "Authorization": "Bearer local-http-verification-token",
            }
        ) as http:
            async with streamable_http_client(url, http_client=http) as streams:
                async with ClientSession(*streams) as client:
                    await client.initialize()
                    exported = {
                        tool.name: tool.model_dump(by_alias=True, exclude_none=True)
                        for tool in (await client.list_tools()).tools
                    }
                    assert exported.keys() == native.keys()
                    for name, definition in native.items():
                        assert exported[name]["description"] == definition["description"]
                        assert exported[name]["inputSchema"] == definition["parameters"]
                    result = await client.call_tool("read_file", {"path": str(sample)})
                    assert not result.is_error, result
                    env = dict(os.environ)
                    env["HERMES_HOME"] = str(profile)
                    if SOURCE:
                        env["PYTHONPATH"] = SOURCE
                    script = """
import contextlib, json, sys
with contextlib.redirect_stdout(sys.stderr):
    import model_tools
    result = model_tools.handle_function_call(
        "read_file", {"path": sys.argv[1]}, task_id="direct-baseline",
        session_id="direct-baseline", enabled_tools=["read_file"],
    )
print(json.dumps(result, ensure_ascii=False))
"""
                    baseline = await asyncio.to_thread(
                        subprocess.run,
                        [PYTHON, "-c", script, str(sample)],
                        env=env,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        timeout=60,
                    )
                    assert baseline.returncode == 0, baseline.stderr
                    assert result.content[0].text == json.loads(baseline.stdout)
                    memory = await client.call_tool(
                        "memory",
                        {
                            "action": "add",
                            "target": "memory",
                            "content": "Adapter verification note.",
                        },
                    )
                    assert not memory.is_error, memory
                    assert any(
                        "Adapter verification note." in path.read_text(encoding="utf-8")
                        for path in profile.rglob("MEMORY.md")
                    )
    assert not backend.running
