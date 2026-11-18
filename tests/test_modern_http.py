"""Modern HTTP context contract, exercised through the real SDK/ASGI path."""

import asyncio
import json
import sys

import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from test_http import FIXTURE, serve

from hermes2mcp.backend import BackendError, HermesBackend
from hermes2mcp.server import ServerSettings, create_app

KEY = "ABCDEF0123456789abcdef0123456789"


def test_modern_context_settings_default_to_128_retained_keys_and_a_1800_second_idle_lease():
    settings = ServerSettings()
    assert settings.max_contexts == 128
    assert getattr(settings, "context_idle_timeout", None) == 1800


@pytest.mark.parametrize("limit", [0, -1, 1.5, float("inf"), float("nan"), True, None, "1"])
def test_retained_context_settings_require_a_positive_integer_limit(limit):
    with pytest.raises(ValueError, match="max_contexts.*positive integer"):
        ServerSettings(max_contexts=limit)


@pytest.mark.parametrize(
    "timeout", [0, -1, float("inf"), float("-inf"), float("nan"), True, None, "1", 10**1000]
)
def test_retained_context_settings_require_a_positive_finite_idle_timeout(timeout):
    with pytest.raises(ValueError, match="context_idle_timeout.*positive.*finite"):
        ServerSettings(context_idle_timeout=timeout)


def modern_request(method="tools/call", *, arguments=None, rpc_id=1):
    params = {
        "_meta": {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientCapabilities": {},
        }
    }
    if method == "tools/call":
        params.update(name="fixture_echo", arguments=arguments or {})
    return {"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params}


def modern_headers(method="tools/call", **extra):
    return {
        "MCP-Protocol-Version": "2026-07-28",
        "MCP-Method": method,
        "Accept": "application/json, text/event-stream",
        **({"MCP-Name": "fixture_echo"} if method == "tools/call" else {}),
        **extra,
    }


@pytest.mark.asyncio
async def test_keyed_modern_requests_require_configured_bearer_authentication():
    backend = HermesBackend(python=sys.executable, hermes_path=FIXTURE)
    async with serve(create_app(backend)) as url:
        async with httpx2.AsyncClient() as http:
            response = await http.post(
                url,
                headers=modern_headers(**{"X-Hermes-Context": KEY}),
                json=modern_request(),
            )
            assert response.status_code == 401, response.text
            assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key",
    [b"", b"a" * 31, b"a" * 33, b"g" * 32, b"a" * 16 + b" " + b"a" * 15, b"\xe9" * 32],
)
async def test_context_header_rejects_anything_except_exactly_32_ascii_hex_characters(key):
    backend = HermesBackend(python=sys.executable, hermes_path=FIXTURE)
    async with serve(create_app(backend, ServerSettings(token="header-test-token"))) as url:
        async with httpx2.AsyncClient() as http:
            headers = {
                **modern_headers(),
                "Authorization": "Bearer header-test-token",
                "X-Hermes-Context": key,
            }
            response = await http.post(url, headers=headers, json=modern_request())
            assert response.status_code == 400, response.text
            assert "32 ASCII hex" in response.text


@pytest.mark.asyncio
async def test_repeated_context_headers_are_rejected_instead_of_choosing_one_key():
    backend = HermesBackend(python=sys.executable, hermes_path=FIXTURE)
    async with serve(create_app(backend, ServerSettings(token="header-test-token"))) as url:
        async with httpx2.AsyncClient() as http:
            headers = [
                *modern_headers().items(),
                ("Authorization", "Bearer header-test-token"),
                ("X-Hermes-Context", KEY),
                ("X-Hermes-Context", KEY.lower()),
            ]
            response = await http.post(url, headers=headers, json=modern_request())
            assert response.status_code == 400, response.text
            assert "32 ASCII hex" in response.text


class FailingCleanupBackend(HermesBackend):
    """Execute the real test worker, then inject only the cleanup fault."""

    def __init__(self, *, fail_call=False):
        super().__init__(python=sys.executable, hermes_path=FIXTURE)
        self.fail_call = fail_call
        self.last_result = None
        self.cleanup_attempted = asyncio.Event()

    async def call(self, name, arguments, session_id):
        self.last_result = await super().call(name, arguments, session_id)
        if self.fail_call:
            raise BackendError("Test-only backend call failure")
        return self.last_result

    async def release(self, session_id):
        await super().release(session_id)
        self.cleanup_attempted.set()
        raise BackendError("Test-only release failure")


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_call", [False, True])
async def test_ephemeral_cleanup_failure_cannot_replace_an_already_produced_outcome(fail_call):
    backend = FailingCleanupBackend(fail_call=fail_call)
    async with serve(create_app(backend)) as url:
        async with httpx2.AsyncClient() as http:
            response = await http.post(
                url, headers=modern_headers(), json=modern_request(arguments={"value": "café ☃"})
            )
            assert response.status_code == 200, response.text
            result = response.json()["result"]
            if fail_call:
                assert result["isError"] is True
                assert json.loads(result["content"][0]["text"]) == {
                    "error": "Native Hermes execution failed."
                }
            else:
                assert result["isError"] is False
                assert result["content"][0]["text"] == backend.last_result
            await asyncio.wait_for(backend.cleanup_attempted.wait(), 2)


@pytest.mark.asyncio
async def test_context_release_requires_configured_auth_even_on_loopback():
    backend = HermesBackend(python=sys.executable, hermes_path=FIXTURE)
    async with serve(create_app(backend)) as url:
        async with httpx2.AsyncClient() as http:
            response = await http.delete(
                url + "/context",
                headers={"X-Hermes-Context": KEY, "Authorization": "Bearer arbitrary-test-token"},
            )
            assert response.status_code == 401, response.text
            assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_header,status", [("Host", 421), ("Origin", 403)])
async def test_context_release_reuses_sdk_host_and_origin_protection(bad_header, status):
    backend = HermesBackend(python=sys.executable, hermes_path=FIXTURE)
    settings = ServerSettings(
        token="release-security-token",
        allowed_hosts=("mcp.example.test",),
        allowed_origins=("https://mcp.example.test",),
    )
    headers = {
        "Authorization": "Bearer release-security-token",
        "X-Hermes-Context": KEY,
        "Host": "mcp.example.test",
        "Origin": "https://mcp.example.test",
    }
    async with serve(create_app(backend, settings)) as url:
        async with httpx2.AsyncClient() as http:
            first = await http.post(url, headers=modern_headers(**headers), json=modern_request())
            assert first.status_code == 200, first.text
            native_id = first.json()["result"]["structuredContent"]["task_id"]
            response = await http.delete(
                url + "/context", headers={**headers, bad_header: "https://evil.test"}
            )
            assert response.status_code == status, response.text
            readback = await http.post(
                url, headers=modern_headers(**headers), json=modern_request()
            )
            assert readback.json()["result"]["structuredContent"]["task_id"] == native_id
            valid = await http.delete(url + "/context", headers=headers)
            assert valid.status_code == 202


@pytest.mark.asyncio
async def test_context_release_requires_one_valid_key_and_is_idempotent():
    backend = HermesBackend(python=sys.executable, hermes_path=FIXTURE)
    async with serve(create_app(backend, ServerSettings(token="release-security-token"))) as url:
        async with httpx2.AsyncClient(
            headers={"Authorization": "Bearer release-security-token"}
        ) as http:
            missing = await http.delete(url + "/context")
            assert missing.status_code == 400
            malformed = await http.delete(url + "/context", headers={"X-Hermes-Context": "short"})
            assert malformed.status_code == 400
            for _ in range(2):
                unknown = await http.delete(url + "/context", headers={"X-Hermes-Context": KEY})
                assert unknown.status_code == 204


@pytest.mark.asyncio
async def test_default_retained_context_limit_refuses_the_129th_key_not_existing_or_unkeyed_calls():
    backend = HermesBackend(python=sys.executable, hermes_path=FIXTURE)
    async with serve(create_app(backend, ServerSettings(token="context-limit-token"))) as url:
        async with httpx2.AsyncClient(
            headers={"Authorization": "Bearer context-limit-token"}, timeout=20
        ) as http:
            for number in range(128):
                response = await http.post(
                    url,
                    headers=modern_headers(**{"X-Hermes-Context": f"{number:032x}"}),
                    json=modern_request(),
                )
                assert "result" in response.json(), response.text
            excess = await http.post(
                url,
                headers=modern_headers(**{"X-Hermes-Context": f"{128:032x}"}),
                json=modern_request(),
            )
            assert "error" in excess.json(), excess.text
            assert "context limit" in excess.json()["error"]["message"].lower()
            for extra in ({"X-Hermes-Context": f"{0:032x}"}, {}):
                allowed = await http.post(
                    url, headers=modern_headers(**extra), json=modern_request()
                )
                assert "result" in allowed.json(), allowed.text


class RecordingCleanupBackend(HermesBackend):
    def __init__(self):
        super().__init__(python=sys.executable, hermes_path=FIXTURE)
        self.released = []
        self.release_done = asyncio.Event()

    async def release(self, session_id):
        await super().release(session_id)
        self.released.append(session_id)
        self.release_done.set()


class GatedCallBackend(RecordingCleanupBackend):
    """Hold one HTTP call before original worker execution, without faking it."""

    def __init__(self):
        super().__init__()
        self.call_entered = asyncio.Event()
        self.allow_call = asyncio.Event()
        self.queued_entered = asyncio.Event()
        self.allow_queued = asyncio.Event()
        self.held_sessions = []

    async def call(self, name, arguments, session_id):
        if arguments.get("value") == "held":
            self.held_sessions.append(session_id)
            self.call_entered.set()
            await asyncio.wait_for(self.allow_call.wait(), 10)
        elif arguments.get("value") == "queued":
            self.held_sessions.append(session_id)
            self.queued_entered.set()
            await asyncio.wait_for(self.allow_queued.wait(), 10)
        return await super().call(name, arguments, session_id)


@pytest.mark.asyncio
async def test_idle_reaper_keeps_an_active_http_context_until_its_call_completes():
    backend = GatedCallBackend()
    settings = ServerSettings(token="lease-test-token", max_contexts=1, context_idle_timeout=0.15)
    async with serve(create_app(backend, settings)) as url:
        async with httpx2.AsyncClient(
            headers={"Authorization": "Bearer lease-test-token"}, timeout=10
        ) as http:
            pending = asyncio.create_task(
                http.post(
                    url,
                    headers=modern_headers(**{"X-Hermes-Context": KEY}),
                    json=modern_request(arguments={"value": "held"}),
                )
            )
            try:
                await asyncio.wait_for(backend.call_entered.wait(), 5)
                await asyncio.sleep(0.35)
                assert backend.released == [], "The reaper expired an active native call"
                excess = await http.post(
                    url,
                    headers=modern_headers(**{"X-Hermes-Context": KEY.lower()}),
                    json=modern_request(),
                )
                assert "context limit" in excess.json()["error"]["message"].lower()
                backend.allow_call.set()
                completed = await asyncio.wait_for(pending, 5)
                assert completed.status_code == 200, completed.text
                assert completed.json()["result"]["content"][0]["text"]
                # Idle age starts at completion, not acquisition.
                await asyncio.sleep(0.05)
                assert backend.released == []
                await asyncio.wait_for(backend.release_done.wait(), 5)
                assert backend.released == backend.held_sessions
            finally:
                backend.allow_call.set()
                await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_delete_defers_cleanup_of_active_work_without_touching_a_reused_key():
    backend = GatedCallBackend()
    settings = ServerSettings(token="lease-test-token", max_contexts=1)
    async with serve(create_app(backend, settings)) as url:
        async with httpx2.AsyncClient(
            headers={"Authorization": "Bearer lease-test-token", "X-Hermes-Context": KEY},
            timeout=10,
        ) as http:
            pending = asyncio.create_task(
                http.post(
                    url,
                    headers=modern_headers(),
                    json=modern_request(arguments={"value": "held"}),
                )
            )
            try:
                await asyncio.wait_for(backend.call_entered.wait(), 5)
                old_session = backend.held_sessions[0]
                released = await http.delete(url + "/context")
                assert released.status_code == 202, released.text
                absent = await http.delete(url + "/context")
                assert absent.status_code == 204, absent.text
                replacement = await http.post(url, headers=modern_headers(), json=modern_request())
                assert replacement.status_code == 200, replacement.text
                new_id = replacement.json()["result"]["structuredContent"]["task_id"]
                assert new_id != "hermes2mcp-" + old_session
                # DELETE detaches the old generation, but cannot release it
                # before the native work it has already leased.
                await asyncio.sleep(0.05)
                assert backend.released == [], "DELETE cleanup overtook its active call"
                backend.allow_call.set()
                completed = await asyncio.wait_for(pending, 5)
                assert completed.status_code == 200, completed.text
                assert completed.json()["result"]["structuredContent"]["task_id"] == (
                    "hermes2mcp-" + old_session
                )
                await asyncio.wait_for(backend.release_done.wait(), 5)
                assert backend.released == [old_session]
                readback = await http.post(url, headers=modern_headers(), json=modern_request())
                assert readback.json()["result"]["structuredContent"]["task_id"] == new_id
            finally:
                backend.allow_call.set()
                await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("delete", [False, True])
async def test_queued_http_acquisition_holds_cleanup_after_the_first_call_completes(delete):
    backend = GatedCallBackend()
    settings = ServerSettings(token="lease-test-token", context_idle_timeout=0.15)
    async with serve(create_app(backend, settings)) as url:
        async with httpx2.AsyncClient(
            headers={"Authorization": "Bearer lease-test-token", "X-Hermes-Context": KEY},
            timeout=10,
        ) as http:
            first = asyncio.create_task(
                http.post(
                    url, headers=modern_headers(), json=modern_request(arguments={"value": "held"})
                )
            )
            queued = asyncio.create_task(
                http.post(
                    url,
                    headers=modern_headers(),
                    json=modern_request(arguments={"value": "queued"}),
                )
            )
            try:
                await asyncio.wait_for(backend.call_entered.wait(), 5)
                await asyncio.wait_for(backend.queued_entered.wait(), 5)
                assert len(set(backend.held_sessions)) == 1
                if delete:
                    response = await http.delete(url + "/context")
                    assert response.status_code == 202, response.text
                backend.allow_call.set()
                completed = await asyncio.wait_for(first, 5)
                assert completed.status_code == 200, completed.text
                native_id = completed.json()["result"]["structuredContent"]["task_id"]
                await asyncio.sleep(0.35)
                assert backend.released == [], "A queued lease was mistaken for an idle context"
                if not delete:
                    readback = await http.post(url, headers=modern_headers(), json=modern_request())
                    assert readback.json()["result"]["structuredContent"]["task_id"] == native_id
                backend.allow_queued.set()
                completed = await asyncio.wait_for(queued, 5)
                assert completed.status_code == 200, completed.text
                assert completed.json()["result"]["structuredContent"]["task_id"] == native_id
                await asyncio.wait_for(backend.release_done.wait(), 5)
                assert backend.released == [native_id.removeprefix("hermes2mcp-")]
            finally:
                backend.allow_call.set()
                backend.allow_queued.set()
                await asyncio.gather(first, queued, return_exceptions=True)


@pytest.mark.asyncio
async def test_idle_context_lease_releases_without_another_http_request_and_frees_capacity():
    backend = RecordingCleanupBackend()
    settings = ServerSettings(token="lease-test-token", max_contexts=1, context_idle_timeout=0.05)
    async with serve(create_app(backend, settings)) as url:
        async with httpx2.AsyncClient(headers={"Authorization": "Bearer lease-test-token"}) as http:
            first = await http.post(
                url, headers=modern_headers(**{"X-Hermes-Context": KEY}), json=modern_request()
            )
            assert first.status_code == 200, first.text
            native_id = first.json()["result"]["structuredContent"]["task_id"]
            try:
                await asyncio.wait_for(backend.release_done.wait(), 2)
            except TimeoutError:
                pass
            assert backend.release_done.is_set(), (
                "The idle lease retained native state indefinitely"
            )
            assert backend.released == [native_id.removeprefix("hermes2mcp-")]
            second = await http.post(
                url, headers=modern_headers(**{"X-Hermes-Context": KEY}), json=modern_request()
            )
            assert "result" in second.json(), second.text
            assert second.json()["result"]["structuredContent"]["task_id"] != native_id


@pytest.mark.asyncio
async def test_http_shutdown_releases_retained_contexts_and_joins_the_reaper():
    backend = RecordingCleanupBackend()
    reapers = []
    native_sessions = []
    settings = ServerSettings(token="shutdown-test-token")
    async with serve(create_app(backend, settings)) as url:
        reapers = [
            task
            for task in asyncio.all_tasks()
            if task.get_coro().__qualname__ == "NativeContexts.reap_idle"
        ]
        assert len(reapers) == 1
        async with httpx2.AsyncClient(
            headers={"Authorization": "Bearer shutdown-test-token"}, timeout=10
        ) as http:
            for key in (KEY, KEY.lower()):
                response = await http.post(
                    url, headers=modern_headers(**{"X-Hermes-Context": key}), json=modern_request()
                )
                assert response.status_code == 200, response.text
                native_sessions.append(
                    response.json()["result"]["structuredContent"]["task_id"].removeprefix(
                        "hermes2mcp-"
                    )
                )
            assert backend.released == []
    assert set(backend.released) == set(native_sessions), (
        "Shutdown skipped retained context release"
    )
    assert len(backend.released) == 2
    assert all(task.done() for task in reapers)
    assert not backend.running
    assert not [
        task
        for task in asyncio.all_tasks()
        if task.get_coro().__qualname__ == "RecordingCleanupBackend.release"
    ]


@pytest.mark.asyncio
async def test_http_shutdown_cleans_native_legacy_sessions_not_deleted_by_the_client():
    backend = RecordingCleanupBackend()
    settings = ServerSettings(token="shutdown-test-token")
    async with serve(create_app(backend, settings)) as url:
        async with httpx2.AsyncClient(
            headers={"Authorization": "Bearer shutdown-test-token"}, timeout=10
        ) as http:
            async with streamable_http_client(
                url, http_client=http, terminate_on_close=False
            ) as streams:
                async with ClientSession(*streams) as client:
                    await client.initialize()
                    one = await client.call_tool("fixture_echo", {"value": "legacy"})
                    two = await client.call_tool("fixture_echo", {})
                    native_id = json.loads(one.content[0].text)["task_id"]
                    assert json.loads(two.content[0].text)["task_id"] == native_id
            assert backend.released == []
    assert backend.released == [native_id.removeprefix("hermes2mcp-")]
    assert not backend.running
