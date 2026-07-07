"""The bus_tool decorator: schema-from-Pydantic + runtime validation + the
.handler (wire) / .trusted (in-process, typed flags) split. Machinery lands
dark here — no real bus tool is migrated onto it yet.
"""

from __future__ import annotations

import unittest

from pydantic import BaseModel, ConfigDict

from salient_core.bus import BusFlags
from salient_core.bus._common import (
    _clean_tool_schema,
    bus_tool,
)


class _M(BaseModel):
    agent: str
    key: str


class _Opt(BaseModel):
    name: str
    limit: int = 20
    note: str | None = None


class CleanSchemaTests(unittest.TestCase):
    def test_strips_titles_and_keeps_shape(self):
        cleaned = _clean_tool_schema(_M.model_json_schema())
        # no cosmetic titles (root or per-property)
        self.assertNotIn("title", cleaned)
        for prop in cleaned["properties"].values():
            self.assertNotIn("title", prop)
        self.assertEqual(cleaned["properties"]["agent"], {"type": "string"})
        self.assertEqual(cleaned["required"], ["agent", "key"])

    def test_field_literally_named_title_is_preserved(self):
        # The title-strip removes the COSMETIC schema keyword, never a field
        # whose NAME happens to be "title" — else that field would vanish from
        # the advertised schema while validation still requires it.
        class _HasTitle(BaseModel):
            title: str
            body: str = ""

        cleaned = _clean_tool_schema(_HasTitle.model_json_schema())
        self.assertIn("title", cleaned["properties"])
        self.assertEqual(cleaned["properties"]["title"], {"type": "string"})
        self.assertEqual(cleaned["required"], ["title"])

    def test_strips_default_keyword(self):
        # An optional field needs a Pydantic default (that's what drops it from
        # `required`), but the default is a server-side fill, not wire contract.
        # Stripping it lets `field: T = <val>` reproduce the bare `{"type": ...}`
        # shape the pre-migration inline/shorthand schemas advertised.
        cleaned = _clean_tool_schema(_Opt.model_json_schema())
        self.assertEqual(cleaned["properties"]["limit"], {"type": "integer"})
        self.assertNotIn("default", cleaned["properties"]["limit"])
        self.assertEqual(cleaned["required"], ["name"])

    def test_field_literally_named_default_is_preserved(self):
        # As with "title", the strip removes the COSMETIC schema keyword, never a
        # field whose NAME happens to be "default".
        class _HasDefault(BaseModel):
            default: str
            other: str = ""

        cleaned = _clean_tool_schema(_HasDefault.model_json_schema())
        self.assertIn("default", cleaned["properties"])
        self.assertEqual(cleaned["properties"]["default"], {"type": "string"})
        self.assertEqual(cleaned["required"], ["default"])

    def test_recursive_model_raises_not_loops(self):
        # A self-referential model emits a cyclic $ref; inlining must raise a
        # clear error rather than RecursionError.
        class _Node(BaseModel):
            val: int
            child: _Node | None = None

        _Node.model_rebuild()
        with self.assertRaises(ValueError):
            _clean_tool_schema(_Node.model_json_schema())


class SchemaGenerationTests(unittest.TestCase):
    def test_bus_tool_schema_adds_additional_properties_false(self):
        @bus_tool("t", "d", _M)
        async def _h(args):
            return {}

        self.assertIs(_h.input_schema["additionalProperties"], False)
        self.assertEqual(_h.input_schema["required"], ["agent", "key"])

    def test_optional_fields_absent_from_required(self):
        @bus_tool("t", "d", _Opt)
        async def _h(args):
            return {}

        self.assertEqual(_h.input_schema["required"], ["name"])
        self.assertIn("limit", _h.input_schema["properties"])

    def test_extra_forbid_model_rejected_at_registration(self):
        # extra='forbid' would flip the wire path from drop+log to raise on a
        # wire-injected `_`-key — regressing the step-1 security posture. Reject
        # it loudly at decoration time.
        class _Strict(BaseModel):
            model_config = ConfigDict(extra="forbid")
            name: str

        with self.assertRaises(ValueError):

            @bus_tool("t", "d", _Strict)
            async def _h(args):
                return {}


class ValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_required_returns_friendly_error(self):
        @bus_tool("t", "d", _M)
        async def _h(args):
            return {"content": [{"type": "text", "text": "ok"}]}

        out = await _h.handler({"agent": "a"})  # missing key
        self.assertTrue(out.get("is_error"))
        self.assertIn("key", out["content"][0]["text"])

    async def test_handler_receives_only_declared_fields(self):
        seen = {}

        @bus_tool("t", "d", _M)
        async def _h(args):
            seen.update(args)
            return {}

        await _h.handler({"agent": "a", "key": "k", "extra": "x", "_flag": True})
        self.assertEqual(seen, {"agent": "a", "key": "k"})  # extras + _keys dropped


class RoutedTests(unittest.IsolatedAsyncioTestCase):
    async def test_routed_trusted_passes_typed_flags(self):
        got = {}

        @bus_tool("t", "d", _M, routed=True)
        async def _h(args, flags):
            got["flags"] = flags
            return {}

        await _h.trusted({"agent": "a", "key": "k"}, flags=BusFlags(skip_redispatch_gate=True))
        self.assertTrue(got["flags"].skip_redispatch_gate)

    async def test_routed_wire_path_gets_default_flags(self):
        got = {}

        @bus_tool("t", "d", _M, routed=True)
        async def _h(args, flags):
            got["flags"] = flags
            return {}

        await _h.handler({"agent": "a", "key": "k"})
        self.assertEqual(got["flags"], BusFlags())

    async def test_routed_trusted_rejects_stray_underscore_key(self):
        @bus_tool("t", "d", _M, routed=True)
        async def _h(args, flags):
            return {}

        with self.assertRaises(ValueError):
            await _h.trusted({"agent": "a", "key": "k", "_skip_redispatch_gate": True})

    async def test_plain_trusted_takes_no_flags(self):
        got = {}

        @bus_tool("t", "d", _M)
        async def _h(args):
            got.update(args)
            return {}

        await _h.trusted({"agent": "a", "key": "k"})
        self.assertEqual(got, {"agent": "a", "key": "k"})


if __name__ == "__main__":
    unittest.main()
