import asyncio
import json
import runpy
import socket
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from hermes2mcp.backend import HermesBackend

FIXTURE = Path(__file__).parent / "fixtures" / "hermes"


@asynccontextmanager
async def serve(app, *, startup_timeout=15):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    sock.setblocking(False)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(startup_timeout):
            while not server.started:
                if task.done():
                    await task
                    raise RuntimeError("HTTP server failed to start")
                await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 15)
        sock.close()


@pytest.mark.asyncio
async def test_real_http_connection_preserves_tools_and_native_session_identity():
    from hermes2mcp.server import create_app

    backend = HermesBackend(python=sys.executable, hermes_path=FIXTURE)
    app = create_app(backend)
    async with serve(app) as url:
        async with streamable_http_client(url) as streams:
            async with ClientSession(*streams) as first:
                await first.initialize()
                tools = (await first.list_tools()).tools
                expected = runpy.run_path(str(FIXTURE / "model_tools.py"))["DEFINITION"]["function"]
                assert [tool.name for tool in tools] == [expected["name"]]
                assert tools[0].description == expected["description"]
                assert tools[0].input_schema == expected["parameters"]
                one = json.loads(
                    (await first.call_tool("fixture_echo", {"value": "exact"})).content[0].text
                )
                two = json.loads(
                    (await first.call_tool("fixture_echo", {"value": None})).content[0].text
                )
                async with streamable_http_client(url) as other_streams:
                    async with ClientSession(*other_streams) as other:
                        await other.initialize()
                        three = json.loads(
                            (await other.call_tool("fixture_echo", {})).content[0].text
                        )
                assert one["arguments"] == {"value": "exact"}
                assert two["arguments"] == {"value": None}
                assert one["task_id"] == two["task_id"]
                assert one["task_id"] != three["task_id"]
    assert backend.running is False
