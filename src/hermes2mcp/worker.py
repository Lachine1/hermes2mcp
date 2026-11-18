"""Private worker IPC, not an MCP transport. This file uses only the Python stdlib."""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import io
import json
import os
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-path", type=Path)
    parser.add_argument("--include-upstream", action="store_true")
    parser.add_argument("--toolset", action="append")
    parser.add_argument("--self-url", action="append", default=[])
    args = parser.parse_args(argv)
    incoming = os.fdopen(os.dup(sys.stdin.fileno()), "r", encoding="utf-8")
    null_input = os.open(os.devnull, os.O_RDONLY)
    try:
        os.dup2(null_input, sys.stdin.fileno())
    finally:
        os.close(null_input)
    outgoing = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", buffering=1)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr
    sys.stdin = io.StringIO("")
    os.environ.setdefault("HERMES_QUIET", "1")
    os.environ.setdefault("HERMES_REDACT_SECRETS", "true")
    if args.hermes_path is not None:
        if not (args.hermes_path / "model_tools.py").is_file():
            outgoing.write(
                json.dumps(
                    {
                        "ok": False,
                        "error": "--hermes-path is not a Hermes source root.",
                    }
                )
                + "\n"
            )
            return 2
        sys.path.insert(0, str(args.hermes_path.resolve()))
    shutdown_mcp = None
    try:
        if importlib.util.find_spec("hermes_bootstrap") is not None:
            importlib.import_module("hermes_bootstrap")
        if importlib.util.find_spec("hermes_constants") is not None:
            from hermes_cli.env_loader import load_hermes_dotenv
            from hermes_constants import get_hermes_home

            load_hermes_dotenv(hermes_home=get_hermes_home())
        os.environ.pop("HERMES2MCP_TOKEN", None)
        model_tools = importlib.import_module("model_tools")
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from hermes2mcp.runtime import NativeRuntime

        runtime = NativeRuntime(model_tools, toolsets=args.toolset)
        if args.include_upstream:
            from agent.safe_worker_policy import safe_worker_enabled
            from tools.mcp_tool_config import _load_mcp_config
            from tools.mcp_tool_discovery import _owner_secret_scope, discover_mcp_tools
            from tools.mcp_tool_lifecycle import shutdown_mcp_servers

            shutdown_mcp = shutdown_mcp_servers
            if not safe_worker_enabled():
                with _owner_secret_scope():
                    config = _load_mcp_config()
                own_urls = {url.rstrip("/") for url in args.self_url}
                selected = config.keys() if args.toolset is None else args.toolset
                allowed = [
                    name
                    for name in selected
                    if str(config.get(name, {}).get("url", "")).rstrip("/") not in own_urls
                ]
                discover_mcp_tools(allowed_mcp_names=allowed)
    except Exception as exc:
        outgoing.write(
            json.dumps(
                {
                    "ok": False,
                    "error": "Hermes bootstrap failed ("
                    + type(exc).__name__
                    + "). Select Hermes's prepared Python with --hermes-python.",
                }
            )
            + "\n"
        )
        return 2

    try:
        for line in incoming:
            try:
                request = json.loads(line)
                operation = request["op"]
                if operation == "list":
                    result = runtime.definitions()
                elif operation == "call":
                    result = runtime.call(
                        request["name"],
                        request["arguments"],
                        request["session_id"],
                    )
                elif operation == "release":
                    runtime.release(request["session_id"])
                    result = None
                else:
                    raise ValueError("Unknown private worker operation.")
                response = {"ok": True, "result": result}
            except Exception as exc:
                response = {
                    "ok": False,
                    "error": "Native Hermes request failed (" + type(exc).__name__ + ").",
                }
            outgoing.write(json.dumps(response, ensure_ascii=False) + "\n")
    finally:
        try:
            runtime.close()
            if shutdown_mcp is not None:
                shutdown_mcp(timeout=2)
        finally:
            incoming.close()
            outgoing.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
