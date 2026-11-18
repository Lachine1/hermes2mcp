import json

import pytest


@pytest.mark.parametrize(
    ("raw", "structured", "is_error"),
    [
        ("plain text — exact\n", None, False),
        (
            '{ "success": true, "nested": {"value": 1} }\n',
            {"success": True, "nested": {"value": 1}},
            False,
        ),
        ('{ "error": "upstream failure" }', {"error": "upstream failure"}, True),
        ('{"success": false}', {"success": False}, True),
        ('{"error": null, "output": "ok"}', {"error": None, "output": "ok"}, False),
        ('[1, "two"]', None, False),
        (
            {"content": [{"type": "image_url", "url": "data:abc"}]},
            {"content": [{"type": "image_url", "url": "data:abc"}]},
            False,
        ),
    ],
)
def test_native_tool_results_keep_original_text_and_error_status(raw, structured, is_error):
    from hermes2mcp.bridge import result_from_hermes

    wire = result_from_hermes(raw).model_dump(by_alias=True, exclude_none=True)
    expected_text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
    assert wire["content"] == [{"type": "text", "text": expected_text}]
    assert wire["isError"] is is_error
    assert wire.get("structuredContent") == structured
