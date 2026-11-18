# hermes2mcp

Expose [Hermes Agent](https://github.com/NousResearch/hermes-agent)'s native tools through **stateful MCP Streamable HTTP** at `/mcp`.

This is a protocol adapter, not a collection of rewritten tools:

- Original Hermes tool names and descriptions.
- Complete original input JSON schemas, including nested constraints and defaults.
- Original registry dispatch and native inline executors, with their middleware and hooks.
- Native result strings preserved exactly in MCP text content.
- Persistent native task identity within an HTTP session; separate todo and terminal contexts between sessions.
- HTTP-only client interface. There is no public MCP stdio transport.

**Alpha:** Hermes is installed separately. The adapter depends on native runtime interfaces; the tested Hermes revision is [`3b421578588243f920886e6fe79a6faa87ea026e`](https://github.com/NousResearch/hermes-agent/commit/3b421578588243f920886e6fe79a6faa87ea026e). The MCP SDK dependency is deliberately bounded to **2.3.x**.

## Install

Requires Python 3.11+ for the adapter and a working, prepared Hermes Python environment for native execution. Keep them separate; do not add adapter dependencies to a running managed Hermes environment.

```bash
git clone https://github.com/Lachine1/hermes2mcp.git
cd hermes2mcp
python -m venv .venv
# Activate .venv using your shell's normal activation command.
python -m pip install .
```

If your managed Hermes Python has no pip, use a separate PM environment instead. For example, pass the **native absolute path** of this checkout:

```bash
hermes pm environment hermes2mcp --requirement 'hermes2mcp @ file:///C:/absolute/path/to/hermes2mcp'
```

PM prints the environment's Python path. Invoke that interpreter with `-m hermes2mcp`. On Unix, use an absolute Unix checkout path instead.

## Start

```bash
hermes2mcp \
  --hermes-python /absolute/path/to/hermes-environment/bin/python \
  --hermes-path /absolute/path/to/hermes-source-or-workspace \
  --host 127.0.0.1 --port 8000
```

On Windows, the native interpreter normally ends in `Scripts/python.exe`; use quoted `C:/...` paths. `--hermes-path` must contain `model_tools.py`. Packaged desktop installations may run from an environment's `workspace`, not the visible checkout.

`--hermes-python` can be omitted only when the adapter's Python can already import the prepared Hermes runtime. `--hermes-path` can be omitted when Hermes is already importable in the selected native interpreter.

The endpoint is:

```text
http://127.0.0.1:8000/mcp
```

Useful options:

| Option | Meaning |
| --- | --- |
| `--hermes-python` | Prepared native Hermes interpreter. |
| `--hermes-path` | Actual Hermes source/workspace root. |
| `--hermes-home` | Explicit Hermes profile/home; otherwise native home resolution applies. |
| `--toolset` | Native toolset or MCP server name; repeat for selection. No selection means the full available native catalog. |
| `--include-upstream` | Discover configured upstream MCP servers using Hermes's own client. Off by default. |
| `--allowed-host` | Additional exact Host value accepted by DNS-rebinding protection. |
| `--allowed-origin` | Additional browser Origin accepted by DNS-rebinding protection. |
| `--log-level` | Uvicorn log level. |

Environment defaults are `HERMES2MCP_HERMES_PYTHON`, `HERMES2MCP_HERMES_PATH`, `HERMES2MCP_HOST`, and `HERMES2MCP_PORT`. Use `HERMES2MCP_TOKEN` for the bearer secret. Do not put non-credential Hermes settings in its `.env` file.

## Connect an MCP client

Configure your client for **Streamable HTTP**, URL `http://127.0.0.1:8000/mcp`. If a token is configured, add:

```text
Authorization: Bearer <your-secret>
```

Use a stateful/legacy MCP client. With SDK 2.3, `ClientSession` negotiates the supported stateful path. Modern stateless protocol `2026-07-28` is explicitly rejected rather than sharing a fallback native context.

A Python client example (SDK 2.3 uses `httpx2` and **two** transport streams):

```python
import asyncio
import os

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def main():
    token = os.environ.get("HERMES2MCP_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx2.AsyncClient(headers=headers) as http:
        async with streamable_http_client(
            "http://127.0.0.1:8000/mcp", http_client=http
        ) as streams:
            async with ClientSession(*streams) as client:
                await client.initialize()
                tools = await client.list_tools()
                print([tool.name for tool in tools.tools])
                result = await client.call_tool("read_file", {"path": "README.md"})
                print(result.content[0].text)


asyncio.run(main())
```

Paths are resolved by the server's native tools, not on the MCP client's machine.

## Security and operational boundaries

**These tools can execute commands, read secrets, and modify files. This is not a sandbox.**

- Default binding is loopback. A non-loopback bind requires a bearer token.
- A configured token is enforced on POST, GET, and DELETE, including loopback.
- This is one shared OS/profile identity, not OAuth or a multi-tenant service. Session isolation does not isolate filesystem access, credentials, or persistent profile memories.
- For remote access, require HTTPS through a trusted TLS proxy and restrict the backend with a firewall. Do not expose the plain HTTP listener directly to the Internet.
- Preserve Host, scheme/path routing, and required MCP headers at the proxy. Add exact allowed hosts/origins. The CLI disables forwarded-header trust.
- DNS-rebinding protection rejects unapproved Host and Origin values. Broad CORS is not enabled.
- The native worker does not receive `HERMES2MCP_TOKEN`. Native diagnostics go to stderr; private subprocess pipes are IPC, not an exposed MCP transport.
- Upstream discovery is opt-in and preserves Hermes's native safe-worker gates and secret scope. Known direct bind URLs are excluded from startup discovery. **Do not configure this bridge's public proxy URL, an unlisted alias, or a redirect back to it as an upstream server.**
- Run one ASGI worker. HTTP/native session state is process-local. Native calls are serialized; a long tool call can delay other tool calls, though it does not block the HTTP event loop.
- Client cancellation does not roll back an already-running native operation. Native shutdown is bounded and may terminate a busy worker.
- Native approval behavior is retained. There is no interactive approval UI in the adapter; review your native approval policy before granting clients access.

## Runtime coverage and limitations

The adapter uses Hermes's registry for ordinary tools and its original agent-runtime invocation helper for inline tools. Real `TodoStore`, built-in `MemoryStore`, session database acquisition, and lazy native delegation state are supplied per native session. Deleting an HTTP session releases its native todo/terminal resources; persistent memory files remain persistent.

Tool availability depends on the native installation, configured toolsets, credentials, plugins, and trust policy. No arbitrary catalog-size cap or curated tool-name list is applied. Original Hermes prefixes on upstream tools are retained, not replaced with adapter prefixes.

Built-in memory is covered; custom memory-provider augmentation and desktop-specific callbacks are not bootstrapped. Tools requiring an interactive surface can return their native unavailable/error result. Actual model-backed delegation requires configured native model credentials and may incur provider costs; the automated native delegation test exercises status/list without an LLM request, not a paid subagent run.

Native JSON-object results also receive MCP `structuredContent`. Arrays/scalars remain exact text for the stateful protocol. This does not invent multimedia blocks or bypass native result formatting. Native execution exceptions receive a generic bridge error instead of leaking exception contents.

## Development and verification

```bash
python -m pip install '.[dev]'
python -m ruff check src tests
python -m ruff format --check src tests
python -m pytest -m 'not hermes'
python -m build
```

To exercise real Hermes, select the actual prepared interpreter/source and use a disposable test directory:

```bash
HERMES2MCP_HERMES_PYTHON=/path/to/hermes/python \
HERMES2MCP_HERMES_PATH=/path/to/hermes/workspace \
python -m pytest --basetemp /path/to/disposable/hermes2mcp-tests
```

The tests cover real TCP MCP connections, complete native catalog/schema equality over authenticated HTTP, a native `read_file` result compared byte-for-byte with direct dispatch, built-in memory writes, todo isolation and deletion cleanup, native session DB bootstrap, execution middleware parity, opt-in local upstream discovery, private IPC protection, and bind/auth/DNS checks.

GitHub CI checks the adapter with a test-only native fixture on Linux/Windows and Python 3.11/3.14. Real-native tests are separate and require the selected Hermes installation; they are not silently replaced with simulated vendor responses. No paid provider or external vendor tool call is part of the acceptance suite.

## License

MIT for this adapter. Hermes Agent and its optional integrations retain their own licenses. This is an independent project, not an official Nous Research service.
