"""Tests for the context_read cap and the new context slicer bus tools.

The bus tools are factory-built (closures over a `daemon` arg passed to
make_bus), which makes calling them in isolation awkward. Rather than
build a half-stubbed daemon mock, these tests:

  - exercise the cap-resolution helper directly
  - confirm the new tool names appear in the registration constants
  - confirm the module imports cleanly with the new wiring
  - assert the search/slice algorithms via a thin
    instrumentation harness that builds a real bus against a real
    ContextStore (no daemon process, just the store + a stub daemon
    object).
"""

import os
import unittest
from unittest import mock

from salient_core import bus as bus_mod
from salient_core.bus import ContextStore


class CapHelperTests(unittest.TestCase):
    """The _context_read_cap helper resolves env override + default."""

    def test_default_when_env_unset(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SALIENT_CONTEXT_READ_CAP", None)
            self.assertEqual(bus_mod._context_read_cap(), bus_mod._DEFAULT_CONTEXT_READ_CAP)

    def test_env_override(self):
        with mock.patch.dict(os.environ, {"SALIENT_CONTEXT_READ_CAP": "1000"}):
            self.assertEqual(bus_mod._context_read_cap(), 1000)

    def test_env_zero_falls_back(self):
        with mock.patch.dict(os.environ, {"SALIENT_CONTEXT_READ_CAP": "0"}):
            self.assertEqual(bus_mod._context_read_cap(), bus_mod._DEFAULT_CONTEXT_READ_CAP)

    def test_env_garbage_falls_back(self):
        with mock.patch.dict(os.environ, {"SALIENT_CONTEXT_READ_CAP": "garbage"}):
            self.assertEqual(bus_mod._context_read_cap(), bus_mod._DEFAULT_CONTEXT_READ_CAP)


class ToolRegistrationTests(unittest.TestCase):
    """The new context slicers appear in the public tool-name constant."""

    EXPECTED = [
        "context_grep",
        "context_section",
        "context_head",
        "context_tail",
        "context_lines",
        "context_count",
        "context_summary",
    ]

    def test_all_new_tools_in_names_tuple(self):
        for name in self.EXPECTED:
            self.assertIn(name, bus_mod._BUS_TOOL_NAMES, f"{name!r} missing from _BUS_TOOL_NAMES")


# ── Behaviour tests via a real bus against a real ContextStore ────────────
# We build a minimal daemon-like stub with just the bits the context
# slicers touch (a ContextStore and an empty runners dict). The bus
# wiring sets up real tool functions and we invoke them through the
# MCP server's tools list.


class _StubDaemon:
    """Minimal daemon surface that the context bus tools actually touch."""

    def __init__(self, store: ContextStore):
        self.context = store
        self.runners = {}
        self.all_cfgs = {}
        self.profile = type("P", (), {"agents": {}, "engagement_id": None})()
        self.skills = {}
        # bus_call_register / etc. aren't reached by the slicers — they're
        # for ask_agent. Leave them off; AttributeError signals "test is
        # exercising a tool it shouldn't be".


def _get_slicer(tool_name: str, daemon: _StubDaemon, owner: str = "test"):
    """Build a bus for `owner`, return the slicer tool function by name."""
    _, _, _ = bus_mod.make_bus(daemon, owner)
    # make_bus returns (server, server_name, wires). The tools were
    # registered via @tool decorators inside make_bus. To reach them we
    # need the server's internal tool registry — but the SDK's
    # create_sdk_mcp_server doesn't expose it cleanly. Easier path:
    # re-resolve through the inner `tools=[...]` list by capturing.
    # The straightforward route is to mock create_sdk_mcp_server and
    # capture its `tools` kwarg.
    raise NotImplementedError("see _build_and_capture")


def _build_and_capture(daemon: _StubDaemon, owner: str = "test") -> dict:
    """Build a bus and capture the tool callables by name."""
    captured: dict = {}

    def _fake_create(name, version, tools, **kw):
        for t in tools:
            # SDK wraps each @tool function in an SdkMcpTool whose
            # `.handler` attr holds the original async callable.
            handler = getattr(t, "handler", t)
            tool_name = getattr(t, "name", getattr(t, "__name__", str(t)))
            captured[tool_name] = handler
        return object()

    with mock.patch("salient_core.bus.create_sdk_mcp_server", _fake_create):
        bus_mod.make_bus(daemon, owner)
    return captured


class ContextSlicerBehaviourTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = ContextStore(db_path=None)
        # Realistic fixture: a 'findings' blob with a known shape so we
        # can assert on grep/section/lines/count/head/tail/summary.
        self.findings = "\n".join(
            [
                "# Findings — engagement test",
                "",
                "## Host 10.13.38.57",
                "- SSH open on port 22",
                "- HTTP open on port 80",
                "- Banner: OpenSSH_8.9p1",
                "",
                "## Host 10.13.38.58",
                "- SSH open on port 22",
                "- Banner: OpenSSH_8.9p1",
                "- Note: identical host key to .57",
                "",
                "## Host 10.13.38.59",
                "- HTTPS open on port 443",
                "- Tag: vendor-authorized",
                "",
                "## CVEs of interest",
                "- CVE-2024-1234",
                "- CVE-2024-5678",
            ]
        )
        self.store.write("scanner", "findings", self.findings)
        self.daemon = _StubDaemon(self.store)
        self.tools = _build_and_capture(self.daemon, owner="manager")

    # --- grep ---
    async def test_grep_returns_matching_lines_with_context(self):
        out = await self.tools["context_grep"](
            {
                "agent": "scanner",
                "key": "findings",
                "pattern": "OpenSSH",
                "before": 1,
                "after": 1,
                "max_matches": 10,
            }
        )
        text = out["content"][0]["text"]
        self.assertIn("OpenSSH_8.9p1", text)
        self.assertIn("matches=2", text)
        # Should include a line number prefix from grep output formatting.
        self.assertRegex(text, r"\s+\d+[:\-]")

    async def test_grep_invalid_regex_errors(self):
        out = await self.tools["context_grep"](
            {
                "agent": "scanner",
                "key": "findings",
                "pattern": "[unterminated",
            }
        )
        self.assertTrue(out.get("is_error"))
        self.assertIn("invalid regex", out["content"][0]["text"])

    async def test_grep_no_match_reports_so(self):
        out = await self.tools["context_grep"](
            {
                "agent": "scanner",
                "key": "findings",
                "pattern": "definitely-not-present-xyzzy",
            }
        )
        self.assertIn("no matches", out["content"][0]["text"])

    # --- section ---
    async def test_section_returns_context_around_anchor(self):
        out = await self.tools["context_section"](
            {
                "agent": "scanner",
                "key": "findings",
                "around": "10.13.38.58",
                "before": 2,
                "after": 3,
            }
        )
        text = out["content"][0]["text"]
        self.assertIn("10.13.38.58", text)
        self.assertIn("identical host key", text)  # in the after-window
        self.assertIn("matches=1", text)

    async def test_section_missing_anchor_errors(self):
        out = await self.tools["context_section"](
            {
                "agent": "scanner",
                "key": "findings",
            }
        )
        self.assertTrue(out.get("is_error"))

    # --- head / tail ---
    async def test_head_returns_first_n_lines(self):
        out = await self.tools["context_head"](
            {
                "agent": "scanner",
                "key": "findings",
                "n": 3,
            }
        )
        text = out["content"][0]["text"]
        self.assertIn("Findings — engagement test", text)
        # Should NOT contain lines from the bottom.
        self.assertNotIn("CVE-2024-5678", text)
        self.assertIn("showing 3/", text)

    async def test_tail_returns_last_n_lines(self):
        out = await self.tools["context_tail"](
            {
                "agent": "scanner",
                "key": "findings",
                "n": 3,
            }
        )
        text = out["content"][0]["text"]
        self.assertIn("CVE-2024-5678", text)
        self.assertNotIn("Findings — engagement test", text)

    # --- lines ---
    async def test_lines_returns_explicit_range(self):
        out = await self.tools["context_lines"](
            {
                "agent": "scanner",
                "key": "findings",
                "start": 3,
                "end": 5,
            }
        )
        text = out["content"][0]["text"]
        self.assertIn("10.13.38.57", text)  # line 3
        self.assertIn("port 80", text)  # line 5
        # Should NOT spill into adjacent ranges.
        self.assertNotIn("10.13.38.58", text)

    async def test_lines_past_eof_returns_clean_message(self):
        out = await self.tools["context_lines"](
            {
                "agent": "scanner",
                "key": "findings",
                "start": 9999,
                "end": 10000,
            }
        )
        self.assertIn("past end", out["content"][0]["text"])

    # --- count ---
    async def test_count_returns_match_stats_only(self):
        out = await self.tools["context_count"](
            {
                "agent": "scanner",
                "key": "findings",
                "pattern": r"CVE-\d{4}-\d+",
            }
        )
        text = out["content"][0]["text"]
        self.assertIn("lines_with_match=2", text)
        self.assertIn("total_match_occurrences=2", text)
        # Should NOT include actual CVE text — count is body-free.
        self.assertNotIn("2024-1234", text)

    # --- summary ---
    async def test_summary_returns_metadata_only(self):
        out = await self.tools["context_summary"](
            {
                "agent": "scanner",
                "key": "findings",
            }
        )
        text = out["content"][0]["text"]
        self.assertIn("chars=", text)
        self.assertIn("lines=", text)
        self.assertIn("first_line=", text)
        self.assertIn("last_line=", text)
        # Body NOT included.
        self.assertNotIn("OpenSSH_8.9p1", text)

    # --- cap on context_read ---
    async def test_context_read_oversize_returns_metadata_only(self):
        # Stash a big blob and force a small cap.
        big_lines = "\n".join(f"line-{i}: " + "x" * 100 for i in range(2000))
        self.store.write("scanner", "bigfindings", big_lines)
        full_len = len(big_lines)
        with mock.patch.dict(os.environ, {"SALIENT_CONTEXT_READ_CAP": "1000"}):
            # Rebuild bus so the cap helper reads the patched env at call time.
            tools = _build_and_capture(self.daemon, owner="manager")
            out = await tools["context_read"](
                {
                    "agent": "scanner",
                    "key": "bigfindings",
                }
            )
        text = out["content"][0]["text"]
        # Header + metadata + slicer pointer present.
        self.assertIn(f"{full_len} chars", text)
        self.assertIn("first line:", text)
        self.assertIn("last line:", text)
        self.assertIn("context_grep", text)
        self.assertIn("context_section", text)
        # CRITICAL: no body content from the middle of the value
        # leaks in. We include the first/last line as metadata
        # (those are intentional), but middle lines like line-1000
        # must never appear.
        self.assertNotIn("line-1000:", text)
        self.assertNotIn("line-500:", text)
        self.assertNotIn("line-1500:", text)

    async def test_context_read_under_cap_returns_whole_value(self):
        # The seeded findings (~400 chars) are way under the default cap.
        out = await self.tools["context_read"](
            {
                "agent": "scanner",
                "key": "findings",
            }
        )
        text = out["content"][0]["text"]
        self.assertNotIn("truncated", text)
        self.assertIn("OpenSSH_8.9p1", text)


class ContextReadMigrationTests(unittest.IsolatedAsyncioTestCase):
    """context_read migrated onto @bus_tool: the de-require fix + validation.

    Before the migration the SDK shorthand `{"agent": str, "key": str}` marked
    `key` REQUIRED, so a model omitting it hit a 'key is a required property'
    input-validation error. The Pydantic model defaults `key` to "latest",
    de-requiring it — that fix is pinned here (and in the golden's `required`).
    """

    def setUp(self):
        self.store = ContextStore(db_path=None)
        self.store.write("scanner", "latest", "most recent scanner reply")
        self.store.write("scanner", "findings", "the findings blob")
        self.tools = _build_and_capture(_StubDaemon(self.store), owner="manager")

    async def test_omitted_empty_and_explicit_key_all_resolve_to_latest(self):
        # Single-source-of-truth check: the field default covers OMITTED, the
        # field_validator maps an explicit "", and "latest" passes through — all
        # three reach the handler as "latest". This proves validator == the old
        # `or "latest"` coalesce, which is why the coalesce could be deleted.
        for args in (
            {"agent": "scanner"},
            {"agent": "scanner", "key": ""},
            {"agent": "scanner", "key": "latest"},
        ):
            out = await self.tools["context_read"](args)
            self.assertFalse(out.get("is_error"), args)
            text = out["content"][0]["text"]
            self.assertIn('key="latest"', text, args)
            self.assertIn("most recent scanner reply", text, args)

    async def test_explicit_key_still_read(self):
        out = await self.tools["context_read"]({"agent": "scanner", "key": "findings"})
        self.assertFalse(out.get("is_error"))
        self.assertIn("the findings blob", out["content"][0]["text"])

    async def test_missing_agent_is_friendly_validation_error(self):
        out = await self.tools["context_read"]({})
        self.assertTrue(out.get("is_error"))
        self.assertIn("agent", out["content"][0]["text"])

    async def test_undeclared_field_dropped_not_errored(self):
        # `additionalProperties: false` is advertised, but the wire path drops
        # undeclared/extra keys rather than 400ing the model mid-task.
        out = await self.tools["context_read"](
            {"agent": "scanner", "key": "latest", "bogus": "x", "_flag": True}
        )
        self.assertFalse(out.get("is_error"))
        self.assertIn("most recent scanner reply", out["content"][0]["text"])


class SlicerConstraintTests(unittest.IsolatedAsyncioTestCase):
    """The ge= constraints + single-source cleanup on the slicers: an explicit
    0 / out-of-range value is now HONORED or corrected, not silently overridden
    by the old `max()`/`or N` coalesce; the dynamic `end` default resolves via a
    None sentinel."""

    def setUp(self):
        self.store = ContextStore(db_path=None)
        self.store.write("scanner", "findings", "\n".join(f"line-{i}" for i in range(1, 21)))
        self.tools = _build_and_capture(_StubDaemon(self.store), owner="manager")

    async def test_grep_before_zero_is_honored_not_coalesced(self):
        # Pre-fix, `int(args.get("before") or 2)` turned an explicit 0 into 2.
        # Now before=0 means zero context lines — only the matching line shows.
        out = await self.tools["context_grep"](
            {"agent": "scanner", "key": "findings", "pattern": "line-5", "before": 0, "after": 0}
        )
        self.assertFalse(out.get("is_error"))
        text = out["content"][0]["text"]
        self.assertIn("line-5", text)
        self.assertNotIn("line-4", text)  # no BEFORE context leaked in
        self.assertNotIn("line-6", text)  # no AFTER context leaked in

    async def test_grep_out_of_range_rejected_not_clamped(self):
        # max_matches has ge=1; 0 is now a friendly validation error instead of
        # the old silent max(1, ...) clamp.
        out = await self.tools["context_grep"](
            {"agent": "scanner", "key": "findings", "pattern": "line", "max_matches": 0}
        )
        self.assertTrue(out.get("is_error"))
        self.assertIn("max_matches", out["content"][0]["text"])

    async def test_lines_end_defaults_to_start_via_none_sentinel(self):
        out = await self.tools["context_lines"]({"agent": "scanner", "key": "findings", "start": 7})
        self.assertFalse(out.get("is_error"))
        text = out["content"][0]["text"]
        self.assertIn("line-7", text)
        self.assertNotIn("line-8", text)  # single line when end omitted

    async def test_lines_explicit_range(self):
        out = await self.tools["context_lines"](
            {"agent": "scanner", "key": "findings", "start": 3, "end": 5}
        )
        text = out["content"][0]["text"]
        for i in (3, 4, 5):
            self.assertIn(f"line-{i}", text)
        self.assertNotIn("line-6", text)


if __name__ == "__main__":
    unittest.main()
