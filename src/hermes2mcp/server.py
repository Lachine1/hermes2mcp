"""HTTP-only, lossless MCP adapter for the live Hermes tool catalog."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import (
    StreamableHTTPASGIApp,
    StreamableHTTPSessionManager,
)
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import __version__
from .backend import BackendError, HermesBackend
from .bridge import result_from_hermes, tool_from_definition

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ServerSettings:
    """Safe HTTP bind configuration; secrets are excluded from repr."""

    host: str = "127.0.0.1"
    port: int = 8000
    token: str | None = field(default=None, repr=False)
    allowed_hosts: tuple[str, ...] = ()
    allowed_origins: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        try:
            loopback = ipaddress.ip_address(self.host).is_loopback
        except ValueError:
            loopback = self.host == "localhost"
        if not loopback and not self.token:
            raise ValueError("A bearer token is required for non-loopback HTTP binding.")


class HTTPGuard:
    """Authenticate every HTTP request before it reaches the MCP session manager."""

    def __init__(self, app: Any, token: str | None):
        self.app = app
        self.token = token

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http" and self.token is not None:
            supplied = Headers(scope=scope).get("authorization", "")
            expected = "Bearer " + self.token
            if not secrets.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
                await JSONResponse(
                    {"error": "Bearer authentication required."},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )(scope, receive, send)
                return
        if (
            scope["type"] == "http"
            and Headers(scope=scope).get("mcp-protocol-version") == "2026-07-28"
        ):
            await JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": types.INVALID_REQUEST,
                        "message": "A stateful MCP HTTP session is required; "
                        "use the legacy HTTP client mode.",
                    },
                },
                status_code=400,
            )(scope, receive, send)
            return
        await self.app(scope, receive, send)


class NativeHTTPSessions(StreamableHTTPSessionManager):
    """Attach native cleanup to the SDK 2.3 session disposal path."""

    def __init__(self, server: Server, backend: HermesBackend, **kwargs: Any):
        super().__init__(server, **kwargs)
        self.backend = backend
        self.cleanup_jobs: set[asyncio.Task[None]] = set()

    async def _discard_session(self, session_id: str, transport: Any) -> None:
        registered = self._server_instances.get(session_id) is transport
        await super()._discard_session(session_id, transport)
        if registered:
            task = asyncio.create_task(self.backend.release(session_id))
            self.cleanup_jobs.add(task)
            task.add_done_callback(self._cleanup_done)

    def _cleanup_done(self, task: asyncio.Task[None]) -> None:
        self.cleanup_jobs.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.warning("Native session cleanup failed.")

    async def finish_cleanup(self) -> None:
        if self.cleanup_jobs:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self.cleanup_jobs, return_exceptions=True),
                    timeout=5,
                )
            except TimeoutError:
                pass  # The bounded backend shutdown handles a still-busy worker.


def create_app(backend: HermesBackend, settings: ServerSettings | None = None) -> Starlette:
    """Create one stateful Streamable HTTP endpoint at ``/mcp``."""
    settings = settings or ServerSettings()
    bind_host = f"[{settings.host}]" if ":" in settings.host else settings.host
    backend.self_urls = (
        *backend.self_urls,
        f"http://{bind_host}:{settings.port}/mcp",
        f"http://127.0.0.1:{settings.port}/mcp",
        f"http://localhost:{settings.port}/mcp",
        f"http://[::1]:{settings.port}/mcp",
    )

    async def list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
        definitions = await backend.definitions()
        return types.ListToolsResult(tools=[tool_from_definition(d) for d in definitions])

    async def call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        definitions = await backend.definitions()
        names = {definition.get("function", definition)["name"] for definition in definitions}
        if params.name not in names:
            raise MCPError(types.INVALID_PARAMS, "Tool is not in the active Hermes catalog.")
        request = ctx.request
        session = request.headers.get("mcp-session-id") if request is not None else None
        if not session:
            raise MCPError(types.INVALID_PARAMS, "A stateful MCP HTTP session is required.")
        try:
            result = await backend.call(params.name, params.arguments or {}, session)
        except BackendError:
            return result_from_hermes(json.dumps({"error": "Native Hermes execution failed."}))
        return result_from_hermes(result)

    server = Server(
        "hermes2mcp",
        version=__version__,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
        instructions="Hermes Agent's original tool catalog, names, descriptions, "
        "and input schemas.",
    )
    manager = NativeHTTPSessions(
        server,
        backend,
        json_response=True,
        stateless=False,
        max_sessions=128,
        security_settings=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", *settings.allowed_hosts],
            allowed_origins=[
                "http://127.0.0.1:*",
                "http://localhost:*",
                "http://[::1]:*",
                *settings.allowed_origins,
            ],
        ),
    )

    @asynccontextmanager
    async def lifespan(app: Starlette):
        async with backend:
            try:
                async with manager.run():
                    yield
            finally:
                await manager.finish_cleanup()

    app = Starlette(
        routes=[Route("/mcp", endpoint=StreamableHTTPASGIApp(manager))],
        lifespan=lifespan,
    )
    app.add_middleware(HTTPGuard, token=settings.token)
    app.state.backend = backend
    return app
