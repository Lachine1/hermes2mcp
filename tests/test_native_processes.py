"""Native process ownership and persist-on-release, without fake tool results."""

import asyncio
import json
import shlex
from pathlib import Path

import httpx2
import pytest
from test_native import PYTHON, native_backend

pytestmark = [pytest.mark.hermes, pytest.mark.skipif(not PYTHON, reason="real Hermes not selected")]


@pytest.mark.asyncio
@pytest.mark.parametrize("persist", [False, True])
async def test_native_release_stops_only_its_unpersisted_background_processes(tmp_path, persist):
    script = tmp_path / "serve.py"
    script.write_text(
        "import functools, json, os, sys, threading\n"
        "from http.server import HTTPServer, SimpleHTTPRequestHandler\n"
        "from pathlib import Path\n"
        "handler = functools.partial(SimpleHTTPRequestHandler, directory=sys.argv[2])\n"
        "server = HTTPServer(('127.0.0.1', 0), handler)\n"
        "Path(sys.argv[1]).write_text(json.dumps({'port':server.server_port,'pid':os.getpid()}))\n"
        "timer = threading.Timer(90, server.shutdown)\n"
        "timer.daemon = True\n"
        "timer.start()\n"
        "try:\n"
        "    server.serve_forever()\n"
        "finally:\n"
        "    server.server_close()\n"
        "    timer.cancel()\n",
        encoding="utf-8",
    )
    (tmp_path / "index.html").write_text("Native lifecycle server\n", encoding="utf-8")
    ready = {name: tmp_path / (name + ".json") for name in ("owner", "sibling")}
    handles = {}
    async with native_backend(tmp_path / "profile", toolsets=("terminal",)) as backend:
        try:
            for name in ready:
                command = shlex.join(
                    [
                        Path(PYTHON).as_posix(),
                        script.as_posix(),
                        ready[name].as_posix(),
                        tmp_path.as_posix(),
                    ]
                )
                spawned = json.loads(
                    await backend.call(
                        "terminal",
                        {
                            "command": command,
                            "workdir": str(tmp_path),
                            "background": True,
                            "persist_on_release": persist if name == "owner" else False,
                        },
                        name,
                    )
                )
                assert "session_id" in spawned, spawned
                handles[name] = spawned["session_id"]
            async with asyncio.timeout(15):
                # Each sentinel is written by a different native child process.
                while not all(path.is_file() for path in ready.values()):  # noqa: ASYNC110
                    await asyncio.sleep(0.02)
            urls = {
                name: "http://127.0.0.1:" + str(json.loads(path.read_text())["port"]) + "/"
                for name, path in ready.items()
            }
            async with httpx2.AsyncClient(timeout=2) as http:
                for url in urls.values():
                    response = await http.get(url)
                    assert response.status_code == 200
                    assert "Native lifecycle server" in response.text
                await backend.release("owner")
                try:
                    response = await http.get(urls["owner"])
                    owner_running = response.status_code == 200
                except httpx2.TransportError:
                    owner_running = False
                assert owner_running is persist, (
                    "Native persist-on-release ownership was not honored"
                )
                sibling = await http.get(urls["sibling"])
                assert sibling.status_code == 200, (
                    "Releasing one owner killed its sibling's process"
                )
        finally:
            for name, identity in handles.items():
                await backend.call(
                    "process_manage", {"action": "kill", "session_id": identity}, name
                )
                await backend.release(name)
