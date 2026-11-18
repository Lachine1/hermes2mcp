"""Test-only backend: real Python modules, not a substitute for Hermes acceptance tests."""

import json

COUNTER = 0
DEFINITION = {
    "type": "function",
    "function": {
        "name": "fixture_echo",
        "description": "Test-only echo. Exact description — no rewriting.",
        "parameters": {
            "type": "object",
            "properties": {"value": {"type": "string", "default": "unchanged"}},
            "additionalProperties": False,
        },
    },
}


def get_tool_definitions(**kwargs):
    assert kwargs.get("quiet_mode") is True
    return [DEFINITION]


def handle_function_call(name, arguments, **kwargs):
    global COUNTER
    COUNTER += 1
    print("native diagnostic must not corrupt private IPC")
    result = {
        "name": name,
        "arguments": arguments,
        "task_id": kwargs["task_id"],
        "counter": COUNTER,
    }
    if arguments.get("probe_stdin"):
        import subprocess
        import sys

        child = subprocess.run(
            [sys.executable, "-c", "import os,sys; sys.stdout.buffer.write(os.read(0,1))"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        result["inherited_stdin"] = child.stdout
    return json.dumps(result, ensure_ascii=False)
