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
