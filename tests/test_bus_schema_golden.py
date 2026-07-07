"""Golden-master snapshots of every bus tool's wire JSON schema.

Pins the exact ``input_schema`` each bus tool advertises to the model. This is
the safety net for the step-B reconciliation (migrating the inline JSON schema
literals to Pydantic-model-generated schemas via a ``bus_tool`` decorator): a
migrated tool must reproduce its golden byte-for-byte, so any drift — or an
intended change like the required-list fix — shows up as a reviewable diff in
the tool's ``.json`` file.

Regenerate after an intended change: ``UPDATE_BUS_GOLDENS=1 pytest -q``.
The tool list is derived from the live registry, so a new tool without a golden
(or a deleted tool with a stale golden) fails loudly.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace

from claude_agent_sdk import _python_type_to_json_schema

from salient_core.bus import make_bus_tools

_GOLDEN_DIR = Path(__file__).parent / "golden" / "bus_schemas"


def _wire_schema(input_schema) -> dict:
    """The JSON schema the SDK actually sends for a tool — mirrors the SDK's
    create_sdk_mcp_server._build_schema. A full JSON-schema dict passes through;
    the ``{field: python-type}`` shorthand expands to an object schema with
    EVERY field required (the SDK's behavior — and the small-model bug step B
    fixes by giving optional fields defaults)."""
    if isinstance(input_schema, dict):
        if (
            "type" in input_schema
            and "properties" in input_schema
            and isinstance(input_schema["type"], str)
        ):
            return input_schema
        properties = {
            name: _python_type_to_json_schema(py_type) for name, py_type in input_schema.items()
        }
        return {
            "type": "object",
            "properties": properties,
            "required": list(properties),
        }
    return {"type": "object", "properties": {}}


# The in-process routing-flag channel (step 1). These must NEVER appear in a
# tool's advertised wire schema — a model that could name them could set them.
_ROUTING_FLAG_KEYS = {
    "_skip_redispatch_gate",
    "_parent_call_id",
    "_skip_substitute_routing",
    "_swarm_fanout_approved",
}

# Every in-process-only side channel that rides inside args but must never be
# advertised: the routing flags PLUS `_job_capture` (the mutable job-id
# write-back dict). None may leak into any wire schema, anywhere.
_INPROCESS_KEYS = _ROUTING_FLAG_KEYS | {"_job_capture"}


def _tools() -> dict:
    # A bare stub daemon is enough to CONSTRUCT the tool closures (schemas are
    # static); handlers are never invoked here.
    tool_fns, _ = make_bus_tools(SimpleNamespace(), "owner")
    return {t.name: t for t in tool_fns}


def _canonical(schema: dict | None) -> str:
    return json.dumps(schema or {}, sort_keys=True, indent=2) + "\n"


class BusSchemaGoldenTests(unittest.TestCase):
    def test_wire_schemas_match_golden(self):
        tools = _tools()
        update = os.environ.get("UPDATE_BUS_GOLDENS")
        if update:
            _GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        for name, t in sorted(tools.items()):
            generated = _canonical(_wire_schema(t.input_schema))
            path = _GOLDEN_DIR / f"{name}.json"
            if update:
                path.write_text(generated)
                continue
            self.assertTrue(
                path.exists(),
                f"no golden for tool {name!r} — new tool? run UPDATE_BUS_GOLDENS=1",
            )
            self.assertEqual(
                generated,
                path.read_text(),
                f"wire schema for {name!r} changed — if intended, regenerate with "
                "UPDATE_BUS_GOLDENS=1 and review the .json diff",
            )
        if update:
            self.skipTest("goldens regenerated")

    def test_golden_set_matches_live_registry(self):
        if os.environ.get("UPDATE_BUS_GOLDENS"):
            self.skipTest("regenerating goldens")
        live = set(_tools())
        golden = {p.stem for p in _GOLDEN_DIR.glob("*.json")}
        self.assertEqual(
            live,
            golden,
            "golden set drifted from the live tool registry — run UPDATE_BUS_GOLDENS=1",
        )

    def test_no_routing_flag_advertised_in_any_schema(self):
        # Guards the step-1 security fix permanently: the wire schema must never
        # expose the in-process routing-flag channel to the model.
        for name, t in _tools().items():
            props = set(_wire_schema(t.input_schema).get("properties", {}))
            leaked = props & _ROUTING_FLAG_KEYS
            self.assertFalse(leaked, f"tool {name!r} advertises routing flag(s) {leaked}")

    def test_no_inprocess_side_channel_key_advertised_anywhere(self):
        # Broader net than the properties-only check above: NO in-process side
        # channel (routing flags OR _job_capture) may appear as a quoted key
        # ANYWHERE in the serialized schema — properties, `required`, a nested
        # object, or an enum — since a model that can name it can try to set it.
        for name, t in _tools().items():
            blob = json.dumps(_wire_schema(t.input_schema))
            leaked = {k for k in _INPROCESS_KEYS if f'"{k}"' in blob}
            self.assertFalse(leaked, f"tool {name!r} advertises in-process key(s) {leaked}")

    def test_all_tool_schemas_are_dict(self):
        # _wire_schema faithfully mirrors the SDK's _build_schema ONLY for a
        # dict input_schema (full JSON schema or {field: type} shorthand). The
        # SDK's is_typeddict branch is deliberately unmirrored; if a future tool
        # ships a TypedDict/class schema the golden would silently snapshot a
        # fiction. Fail loudly so _wire_schema is extended before that lands.
        for name, t in _tools().items():
            self.assertIsInstance(
                t.input_schema,
                dict,
                f"tool {name!r} has a non-dict input_schema — extend _wire_schema "
                "to mirror the SDK's is_typeddict branch before adding such a tool",
            )


if __name__ == "__main__":
    unittest.main()
