# hermes2mcp

Expose [Hermes Agent](https://github.com/NousResearch/hermes-agent)'s native tools through **MCP Streamable HTTP** at `/mcp`, supporting modern stateless and legacy stateful clients.

This is a protocol adapter, not a collection of rewritten tools:

- Original Hermes tool names and descriptions.
- Complete original input JSON schemas, including nested constraints and defaults.
- Original registry dispatch and native inline executors, with their middleware and hooks.
- Native result strings preserved exactly in MCP text content.
- Fresh native identity per unkeyed modern call; optional retained contexts keep todo and terminal state isolated between conversations.
- Persistent native task identity within a legacy HTTP session, with cleanup on session deletion.
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

SDK 2.3's default `Client(url)` autodiscovers the modern `2026-07-28` protocol. Legacy `ClientSession` clients remain supported. Modern calls without a context key receive independent, temporary native identities: state such as todos is not retained between calls and never falls back to a shared global context.

A Python client example using SDK 2.3's modern autodiscovery path:

```python
import asyncio
import os

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client


async def main():
    token = os.environ.get("HERMES2MCP_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx2.AsyncClient(headers=headers, timeout=60) as http:
        async with Client(
            streamable_http_client("http://127.0.0.1:8000/mcp", http_client=http),
            read_timeout_seconds=60,
        ) as client:
            tools = await client.list_tools()
            print([tool.name for tool in tools.tools])
            result = await client.call_tool("read_file", {"path": "README.md"})
            print(result.content[0].text)


asyncio.run(main())
```

Paths are resolved by the server's native tools, not on the MCP client's machine.

### Optional retained modern contexts

For a conversation that needs persistent native todos or terminal state, configure a server bearer token and send one `X-Hermes-Context` header on its requests. Generate a key once, before constructing the HTTP client:

```python
import secrets

context_key = secrets.token_hex(16)
headers = {
    "Authorization": f"Bearer {os.environ['HERMES2MCP_TOKEN']}",
    "X-Hermes-Context": context_key,
}
```

Reuse the same key to resume that conversation; use a new key for an independent conversation. Keys must be exactly 32 ASCII hexadecimal characters and are **case-sensitive**. These are application-owned native contexts, not MCP transport session IDs. Authentication is required for retained contexts even on loopback. Closing a modern client does not delete its context.

Release a retained context explicitly with `DELETE /mcp/context`, supplying the same authorization and context headers. A `202` response detaches the context; native cleanup waits for its active calls to finish. A `204` response means it was already absent. Reusing a released key creates a fresh generation and does not reconnect to old state.

Retained contexts expire after **1800 seconds of inactivity**, with background reaping even if no further HTTP request arrives. Active and queued calls hold their leases; idle age starts after the last call completes. The default retained-context limit is **128**. Embedders can configure `ServerSettings(max_contexts=..., context_idle_timeout=...)`; both values must be positive, and the timeout must be finite. Contexts are also released at server shutdown.

Use explicit HTTP and MCP deadlines for slow tools. An injected `httpx2.AsyncClient` otherwise defaults to a five-second HTTP timeout; that can end a client while native work is still running. The example uses 60-second deadlines, not a claim that every tool completes within that time.

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
- Run one ASGI worker. HTTP/native context state is process-local. The native scheduler has eight execution slots: independent contexts can proceed concurrently, while calls within a context retain order unless Hermes's original admission policy marks them parallel-safe. Unknown or incompatible native admission policies remain sequential. Busy slots and context-local barriers can still cause queueing.
- Client cancellation does not roll back an already-running native operation. Native shutdown is bounded and may terminate a busy worker.
- Native approval behavior is retained. There is no interactive approval UI in the adapter; review your native approval policy before granting clients access.

## Runtime coverage and limitations

The adapter uses Hermes's registry for ordinary tools and its original agent-runtime invocation helper for inline tools. Real `TodoStore`, built-in `MemoryStore`, session database acquisition, and lazy native delegation state are supplied per native context. Legacy session deletion and modern context release/expiration clean up native resources. Hermes's own resource lifecycle stops owned background processes unless they explicitly requested `persist_on_release`; sibling contexts are not released. Persistent memory files remain persistent and are not isolated by context keys.

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

The tests cover real TCP MCP connections, modern SDK autodiscovery, complete native catalog/schema equality over authenticated HTTP, native `read_file` results compared byte-for-byte with dispatch, sequential and concurrent reads, built-in memory writes, ephemeral and retained todo isolation, case-sensitive keys, idle/DELETE/shutdown cleanup, active and queued context leases, native session DB bootstrap, original concurrency admission and execution middleware, background-process ownership, opt-in local upstream discovery, private IPC protection, and bind/auth/DNS checks.

GitHub CI checks the adapter with a test-only native fixture on Linux/Windows and Python 3.11/3.14. Real-native tests are separate and require the selected Hermes installation; they are not silently replaced with simulated vendor responses. No paid provider or external vendor tool call is part of the acceptance suite.

## License

MIT for this adapter. Hermes Agent and its optional integrations retain their own licenses. This is an independent project, not an official Nous Research service.
