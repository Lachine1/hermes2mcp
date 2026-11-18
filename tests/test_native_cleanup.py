"""Original native teardown must continue when one resource fails to close."""

import json
import os
import subprocess
from pathlib import Path

import pytest
from test_native import PYTHON, SOURCE

pytestmark = [pytest.mark.hermes, pytest.mark.skipif(not PYTHON, reason="real Hermes not selected")]


def test_native_context_release_closes_other_resources_after_delegate_close_failure(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "config.yaml").write_text(
        "model:\n  provider: custom\n  default: test-only-model\n"
        "  base_url: http://127.0.0.1:1/v1\n  context_length: 65536\n",
        encoding="utf-8",
    )
    environment = dict(os.environ)
    environment["HERMES_HOME"] = str(profile)
    environment["PYTHONPATH"] = os.pathsep.join(
        [SOURCE, str(Path(__file__).resolve().parents[1] / "src")]
    )
    script = r"""
import contextlib, json, sys
from unittest.mock import patch
with contextlib.redirect_stdout(sys.stderr):
    import model_tools
    import hermes_state_registry
    from agent.client_lifecycle import ClientLifecycleMixin
    from hermes2mcp.runtime import NativeRuntime
    runtime = NativeRuntime(model_tools, toolsets=["session_search", "delegation"])
    assert "error" not in json.loads(runtime.call(
        "session_search", {"query":"no matches", "limit":1}, "parent"
    ))
    assert "error" not in json.loads(runtime.call("delegate_task", {"action":"list"}, "parent"))
    task_id = "hermes2mcp-parent"
    session = runtime.sessions[task_id]
    recall_db = session._session_db
    agent = session._delegate_agent
    original_agent_close = agent.close
    original_release = hermes_state_registry.release
    original_cleanup = ClientLifecycleMixin._close_task_resources
    database_releases, task_cleanups = [], []
    def recorded_release(database):
        database_releases.append(database is recall_db)
        return original_release(database)
    def recorded_cleanup(owner, identity):
        task_cleanups.append(identity)
        return original_cleanup(owner, identity)
    observed = {}
    try:
        with (
            patch.object(
                agent, "close", side_effect=RuntimeError("Test-only delegate close failure")
            ),
            patch.object(hermes_state_registry, "release", recorded_release),
            patch.object(ClientLifecycleMixin, "_close_task_resources", recorded_cleanup),
        ):
            failed = False
            try:
                runtime.release("parent")
            except RuntimeError:
                failed = True
            observed = {
                "original_failure_preserved":failed,
                "database_cleared":session._session_db is None,
                "database_releases":list(database_releases),
                "task_cleanups":list(task_cleanups),
                "task_forgotten":task_id not in runtime.used_tasks,
            }
    finally:
        original_agent_close()
        session.close()
        runtime.close()
print(json.dumps(observed))
"""
    result = subprocess.run(
        [PYTHON, "-c", script],
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=90,
    )
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed["original_failure_preserved"], observed
    assert observed["database_cleared"], observed
    assert observed["database_releases"] == [True], observed
    assert observed["task_cleanups"] == ["hermes2mcp-parent"], observed
    assert observed["task_forgotten"], observed


def test_native_runtime_shutdown_releases_all_contexts_after_one_cleanup_failure(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    environment = dict(os.environ)
    environment["HERMES_HOME"] = str(profile)
    environment["PYTHONPATH"] = os.pathsep.join(
        [SOURCE, str(Path(__file__).resolve().parents[1] / "src")]
    )
    script = r"""
import contextlib, json, sys
from unittest.mock import patch
with contextlib.redirect_stdout(sys.stderr):
    import model_tools
    from hermes2mcp.runtime import NativeRuntime
    runtime = NativeRuntime(model_tools, toolsets=["todo"])
    for name in ("one", "two"):
        todos = [{"id":name, "content":"Native shutdown proof", "status":"pending"}]
        result = json.loads(runtime.call("todo_list", {"todos":todos}, name))
        assert "error" not in result, result
        assert result["todos"][0]["id"] == name, result
    original_release = runtime.release
    first_task = next(iter(runtime.used_tasks))
    failure = RuntimeError("Test-only cleanup failure")
    completed = []
    def failing_release(identity):
        original_release(identity)
        completed.append("hermes2mcp-" + identity)
        if "hermes2mcp-" + identity == first_task:
            raise failure
    try:
        with patch.object(runtime, "release", failing_release):
            error_preserved = False
            try:
                runtime.close()
            except RuntimeError as exc:
                error_preserved = exc is failure
            observed = {
                "error_preserved":error_preserved, "completed":sorted(completed),
                "remaining_tasks":sorted(runtime.used_tasks),
                "remaining_sessions":sorted(runtime.sessions),
            }
    finally:
        runtime.close()
print(json.dumps(observed))
"""
    result = subprocess.run(
        [PYTHON, "-c", script],
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=90,
    )
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed["error_preserved"], observed
    assert observed["completed"] == ["hermes2mcp-one", "hermes2mcp-two"], observed
    assert observed["remaining_tasks"] == [], observed
    assert observed["remaining_sessions"] == [], observed
