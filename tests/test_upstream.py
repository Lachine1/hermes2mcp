"""Native upstream discovery against a test-only local MCP server, not a vendor mock."""

import asyncio
import json

import pytest
from mcp import types
from mcp.server.lowlevel import Server
from test_http import serve
from test_native import PYTHON, native_backend

pytestmark = [pytest.mark.hermes, pytest.mark.skipif(not PYTHON, reason="real Hermes not selected")]


@pytest.mark.asyncio
async def test_upstream_discovery_is_opt_in_and_uses_the_original_hermes_client(tmp_path):
    schema = {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]}

    async def list_tools(ctx, params):
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name="upstream_echo",
                    description="Local upstream integration fixture.",
                    input_schema=schema,
                )
            ]
        )

    async def call_tool(ctx, params):
        return types.CallToolResult(
            content=[
                types.TextContent(
                    type="text",
                    text="local-result: " + params.arguments["value"],
                )
            ]
        )

    upstream = Server("local-upstream-fixture", on_list_tools=list_tools, on_call_tool=call_tool)
    upstream_app = upstream.streamable_http_app(json_response=True)
    requests = []

    async def track_requests(scope, receive, send):
        if scope["type"] == "http":
            requests.append(scope["method"])
        await upstream_app(scope, receive, send)

    async with serve(track_requests) as url:
        profile = tmp_path / "profile"
        profile.mkdir()
        (profile / "config.yaml").write_text(
            "mcp_servers:\n  local_fixture:\n    url: "
            + json.dumps(url)
            + "\n    connect_timeout: 5\n    sampling:\n      enabled: false\n",
            encoding="utf-8",
        )
        async with native_backend(profile, toolsets=None) as off:
            assert not any(
                "upstream_echo" in d["function"]["name"] for d in await off.definitions()
            )
            assert requests == []
        backend = native_backend(profile, toolsets=None, include_upstream=True)
        async with backend:
            exported = [
                d["function"]
                for d in await backend.definitions()
                if "upstream_echo" in d["function"]["name"]
            ]
            assert len(exported) == 1, exported
            assert exported[0]["parameters"] == schema
            result = await backend.call(exported[0]["name"], {"value": "proof"}, "client")
            assert "local-result: proof" in result, result
            assert requests
        assert not backend.running
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_upstream_discovery_cannot_connect_to_the_configured_http_bind_address(tmp_path):
    from urllib.parse import urlsplit

    from hermes2mcp.server import ServerSettings, create_app

    async def list_tools(ctx, params):
        return types.ListToolsResult(tools=[])

    upstream = Server("self-connection-fixture", on_list_tools=list_tools)
    upstream_app = upstream.streamable_http_app(json_response=True)
    requests = []

    async def track_requests(scope, receive, send):
        if scope["type"] == "http":
            requests.append(scope["method"])
        await upstream_app(scope, receive, send)

    async with serve(track_requests) as url:
        profile = tmp_path / "profile"
        profile.mkdir()
        (profile / "config.yaml").write_text(
            "mcp_servers:\n  self_fixture:\n    url: "
            + json.dumps(url)
            + "\n    connect_timeout: 5\n    sampling:\n      enabled: false\n",
            encoding="utf-8",
        )
        backend = native_backend(profile, toolsets=None, include_upstream=True)
        create_app(backend, ServerSettings(port=urlsplit(url).port))
        async with backend:
            assert requests == [], "The native worker connected to its own configured endpoint"
