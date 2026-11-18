import os
import subprocess
import sys
from pathlib import Path


def cli(*args):
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    environment.pop("HERMES2MCP_TOKEN", None)
    return subprocess.run(
        [sys.executable, "-m", "hermes2mcp", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        timeout=20,
    )


def test_module_entrypoint_is_http_only_and_explains_native_python_selection():
    result = cli("--help")
    assert result.returncode == 0, result.stderr
    assert "--host" in result.stdout
    assert "--port" in result.stdout
    assert "--hermes-python" in result.stdout
    assert "--hermes-path" in result.stdout
    assert "stdio" not in result.stdout.lower()


def test_cli_rejects_remote_binding_without_a_token_before_startup():
    result = cli("--host", "0.0.0.0")
    assert result.returncode == 2
    assert "token" in result.stderr.lower()


def test_cli_reports_the_adapter_release_version():
    result = cli("--version")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "hermes2mcp 0.2.1"
