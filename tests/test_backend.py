import sys
from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "hermes"


@pytest.mark.asyncio
async def test_worker_calls_original_module_and_keeps_a_session_context():
    from hermes2mcp.backend import HermesBackend

    async with HermesBackend(python=sys.executable, hermes_path=FIXTURE) as backend:
        definitions = await backend.definitions()
        assert [item["function"]["name"] for item in definitions] == ["fixture_echo"]
        first = await backend.call("fixture_echo", {"value": "verbatim"}, "client-one")
        second = await backend.call("fixture_echo", {"value": None}, "client-one")
        other = await backend.call("fixture_echo", {}, "client-two")
        import json

        first, second, other = map(json.loads, (first, second, other))
        assert first["arguments"] == {"value": "verbatim"}
        assert second["arguments"] == {"value": None}
        assert first["task_id"] == second["task_id"]
        assert first["task_id"] != other["task_id"]
        assert [first["counter"], second["counter"], other["counter"]] == [1, 2, 3]
    assert backend.running is False


@pytest.mark.asyncio
async def test_native_subprocess_cannot_read_the_private_rpc_input(tmp_path):
    import json

    from hermes2mcp.backend import HermesBackend

    async with HermesBackend(
        python=sys.executable,
        hermes_path=FIXTURE,
        hermes_home=tmp_path / "profile",
    ) as backend:
        result = json.loads(await backend.call("fixture_echo", {"probe_stdin": True}, "client"))
        assert result["inherited_stdin"] == ""


@pytest.mark.asyncio
async def test_busy_native_context_does_not_block_an_independent_context(tmp_path):
    import asyncio
    import json

    from hermes2mcp.backend import HermesBackend

    ready, release = tmp_path / "ready", tmp_path / "release"
    value = "fixture-wait:" + json.dumps({"ready": str(ready), "release": str(release)})
    async with HermesBackend(python=sys.executable, hermes_path=FIXTURE) as backend:
        slow = asyncio.create_task(backend.call("fixture_echo", {"value": value}, "busy"))
        try:
            async with asyncio.timeout(2):
                # The readiness sentinel belongs to a separate native process.
                while not ready.exists():  # noqa: ASYNC110
                    await asyncio.sleep(0.01)
            result = json.loads(
                await asyncio.wait_for(
                    backend.call("fixture_echo", {"value": "independent"}, "other"),
                    timeout=1,
                )
            )
            assert result["arguments"] == {"value": "independent"}
            assert not slow.done(), "The independent result arrived only after the blocked call"
        finally:
            release.touch()
            finished = json.loads(await asyncio.wait_for(slow, 5))
        assert finished["arguments"] == {"value": value}
        assert finished["task_id"] != result["task_id"]


@pytest.mark.asyncio
async def test_cancelled_queued_call_is_not_dispatched_after_the_busy_call(tmp_path):
    import asyncio
    import json

    from hermes2mcp.backend import HermesBackend

    ready, release = tmp_path / "ready", tmp_path / "release"
    value = "fixture-wait:" + json.dumps({"ready": str(ready), "release": str(release)})
    async with HermesBackend(python=sys.executable, hermes_path=FIXTURE) as backend:
        slow = asyncio.create_task(backend.call("fixture_echo", {"value": value}, "client"))
        try:
            async with asyncio.timeout(2):
                # The readiness sentinel belongs to a separate native process.
                while not ready.exists():  # noqa: ASYNC110
                    await asyncio.sleep(0.01)
            queued = asyncio.create_task(
                backend.call("fixture_echo", {"value": "cancelled"}, "client")
            )
            await asyncio.sleep(0.05)
            await backend.definitions()  # The worker reader remains responsive while queued.
            queued.cancel()
            with pytest.raises(asyncio.CancelledError):
                await queued
        finally:
            release.touch()
            await asyncio.wait_for(slow, 5)
        probe = json.loads(await backend.call("fixture_echo", {"value": "probe"}, "client"))
        assert probe["counter"] == 2, "A cancelled queued operation still executed"
