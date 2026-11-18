from pathlib import Path


def test_repository_has_complete_operational_documentation():
    root = Path(__file__).resolve().parents[1]
    assert (root / "README.md").is_file(), "Missing operational README"
    readme = (root / "README.md").read_text(encoding="utf-8")
    for contract in (
        "Streamable HTTP",
        "/mcp",
        "HERMES2MCP_TOKEN",
        "--hermes-python",
        "--hermes-path",
    ):
        assert contract in readme
    assert "MIT License" in (root / "LICENSE").read_text(encoding="utf-8")
    assert (root / ".github" / "workflows" / "ci.yml").is_file()
