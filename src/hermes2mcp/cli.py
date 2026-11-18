"""Start Hermes's original tools as an HTTP-only MCP server."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__
from .backend import HermesBackend
from .server import ServerSettings, create_app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=f"hermes2mcp {__version__}")
    parser.add_argument("--host", default=os.environ.get("HERMES2MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=os.environ.get("HERMES2MCP_PORT", "8000"))
    parser.add_argument(
        "--hermes-python",
        default=os.environ.get("HERMES2MCP_HERMES_PYTHON", sys.executable),
        help="Hermes's prepared Python interpreter (may differ from this server's Python).",
    )
    parser.add_argument(
        "--hermes-path",
        type=Path,
        default=os.environ.get("HERMES2MCP_HERMES_PATH"),
        help="Hermes source root if it is not already importable by its Python.",
    )
    parser.add_argument("--hermes-home", type=Path, help="Use a specific Hermes profile/home.")
    parser.add_argument(
        "--toolset", action="append", help="Native Hermes toolset; repeat to select several."
    )
    parser.add_argument(
        "--include-upstream",
        action="store_true",
        help="Also discover configured upstream MCP servers. Do not connect the bridge to itself.",
    )
    parser.add_argument(
        "--allowed-host", action="append", default=[], help="Additional allowed Host value."
    )
    parser.add_argument(
        "--allowed-origin", action="append", default=[], help="Additional allowed browser Origin."
    )
    parser.add_argument(
        "--log-level", choices=["error", "warning", "info", "debug"], default="info"
    )
    args = parser.parse_args(argv)
    try:
        settings = ServerSettings(
            host=args.host,
            port=args.port,
            token=os.environ.get("HERMES2MCP_TOKEN") or None,
            allowed_hosts=tuple(args.allowed_host),
            allowed_origins=tuple(args.allowed_origin),
        )
    except ValueError as exc:
        parser.error(str(exc))
    backend = HermesBackend(
        python=args.hermes_python,
        hermes_path=args.hermes_path,
        hermes_home=args.hermes_home,
        include_upstream=args.include_upstream,
        toolsets=args.toolset,
    )
    import uvicorn

    uvicorn.run(
        create_app(backend, settings),
        host=settings.host,
        port=settings.port,
        log_level=args.log_level,
        lifespan="on",
        proxy_headers=False,
    )
    return 0
