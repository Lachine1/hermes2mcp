from copy import deepcopy


def test_converts_a_hermes_schema_without_changing_its_contract():
    from hermes2mcp.bridge import tool_from_definition

    schema = {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file.\nKeep this exact description — Unicode included.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Original path."},
                    "limit": {"type": "integer", "minimum": 1, "default": 2000},
                    "mode": {"anyOf": [{"const": "text"}, {"type": "null"}]},
                },
                "required": ["path"],
                "additionalProperties": False,
                "$defs": {"unused": {"type": "string"}},
            },
        },
    }
    original = deepcopy(schema)
    tool = tool_from_definition(schema)
    assert tool.name == schema["function"]["name"]
    assert tool.description == schema["function"]["description"]
    wire = tool.model_dump(by_alias=True, exclude_none=True)
    assert wire["inputSchema"] == schema["function"]["parameters"]
    assert schema == original
    parameters = tool.input_schema
    parameters["properties"]["path"]["description"] = "changed client copy"
    assert schema == original
