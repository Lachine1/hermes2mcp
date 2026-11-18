"""Lossless conversion from Hermes tool definitions to MCP definitions."""

import json
from copy import deepcopy
from typing import Any

from mcp.types import CallToolResult, Tool


def tool_from_definition(definition: dict[str, Any]) -> Tool:
    """Change only the envelope: Hermes ``parameters`` becomes MCP ``inputSchema``."""
    function = deepcopy(definition.get("function", definition))
    function["inputSchema"] = function.pop("parameters")
    return Tool.model_validate(function)


def result_from_hermes(result: Any) -> CallToolResult:
    """Keep native text untouched, with a structured copy when the result is a JSON object."""
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
    try:
        payload = json.loads(text)
    except ValueError:
        payload = None
    structured = payload if isinstance(payload, dict) else None
    is_error = bool(structured and (structured.get("error") or structured.get("success") is False))
    wire: dict[str, Any] = {
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
    }
    if structured is not None:
        wire["structuredContent"] = structured
    return CallToolResult.model_validate(wire)
