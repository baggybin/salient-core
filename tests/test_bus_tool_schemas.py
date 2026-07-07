"""Bus-tool schema regression suite.

Background — Salient's bus uses the Claude Agent SDK's `@tool` decorator.
When the decorator is given the shorthand `{"name": type}` dict for an
input schema, the SDK auto-marks EVERY field as required. That's fine for
tools where every param is required, but the bus has several "filter"
tools (prior_actions today, others over time) where the operator-facing
description marks fields "Optional." The shorthand and the description
silently disagree — Claude Code reads the description and calls with only
the fields it wants, then the MCP server rejects the call with
`'X' is a required property`.

Two safeguards here:

- `test_prior_actions_accepts_empty_args` — specific behavioural test
  for the call that triggered this work (deepseek_red fumbled it twice
  before answering a pure-methodology question).
- `test_bus_tool_optionality_matches_description` — broader CHECK that
  scans every registered bus tool. For each tool, if the description
  marks a param "Optional", the JSON schema's `required` list must NOT
  contain that param. Catches future tools that introduce the same trap.
"""

from __future__ import annotations

import re
import unittest
from unittest.mock import MagicMock

from salient_core import bus


def _build_bus_tools() -> list:
    """Construct the bus once, scrape the registered tool objects out.

    `make_bus` packs the tools inside an opaque `create_sdk_mcp_server`
    return value — the SDK keeps them in a closure for `list_tools()`,
    with no public surface to walk them. Easiest reliable extraction is
    to monkey-patch the constructor for the duration of the call: we
    record the `tools=[...]` argument and re-invoke the real factory so
    the rest of `make_bus` keeps working. A MagicMock stands in for the
    daemon — these tests only inspect `input_schema` / `description`,
    they never call a tool body.
    """
    captured: list = []
    real_factory = bus.create_sdk_mcp_server

    def _capture(*args, **kwargs):
        # `create_sdk_mcp_server(name, version, tools)` — bus.make_bus
        # calls it with all three as kwargs today, but accept positional
        # too in case the SDK or bus is refactored.
        tools = kwargs.get("tools")
        if tools is None and len(args) > 2:
            tools = args[2]
        captured[:] = list(tools or [])
        return real_factory(*args, **kwargs)

    daemon_stub = MagicMock()
    bus.create_sdk_mcp_server = _capture  # type: ignore[assignment]
    try:
        bus.make_bus(daemon=daemon_stub, owner="testing")
    finally:
        bus.create_sdk_mcp_server = real_factory  # type: ignore[assignment]
    if not captured:
        raise RuntimeError("bus.make_bus did not register any tools")
    return captured


def _schema_for(tool_obj) -> dict:
    """Return the JSON schema the SDK would publish for a tool.

    The SDK only computes `required` lazily at server construction; for
    tools that hand in a full JSON schema dict we get it back as-is, and
    for the `{name: type}` shorthand we synthesize the same shape the
    SDK would (every param required) so the optionality check is honest.
    """
    schema = tool_obj.input_schema
    if isinstance(schema, dict) and (
        "type" in schema and "properties" in schema and isinstance(schema["type"], str)
    ):
        return schema
    if isinstance(schema, dict):
        return {
            "type": "object",
            "properties": {k: {"type": "string"} for k in schema},
            "required": list(schema.keys()),
        }
    # TypedDict or other — bail; the SDK handles those correctly because
    # NotRequired is explicit. Treat as fully optional for the purposes
    # of this check.
    return {"type": "object", "properties": {}, "required": []}


_OPTIONAL_PARAM_RE = re.compile(
    # Matches lines like "  target — ... Optional." or
    # "  limit  — ... Optional (default 20)." The `—` is a literal
    # em-dash that Salient's bus descriptions use as the param/desc
    # separator.
    r"^\s+(?P<name>[a-z_][a-z0-9_]*)\s+—.*?\bOptional\b",
    re.MULTILINE,
)

# Catches the "0 / omitted = no hint" / "Empty / omitted = no
# constraint" / "(default X)"-without-Optional phrasings that earlier
# bus tools (ask_agent, kg_query) used to soft-document a param as
# optional WITHOUT the literal word "Optional". Those slipped past the
# stricter regex above and produced 'required property' refusals at
# runtime. If a tool description uses any of these soft-optional
# patterns, the param must NOT be in the schema's `required` list.
_SOFT_OPTIONAL_PARAM_RE = re.compile(
    r"^\s+(?P<name>[a-z_][a-z0-9_]*)\s+—.*?"
    r"(?:omitted\s*=\s*no|\(default\s|default:\s)",
    re.MULTILINE | re.IGNORECASE,
)


def _optional_params_from_description(description: str) -> set[str]:
    """Pull every parameter name from a bus-tool description that's
    marked Optional — either via the literal word "Optional" or via
    a soft-optional phrasing (omitted = no..., (default X), etc.)."""
    strict = {m.group("name") for m in _OPTIONAL_PARAM_RE.finditer(description)}
    soft = {m.group("name") for m in _SOFT_OPTIONAL_PARAM_RE.finditer(description)}
    return strict | soft


class TestPriorActionsSchema(unittest.TestCase):
    """Specific behavioural test for the call that motivated this work."""

    def setUp(self):
        tools = _build_bus_tools()
        self.tool = next(t for t in tools if t.name == "prior_actions")

    def test_prior_actions_accepts_empty_args(self):
        # prior_actions documents every param as Optional, so the JSON
        # schema must declare `required: []`. An external caller (small
        # local model, DeepSeek) reading the description should be able
        # to invoke prior_actions with `{}` and get a result.
        schema = _schema_for(self.tool)
        self.assertEqual(
            schema.get("required", []),
            [],
            "prior_actions must accept zero args — every parameter is "
            "documented Optional in the tool description",
        )

    def test_prior_actions_advertises_filter_params(self):
        schema = _schema_for(self.tool)
        props = set(schema.get("properties", {}).keys())
        # Sanity: every Optional-marked parameter from the description
        # is present in the schema. If someone removes a property the
        # description should be updated to match.
        described = _optional_params_from_description(self.tool.description or "")
        missing = described - props
        self.assertFalse(
            missing,
            f"prior_actions description mentions Optional param(s) {missing} "
            f"that are not in the JSON schema properties",
        )


class TestAskAgentSchema(unittest.TestCase):
    """ask_agent grew max_turns + deliverable as Optional envelope
    params in the delegation work, but kept the `{name: type}`
    shorthand — meaning every call without an envelope hit
    'max_turns is a required property'. Specific test so this can't
    silently regress."""

    def setUp(self):
        tools = _build_bus_tools()
        self.tool = next(t for t in tools if t.name == "ask_agent")

    def test_only_name_and_prompt_required(self):
        schema = _schema_for(self.tool)
        self.assertEqual(
            set(schema.get("required", [])),
            {"name", "prompt"},
            "ask_agent should require name + prompt; max_turns and "
            "deliverable are Optional delegation-envelope hints. "
            "Marking them required broke every basic delegation that "
            "didn't bother with the envelope.",
        )

    def test_max_turns_and_deliverable_present_in_properties(self):
        schema = _schema_for(self.tool)
        props = set(schema.get("properties", {}).keys())
        self.assertIn("max_turns", props)
        self.assertIn("deliverable", props)


class TestBusToolOptionalityCheck(unittest.TestCase):
    """Broader regression check across every registered bus tool.

    Runs as a single test that walks the full tool list. If any tool's
    description marks a param `Optional` but the JSON schema still lists
    it under `required`, fail with a clear message naming the offender.
    Catches the next contributor adding a tool with the `{name: type}`
    shorthand when their description says some params are optional.
    """

    def test_bus_tool_optionality_matches_description(self):
        tools = _build_bus_tools()
        self.assertGreater(len(tools), 0, "no bus tools registered")
        mismatches: list[str] = []
        for t in tools:
            description = t.description or ""
            optional_in_desc = _optional_params_from_description(description)
            if not optional_in_desc:
                continue
            schema = _schema_for(t)
            required = set(schema.get("required", []))
            wrongly_required = optional_in_desc & required
            if wrongly_required:
                mismatches.append(
                    f"{t.name}: description marks {sorted(wrongly_required)} "
                    f"Optional but the schema lists them as required"
                )
        self.assertFalse(
            mismatches,
            "bus-tool schema/description optionality drift:\n  - " + "\n  - ".join(mismatches),
        )


class TestAuditModelConstraints(unittest.TestCase):
    """The @bus_tool migration of _audit: read_evidence's de-require + the ge=/le=
    floors/ceilings the handlers used to apply (or silently didn't)."""

    def test_read_evidence_offset_length_now_optional(self):
        from pydantic import ValidationError

        from salient_core.bus._audit import _ReadEvidenceArgs

        # The de-require fix: sha alone validates; offset/length take defaults.
        m = _ReadEvidenceArgs(sha="abc123")
        self.assertEqual((m.offset, m.length), (0, 8192))
        # Ceiling is now enforced (prose promised "capped at 32 KB per call").
        with self.assertRaises(ValidationError):
            _ReadEvidenceArgs(sha="a", length=32769)
        with self.assertRaises(ValidationError):
            _ReadEvidenceArgs(sha="a", offset=-1)

    def test_prior_actions_since_minutes_none_default_and_floor(self):
        from pydantic import ValidationError

        from salient_core.bus._audit import _PriorActionsArgs

        m = _PriorActionsArgs()
        self.assertIsNone(m.since_minutes)  # None ⇒ unbounded (not 0 = "since now")
        self.assertEqual(m.limit, 20)
        # since_minutes=0 would mean "since now" under the old handler; now ge=1.
        with self.assertRaises(ValidationError):
            _PriorActionsArgs(since_minutes=0)


class TestAskAgentsModelConstraints(unittest.TestCase):
    """ask_agents' nested/enum/dynamic-default surface, now enforced by the model
    (the handler used to check these by hand)."""

    def _kids(self, n=1):
        return [{"name": f"a{i}", "prompt": "p"} for i in range(n)]

    def test_defaults_and_nested_shape(self):
        from salient_core.bus._delegation import _AskAgentsArgs

        m = _AskAgentsArgs(children=self._kids())
        self.assertIsNone(m.concurrency)  # None ⇒ auto (min(children, 10))
        self.assertEqual(m.aggregate, "all")
        # children validated into ask_agent envelopes with their own defaults.
        self.assertEqual((m.children[0].max_turns, m.children[0].prefer_primary), (0, False))

    def test_bounds_and_enum_enforced(self):
        from pydantic import ValidationError

        from salient_core.bus._delegation import _AskAgentsArgs

        with self.assertRaises(ValidationError):
            _AskAgentsArgs(children=[])  # min_length=1
        with self.assertRaises(ValidationError):
            _AskAgentsArgs(children=self._kids(21))  # max_length=20
        with self.assertRaises(ValidationError):
            _AskAgentsArgs(children=self._kids(), concurrency=25)  # le=20
        with self.assertRaises(ValidationError):
            _AskAgentsArgs(children=self._kids(), aggregate="sometimes")  # not in enum
        with self.assertRaises(ValidationError):
            _AskAgentsArgs(children=[{"name": "a"}])  # child missing prompt

    def test_explicit_null_concurrency_is_auto(self):
        from salient_core.bus._delegation import _AskAgentsArgs

        # An explicit null must mean auto (as it did on the wire before), which a
        # falsy-0 sentinel would have broken.
        self.assertIsNone(_AskAgentsArgs(children=self._kids(), concurrency=None).concurrency)


# NOTE: cred_record schema tests moved to the salient-security package
# (tests/test_credentials.py) — the credentials bus tool now lives there.


class TestDiscoveryModelConstraints(unittest.TestCase):
    """_discovery: list_agents.filter de-require. (evasion_map's model tests
    moved to the salient-security package.)"""

    def test_list_agents_filter_de_required(self):
        from salient_core.bus._discovery import _ListAgentsArgs

        # The description documents empty ⇒ all agents, so {} must validate.
        self.assertEqual(_ListAgentsArgs().filter, "")

    def test_context_list_filter_de_required(self):
        from salient_core.bus._context import _ContextListArgs

        # Consistency echo of list_agents.filter: empty / '*' ⇒ all keys, so {}
        # must validate to the default "" rather than error on a missing filter.
        self.assertEqual(_ContextListArgs().filter, "")


class TestKgModelConstraints(unittest.TestCase):
    """_kg: de-require, the confidence [0,1] domain, the deliberately-unbounded
    min_score threshold, and record_review.grade's advertised-not-enforced enum."""

    def test_kg_neighbors_de_required(self):
        from salient_core.bus._kg import _KgNeighborsArgs

        m = _KgNeighborsArgs(entity="host:10.0.0.1")  # depth/limit now optional
        self.assertEqual((m.depth, m.limit), (1, 50))

    def test_kg_assert_confidence_domain_and_ttl_nullable(self):
        from pydantic import ValidationError

        from salient_core.bus._kg import _KgAssertArgs

        base = {"subject": "a", "predicate": "p", "object": "o"}
        self.assertIsNone(_KgAssertArgs(**base).ttl_days)  # omit ⇒ engagement default
        self.assertEqual(_KgAssertArgs(**base, confidence=0.0).confidence, 0.0)
        self.assertEqual(_KgAssertArgs(**base, ttl_days=0).ttl_days, 0)  # explicit 0 ⇒ never
        self.assertEqual(_KgAssertArgs(**base, ttl_days=-1).ttl_days, -1)  # negative ⇒ never
        with self.assertRaises(ValidationError):
            _KgAssertArgs(**base, confidence=1.5)  # le=1

    def test_kg_assert_ttl_rejects_inf_nan(self):
        # ttl_days writes a persisted expiry; NaN would slip through `ttl > 0`
        # (always False) to silently mean "never expires". allow_inf_nan=False
        # rejects inf/NaN at validation while leaving the meaningful sentinels.
        from pydantic import ValidationError

        from salient_core.bus._kg import _KgAssertArgs

        base = {"subject": "a", "predicate": "p", "object": "o"}
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(ValidationError):
                _KgAssertArgs(**base, ttl_days=bad)

    def test_min_score_is_unbounded(self):
        from salient_core.bus._kg import _KgSemanticQueryArgs

        # A threshold vs the (clamped) scores: >1 = match nothing, <0 = match all
        # — both meaningful, so NO ge/le.
        self.assertEqual(_KgSemanticQueryArgs(text="t", min_score=2.0).min_score, 2.0)
        self.assertEqual(_KgSemanticQueryArgs(text="t", min_score=-1.0).min_score, -1.0)
        # ...but inf/NaN are garbage (NaN silently matches nothing) — rejected,
        # matching the ttl_days guard. The domain stays otherwise unbounded.
        from pydantic import ValidationError

        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(ValidationError):
                _KgSemanticQueryArgs(text="t", min_score=bad)

    def test_record_review_grade_advertises_enum_but_model_does_not_enforce(self):
        from salient_core.bus._common import _clean_tool_schema
        from salient_core.bus._kg import _RecordReviewArgs
        from salient_core.tutor.schedule import GRADES

        # The enum is advertised (sourced from GRADES) for the model to see...
        schema = _clean_tool_schema(_RecordReviewArgs.model_json_schema())
        self.assertEqual(schema["properties"]["grade"]["enum"], list(GRADES))
        # ...but json_schema_extra is not enforced by pydantic — normalize_grade
        # (the handler) stays the single, case-lenient validator.
        self.assertEqual(_RecordReviewArgs(topic="t", grade="Good").grade, "Good")


if __name__ == "__main__":
    unittest.main()
