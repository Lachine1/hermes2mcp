"""Exercise scheduling against Hermes's real dispatch and admission policy."""

import json
import os
import subprocess
from pathlib import Path

import pytest
from test_native import PYTHON, SOURCE

pytestmark = [pytest.mark.hermes, pytest.mark.skipif(not PYTHON, reason="real Hermes not selected")]


@pytest.mark.parametrize("serial_policy", [False, True])
def test_parallel_safe_native_reads_share_a_context_without_queue_blocking(tmp_path, serial_policy):
    profile = tmp_path / "profile"
    profile.mkdir()
    slow_path, fast_path = tmp_path / "slow.txt", tmp_path / "fast.txt"
    slow_path.write_text("Native slow read\n", encoding="utf-8")
    fast_path.write_text("Native independent read — café\n", encoding="utf-8")
    environment = dict(os.environ)
    environment["HERMES_HOME"] = str(profile)
    environment["PYTHONPATH"] = os.pathsep.join(
        [SOURCE, str(Path(__file__).resolve().parents[1] / "src")]
    )
    script = r"""
import contextlib, json, queue, sys, threading, time
from pathlib import Path
with contextlib.redirect_stdout(sys.stderr):
    import model_tools
    from hermes_cli.plugins import PluginContext, PluginManifest, _delivery_manager
    from hermes2mcp.runtime import NativeRuntime
    from hermes2mcp.dispatch import NativeDispatch
    slow_path, fast_path, serial_policy = sys.argv[1:]
    ready, release = threading.Event(), threading.Event()
    replies = queue.Queue()
    trace = []
    def around_tool(tool_name, args, next_call):
        trace.append((tool_name, args.get("path"), "start", time.perf_counter()))
        if tool_name == "read_file" and args.get("path") == slow_path:
            ready.set()
            if not release.wait(20):
                raise TimeoutError("Test gate was not released")
        result = next_call(args)
        trace.append((tool_name, args.get("path"), "done", time.perf_counter()))
        return result
    plugin = PluginContext(PluginManifest(name="native-parallel-test"), _delivery_manager())
    handle = plugin.register_middleware("tool_execution", around_tool)
    runtime = NativeRuntime(model_tools, toolsets=["file"])
    runtime.definitions()
    if serial_policy == "True":
        runtime.parallel_safe = lambda session_id, requests: False
    dispatch = NativeDispatch(runtime, replies.put)
    first = None
    try:
        dispatch.submit({"id":"slow", "op":"call", "name":"read_file",
                         "arguments":{"path":slow_path}, "session_id":"reader"})
        if not ready.wait(5):
            raise AssertionError("Native middleware did not reach the controlled gate")
        dispatch.submit({"id":"fast", "op":"call", "name":"read_file",
                         "arguments":{"path":fast_path}, "session_id":"reader"})
        try:
            first = replies.get(timeout=10)
        except queue.Empty:
            pass
    finally:
        release.set()
        dispatch.close()
        runtime.close()
        handle.dispose()
    results = [first] if first is not None else []
    while not replies.empty():
        results.append(replies.get_nowait())
print(json.dumps(
    {"first_before_release":first, "results":results, "trace":trace}, ensure_ascii=False
))
"""
    result = subprocess.run(
        [PYTHON, "-c", script, str(slow_path), str(fast_path), str(serial_policy)],
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=90,
    )
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert all(item["ok"] for item in observed["results"]), observed
    if serial_policy:
        assert observed["first_before_release"] is None, observed
        assert [item["id"] for item in observed["results"]] == ["slow", "fast"], observed
        return
    assert observed["first_before_release"] is not None, observed
    assert observed["first_before_release"]["id"] == "fast", observed
    assert "Native independent read — café" in observed["first_before_release"]["result"]


def test_parallel_first_inline_queries_acquire_one_native_context_and_database_lease(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    environment = dict(os.environ)
    environment["HERMES_HOME"] = str(profile)
    environment["PYTHONPATH"] = os.pathsep.join(
        [SOURCE, str(Path(__file__).resolve().parents[1] / "src")]
    )
    script = r"""
import contextlib, json, queue, sys, threading
from unittest.mock import patch
with contextlib.redirect_stdout(sys.stderr):
    import model_tools
    import hermes_state_registry
    from hermes2mcp.runtime import NativeRuntime, NativeSession
    from hermes2mcp.dispatch import NativeDispatch
    runtime = NativeRuntime(model_tools, toolsets=["session_search"])
    runtime.definitions()
    replies = queue.Queue()
    initialized, acquired = [], []
    init_ready, second_init, init_release = threading.Event(), threading.Event(), threading.Event()
    db_ready, second_db, db_release = threading.Event(), threading.Event(), threading.Event()
    original_init, original_acquire = NativeSession.__init__, hermes_state_registry.acquire
    def gated_init(self, runtime, task_id):
        initialized.append(task_id)
        init_ready.set()
        if len(initialized) > 1:
            second_init.set()
        if not init_release.wait(15):
            raise TimeoutError("Native context initialization gate was not released")
        original_init(self, runtime, task_id)
    def gated_acquire(*args, **kwargs):
        acquired.append(True)
        db_ready.set()
        if len(acquired) > 1:
            second_db.set()
        if not db_release.wait(15):
            raise TimeoutError("Native database acquisition gate was not released")
        return original_acquire(*args, **kwargs)
    with (
        patch.object(NativeSession, "__init__", gated_init),
        patch.object(hermes_state_registry, "acquire", gated_acquire),
    ):
        dispatch = NativeDispatch(runtime, replies.put)
        try:
            dispatch.submit({
                "id":"one", "op":"call", "name":"session_search",
                "arguments":{"query":"no first matches", "limit":1},
                "session_id":"recaller",
            })
            if not init_ready.wait(5):
                raise AssertionError("Original inline dispatch did not initialize native context")
            dispatch.submit({
                "id":"two", "op":"call", "name":"session_search",
                "arguments":{"query":"no second matches", "limit":1},
                "session_id":"recaller",
            })
            second_init.wait(2)
            init_release.set()
            if not db_ready.wait(5):
                raise AssertionError("Original session search did not acquire its native database")
            second_db.wait(2)
        finally:
            init_release.set()
            db_release.set()
            dispatch.close()
            runtime.close()
    results = []
    while not replies.empty():
        results.append(replies.get_nowait())
print(json.dumps({
    "initializations":initialized, "database_acquisitions":len(acquired), "results":results
}))
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
    assert {item["id"] for item in observed["results"]} == {"one", "two"}, observed
    assert all(item["ok"] for item in observed["results"]), observed
    assert all("error" not in json.loads(item["result"]) for item in observed["results"]), observed
    assert observed["initializations"] == ["hermes2mcp-recaller"], observed
    assert observed["database_acquisitions"] == 1, observed
    assert (profile / "state.db").is_file()


def test_parallel_native_read_policy_hooks_have_distinct_call_identities(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    slow_path, fast_path = tmp_path / "guarded-slow.txt", tmp_path / "guarded-fast.txt"
    slow_path.write_text("Original slow native read\n", encoding="utf-8")
    fast_path.write_text("Original fast native read — café\n", encoding="utf-8")
    environment = dict(os.environ)
    environment["HERMES_HOME"] = str(profile)
    environment["PYTHONPATH"] = os.pathsep.join(
        [SOURCE, str(Path(__file__).resolve().parents[1] / "src")]
    )
    script = r"""
import contextlib, json, queue, sys, threading
with contextlib.redirect_stdout(sys.stderr):
    import model_tools
    from hermes_cli.plugins import PluginContext, PluginManifest, _delivery_manager
    from hermes2mcp.runtime import NativeRuntime
    from hermes2mcp.dispatch import NativeDispatch
    slow_path, fast_path = sys.argv[1:]
    ready, release = threading.Event(), threading.Event()
    replies = queue.Queue()
    before, after = [], []
    def guard(tool_name, args, tool_call_id=None, task_id=None, session_id=None):
        before.append({"path": args.get("path"), "call_id": tool_call_id,
                       "task_id": task_id, "session_id": session_id})
        if tool_name == "read_file" and args.get("path") == slow_path:
            ready.set()
            if not release.wait(20):
                raise TimeoutError("Native policy hook test gate was not released")
    def observe(tool_name, args, tool_call_id=None, task_id=None, session_id=None):
        after.append({"path": args.get("path"), "call_id": tool_call_id,
                      "task_id": task_id, "session_id": session_id})
    plugin = PluginContext(PluginManifest(name="native-policy-identity-test"), _delivery_manager())
    handles = [plugin.register_hook("pre_tool_call", guard),
               plugin.register_hook("post_tool_call", observe)]
    runtime = NativeRuntime(model_tools, toolsets=["file"])
    runtime.definitions()
    dispatch = NativeDispatch(runtime, replies.put)
    first = None
    try:
        dispatch.submit({"id":"slow", "op":"call", "name":"read_file",
                         "arguments":{"path":slow_path}, "session_id":"reader"})
        if not ready.wait(5):
            raise AssertionError("Original native policy hook did not reach the gate")
        dispatch.submit({"id":"fast", "op":"call", "name":"read_file",
                         "arguments":{"path":fast_path}, "session_id":"reader"})
        try:
            first = replies.get(timeout=5)
        except queue.Empty:
            pass
    finally:
        release.set()
        dispatch.close()
        runtime.close()
        for handle in handles:
            handle.dispose()
    results = [first] if first is not None else []
    while not replies.empty():
        results.append(replies.get_nowait())
print(json.dumps({"first_before_release":first, "results":results,
                  "pre_hooks":before, "post_hooks":after}, ensure_ascii=False))
"""
    result = subprocess.run(
        [PYTHON, "-c", script, str(slow_path), str(fast_path)],
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=90,
    )
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert {item["id"] for item in observed["results"]} == {"slow", "fast"}, observed
    assert all(item["ok"] for item in observed["results"]), observed
    assert all("error" not in json.loads(item["result"]) for item in observed["results"]), observed
    assert observed["first_before_release"]["id"] == "fast", observed
    assert "Original fast native read — café" in observed["first_before_release"]["result"]
    pre = {item["path"]: item for item in observed["pre_hooks"]}
    post = {item["path"]: item for item in observed["post_hooks"]}
    assert pre.keys() == post.keys() == {str(slow_path), str(fast_path)}, observed
    assert all(isinstance(item["call_id"], str) and item["call_id"] for item in pre.values())
    assert len({item["call_id"] for item in pre.values()}) == 2, observed
    assert all(pre[path]["call_id"] == post[path]["call_id"] for path in pre), observed
    assert all(
        item["task_id"] == item["session_id"] == "hermes2mcp-reader"
        for item in [*pre.values(), *post.values()]
    ), observed
