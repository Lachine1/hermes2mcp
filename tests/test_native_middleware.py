"""Compare native execution middleware with Hermes's own invocation path."""

import json
import os
import subprocess
from pathlib import Path

import pytest
from test_native import PYTHON, SOURCE

pytestmark = [pytest.mark.hermes, pytest.mark.skipif(not PYTHON, reason="real Hermes not selected")]


def test_inline_tools_preserve_original_native_execution_middleware(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    env = dict(os.environ)
    env["HERMES_HOME"] = str(profile)
    env["PYTHONPATH"] = os.pathsep.join(
        [
            SOURCE,
            str(Path(__file__).resolve().parents[1] / "src"),
        ]
    )
    script = """
import contextlib, json, sys
with contextlib.redirect_stdout(sys.stderr):
    import model_tools
    from agent.agent_runtime_helpers import invoke_tool
    from hermes_cli.plugins import PluginContext, PluginManifest, _delivery_manager
    from hermes2mcp.runtime import NativeRuntime, NativeSession

    def around_tool(tool_name, args, next_call):
        result = json.loads(next_call(args))
        result["native_execution_middleware"] = tool_name
        return json.dumps(result)

    context = PluginContext(PluginManifest(name="local-test-middleware"), _delivery_manager())
    handle = context.register_middleware("tool_execution", around_tool)
    runtime = NativeRuntime(model_tools, toolsets=["todo"])
    try:
        baseline = json.loads(invoke_tool(
            NativeSession(runtime, "baseline"), "todo_list", {}, "baseline"
        ))
        candidate = json.loads(runtime.call("todo_list", {}, "candidate"))
    finally:
        runtime.close()
        handle.dispose()
print(json.dumps({"baseline": baseline, "candidate": candidate}))
"""
    process = subprocess.run(
        [PYTHON, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert process.returncode == 0, process.stderr
    results = json.loads(process.stdout)
    assert results["baseline"]["native_execution_middleware"] == "todo_list", results
    assert results["candidate"].get("native_execution_middleware") == "todo_list", results
