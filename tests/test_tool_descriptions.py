"""Guards the tool-description quality Glama scores: every parameter is explained in the
description prose (not only in the JSON schema) and descriptions are not stubs."""

import pytest

from signal_mcp.server import TOOLS


@pytest.mark.parametrize("tool", TOOLS, ids=lambda t: t.name)
def test_every_parameter_is_named_in_the_description(tool):
    props = (tool.input_schema or {}).get("properties", {})
    text = tool.description.lower()
    missing = [p for p in props if p.lower() not in text]
    assert not missing, f"{tool.name}: parameters not explained in the description: {missing}"


@pytest.mark.parametrize("tool", TOOLS, ids=lambda t: t.name)
def test_description_is_not_a_stub(tool):
    assert len(tool.description) >= 200, f"{tool.name} description is only {len(tool.description)} chars"


@pytest.mark.parametrize("tool", TOOLS, ids=lambda t: t.name)
def test_every_schema_property_has_its_own_description(tool):
    props = (tool.input_schema or {}).get("properties", {})
    bare = [k for k, v in props.items() if not v.get("description")]
    assert not bare, f"{tool.name}: schema properties without description: {bare}"
