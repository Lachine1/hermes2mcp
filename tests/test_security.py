import pytest


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.0.2.10", "mcp.example.test"])
def test_non_loopback_binding_requires_an_explicit_bearer_token(host):
    from hermes2mcp.server import ServerSettings

    with pytest.raises(ValueError, match="token"):
        ServerSettings(host=host)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_binding_does_not_require_a_token(host):
    from hermes2mcp.server import ServerSettings

    assert ServerSettings(host=host).token is None


@pytest.mark.parametrize("token", ["", " ", "has spaces", "café", "line\r\nbreak", b"bytes", True])
def test_configured_bearer_token_must_be_nonempty_and_header_safe(token):
    from hermes2mcp.server import ServerSettings

    with pytest.raises(ValueError, match="bearer token"):
        ServerSettings(token=token)


def test_bearer_token_supports_the_rfc6750_token_alphabet_without_exposing_it_in_repr():
    from hermes2mcp.server import ServerSettings

    token = "test_TOKEN.~+/=="
    settings = ServerSettings(token=token)
    assert settings.token == token
    assert token not in repr(settings)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["POST", "GET", "DELETE"])
async def test_bearer_token_protects_every_mcp_http_method(method):
    import sys
    from pathlib import Path

    import httpx2
    from test_http import serve

    from hermes2mcp.backend import HermesBackend
    from hermes2mcp.server import ServerSettings, create_app

    fixture = Path(__file__).parent / "fixtures" / "hermes"
    app = create_app(
        HermesBackend(python=sys.executable, hermes_path=fixture),
        ServerSettings(token="only-this-test-token"),
    )
    async with serve(app) as url:
        async with httpx2.AsyncClient() as client:
            response = await client.request(method, url)
            assert response.status_code == 401
            assert "Bearer" in response.headers["www-authenticate"]
            response = await client.request(method, url, headers={"Authorization": "Bearer wrong"})
            assert response.status_code == 401
            response = await client.request(
                method, url, headers={"Authorization": "Bearer only-this-test-token"}
            )
            assert response.status_code != 401


@pytest.mark.asyncio
async def test_configured_host_and_origin_are_used_by_dns_rebinding_protection():
    import sys
    from pathlib import Path

    import httpx2
    from test_http import serve

    from hermes2mcp.backend import HermesBackend
    from hermes2mcp.server import ServerSettings, create_app

    fixture = Path(__file__).parent / "fixtures" / "hermes"
    settings = ServerSettings(
        token="dns-test-token",
        allowed_hosts=("mcp.example.test",),
        allowed_origins=("https://mcp.example.test",),
    )
    app = create_app(HermesBackend(python=sys.executable, hermes_path=fixture), settings)
    headers = {
        "Host": "mcp.example.test",
        "Origin": "https://mcp.example.test",
        "Authorization": "Bearer dns-test-token",
        "Accept": "application/json, text/event-stream",
    }
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "security-test", "version": "1"},
        },
    }
    async with serve(app) as url:
        async with httpx2.AsyncClient() as client:
            response = await client.post(url, headers=headers, json=initialize)
            assert response.status_code == 200
            response = await client.post(
                url,
                headers={**headers, "Origin": "https://evil.test"},
                json=initialize,
            )
            assert response.status_code == 403
            response = await client.post(
                url,
                headers={**headers, "Host": "evil.test"},
                json=initialize,
            )
            assert response.status_code == 421


@pytest.mark.asyncio
async def test_modern_stateless_discovery_succeeds_without_a_shared_native_session():
    import sys
    from pathlib import Path

    import httpx2
    from test_http import serve

    from hermes2mcp.backend import HermesBackend
    from hermes2mcp.server import create_app

    fixture = Path(__file__).parent / "fixtures" / "hermes"
    app = create_app(HermesBackend(python=sys.executable, hermes_path=fixture))
    async with serve(app) as url:
        async with httpx2.AsyncClient() as client:
            response = await client.post(
                url,
                headers={
                    "MCP-Protocol-Version": "2026-07-28",
                    "MCP-Method": "tools/list",
                    "Accept": "application/json, text/event-stream",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/list",
                    "params": {
                        "_meta": {
                            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                            "io.modelcontextprotocol/clientCapabilities": {},
                        },
                    },
                },
            )
            assert response.status_code == 200
            assert response.json()["result"]["tools"][0]["name"] == "fixture_echo"
            assert "mcp-session-id" not in response.headers


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", [None, "2025-11-25"])
async def test_context_header_cannot_bypass_auth_by_omitting_the_modern_protocol(protocol):
    import sys

    import httpx2
    from test_http import FIXTURE, serve

    from hermes2mcp.backend import HermesBackend
    from hermes2mcp.server import create_app

    headers = {
        "Accept": "application/json, text/event-stream",
        "X-Hermes-Context": "ABCDEF0123456789abcdef0123456789",
        "Authorization": "Bearer arbitrary-test-token",
    }
    if protocol is not None:
        headers["MCP-Protocol-Version"] = protocol
    async with serve(create_app(HermesBackend(python=sys.executable, hermes_path=FIXTURE))) as url:
        async with httpx2.AsyncClient(timeout=10) as http:
            response = await http.post(
                url,
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "context-auth-test", "version": "1"},
                    },
                },
            )
            assert response.status_code == 401, response.text
            assert response.headers["www-authenticate"] == "Bearer"
