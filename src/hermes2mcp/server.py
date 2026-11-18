"""HTTP-only, lossless MCP adapter for the live Hermes tool catalog."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
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
from mcp.server.transport_security import TransportSecurityMiddleware, TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from . import __version__
from .backend import BackendError, HermesBackend
from .bridge import result_from_hermes, tool_from_definition
from .contexts import ContextLimitError, NativeContexts, validate_context_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ServerSettings:
    """Safe HTTP bind configuration; secrets are excluded from repr."""

    host: str = "127.0.0.1"
    port: int = 8000
    token: str | None = field(default=None, repr=False)
    allowed_hosts: tuple[str, ...] = ()
    allowed_origins: tuple[str, ...] = ()
    max_contexts: int = 128
    context_idle_timeout: float = 1800

    def __post_init__(self) -> None:
        validate_context_settings(self.max_contexts, self.context_idle_timeout)
        if self.token is not None and (
            not isinstance(self.token, str)
            or re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", self.token) is None
        ):
            raise ValueError("A configured bearer token must be nonempty RFC 6750 token text.")
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
        if scope["type"] == "http":
            headers = Headers(scope=scope)
            keyed = headers.get("x-hermes-context") is not None
            supplied = headers.get("authorization", "")
            expected = "Bearer " + self.token if self.token is not None else None
            if (self.token is not None or keyed or scope["path"] == "/mcp/context") and (
                expected is None
                or not secrets.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))
            ):
                await JSONResponse(
                    {"error": "Bearer authentication required."},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )(scope, receive, send)
                return
            keys = headers.getlist("x-hermes-context")
            if keys and (len(keys) != 1 or re.fullmatch(r"[0-9A-Fa-f]{32}", keys[0]) is None):
                await JSONResponse(
                    {"error": "X-Hermes-Context must be exactly 32 ASCII hex characters."},
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

    @asynccontextmanager
    async def run(self):
        remaining: tuple[str, ...] = ()
        try:
            async with super().run():
                try:
                    yield
                finally:
                    # SDK shutdown clears its registry before server tasks
                    # discard themselves, so preserve the remaining IDs.
                    remaining = tuple(self._server_instances)
        finally:
            for session_id in remaining:
                self.schedule_cleanup(session_id)

    async def _discard_session(self, session_id: str, transport: Any) -> None:
        registered = self._server_instances.get(session_id) is transport
        await super()._discard_session(session_id, transport)
        if registered:
            self.schedule_cleanup(session_id)

    def schedule_cleanup(self, session_id: str) -> asyncio.Task[None]:
        task = asyncio.create_task(self.backend.release(session_id))
        self.cleanup_jobs.add(task)
        task.add_done_callback(self._cleanup_done)
        return task

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
    """Create the modern and legacy Streamable HTTP endpoint at ``/mcp``."""
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
        modern = ctx.protocol_version == "2026-07-28"
        request = ctx.request
        context_key = request.headers.get("x-hermes-context") if request is not None else None
        if modern:
            if context_key is None:
                session = "modern-request-" + secrets.token_hex(16)
            else:
                try:
                    session = contexts.acquire(context_key)
                except ContextLimitError as exc:
                    raise MCPError(types.INTERNAL_ERROR, str(exc)) from exc
        else:
            session = request.headers.get("mcp-session-id") if request is not None else None
        if not session:
            raise MCPError(types.INVALID_PARAMS, "A stateful MCP HTTP session is required.")
        try:
            result = await backend.call(params.name, params.arguments or {}, session)
        except BackendError:
            return result_from_hermes(json.dumps({"error": "Native Hermes execution failed."}))
        finally:
            if modern:
                if context_key is None:
                    manager.schedule_cleanup(session)
                else:
                    contexts.complete(context_key, session)
        return result_from_hermes(result)

    server = Server(
        "hermes2mcp",
        version=__version__,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
        instructions="Hermes Agent's original tool catalog, names, descriptions, "
        "and input schemas.",
    )
    security_settings = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", *settings.allowed_hosts],
        allowed_origins=[
            "http://127.0.0.1:*",
            "http://localhost:*",
            "http://[::1]:*",
            *settings.allowed_origins,
        ],
    )
    security = TransportSecurityMiddleware(security_settings)
    manager = NativeHTTPSessions(
        server,
        backend,
        json_response=True,
        stateless=False,
        max_sessions=128,
        security_settings=security_settings,
    )
    contexts = NativeContexts(
        manager.schedule_cleanup,
        limit=settings.max_contexts,
        idle_timeout=settings.context_idle_timeout,
    )

    async def release_context(request: Any) -> Response:
        rejected = await security.validate_request(request)
        if rejected is not None:
            return rejected
        key = request.headers.get("x-hermes-context")
        if key is None:
            return JSONResponse({"error": "X-Hermes-Context is required."}, status_code=400)
        if not contexts.release(key):
            return Response(status_code=204)
        return Response(status_code=202)

    @asynccontextmanager
    async def lifespan(app: Starlette):
        async with backend:
            try:
                async with manager.run():
                    reaper = asyncio.create_task(contexts.reap_idle())
                    try:
                        yield
                    finally:
                        reaper.cancel()
                        await asyncio.gather(reaper, return_exceptions=True)
            finally:
                contexts.release_all()
                await manager.finish_cleanup()

    app = Starlette(
        routes=[
            Route("/mcp", endpoint=StreamableHTTPASGIApp(manager)),
            Route("/mcp/context", endpoint=release_context, methods=["DELETE"]),
        ],
        lifespan=lifespan,
    )
    app.add_middleware(HTTPGuard, token=settings.token)
    app.state.backend = backend
    return app
