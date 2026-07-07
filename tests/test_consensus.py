"""ask_consensus bus tool — salient.bus._consensus.

Two layers:
  - Pure helpers (`_atoms`, `_agreement`) — plain assertions.
  - The handler tested in isolation by INJECTING a fake `ask_agent` (the tool
    factory takes it as a param) + a minimal fake daemon — so panel resolution,
    dispatch flags, synthesis, and judge modes are exercised without the real
    runner stack. There is no end-to-end suite yet; wiring against a real
    daemon/runner stack is exercised only manually (examples/consensus_panel).

Async tests use IsolatedAsyncioTestCase (the repo has no pytest-asyncio).
"""

from __future__ import annotations

import json
import unittest
from typing import Any
from unittest import mock

from salient_core.bus import _consensus as C

# ── pure helpers ─────────────────────────────────────────────────────────────


class AtomTests(unittest.TestCase):
    def test_atoms_extracts_by_kind(self):
        a = C._atoms(
            "host 10.0.0.5 and api.corp.local: ports 22/tcp, 443/tcp; "
            "CVE-2021-44228 hash 5f4dcc3b5aa765d61d8327deb882cf99"
        )
        self.assertIn("10.0.0.5", a["ip"])
        self.assertIn("api.corp.local", a["host"])
        self.assertEqual({"22", "443"}, a["port"])
        self.assertIn("CVE-2021-44228", a["cve"])
        self.assertIn("5f4dcc3b5aa765d61d8327deb882cf99", a["hash"])

    def test_atoms_ignores_bare_numbers(self):
        self.assertEqual(C._atoms("found 8080 things")["port"], set())


class AgreementTests(unittest.TestCase):
    def test_full_overlap_scores_one(self):
        score, corr, div = C._agreement({"a": "10.0.0.5 22/tcp", "b": "ports 22 on 10.0.0.5"})
        self.assertEqual(score, 1.0)
        self.assertIn("10.0.0.5", corr["ip"])
        self.assertIn("22", corr["port"])
        self.assertEqual(div, {})

    def test_partial_with_divergence(self):
        score, corr, div = C._agreement(
            {
                "a": "10.0.0.5 22/tcp 80/tcp",
                "b": "10.0.0.5 22/tcp 8080/tcp",
            }
        )
        # corroborated: 10.0.0.5, 22; divergent: 80(a), 8080(b) → 2/4.
        self.assertEqual(score, 0.5)
        self.assertIn("22", corr["port"])
        self.assertIn("10.0.0.5", corr["ip"])
        self.assertEqual(div["port"]["80"], "a")
        self.assertEqual(div["port"]["8080"], "b")

    def test_prose_fallback_nonzero(self):
        score, corr, div = C._agreement(
            {
                "a": "the service looks vulnerable to injection",
                "b": "the service looks vulnerable to injection attacks",
            }
        )
        self.assertTrue(0.0 < score <= 1.0)  # SequenceMatcher fallback
        self.assertEqual(corr, {})
        self.assertEqual(div, {})

    def test_disjoint_prose_low(self):
        score, _, _ = C._agreement(
            {"a": "completely different alpha", "b": "totally unrelated omega"}
        )
        self.assertLess(score, 0.6)


# ── handler in isolation (injected fake ask_agent + fake daemon) ─────────────


class _Runner:
    def __init__(self, status: str = "idle"):
        self.status = status


class _FakeContext:
    """Minimal event store: query_events(agent, since_ts, job_id) over a canned
    list. Mirrors the real signature — if the real store gains/renames params,
    the trace tests here must move in lockstep (trace errors are swallowed)."""

    def __init__(self, events: list[dict] | None = None):
        self._events = events or []

    def query_events(
        self, *, agent=None, kind=None, tool=None, since_ts=None, job_id=None, limit=50
    ):
        out = [
            e
            for e in self._events
            if (agent is None or e.get("agent") == agent)
            and (since_ts is None or e.get("ts", 0) >= since_ts)
            and (job_id is None or e.get("job_id") == job_id)
        ]
        return out[:limit]


class _FakeDaemon:
    def __init__(self):
        self.all_cfgs: dict = {}
        self.runners: dict = {}
        self._bus_calls: dict = {}
        self.profile: dict = {}
        self.context = None  # set to a _FakeContext to exercise trace capture


class _FakeAskAgent:
    """Stands in for the delegation `ask_agent` tool: records every dispatch and
    returns a canned `_text`-shaped reply per agent name.

    Mirrors the migrated ask_agent contract — consensus legs dispatch through
    `.trusted(args, *, flags)` (the routed in-process entry), and the job-id
    write-back rides on `flags.job_capture`, NOT an `_job_capture` key in args."""

    def __init__(
        self,
        replies: dict[str, tuple[bool, str]],
        job_ids: dict[str, int] | None = None,
    ):
        self.replies = replies
        self.job_ids = job_ids or {}  # agent name → child job id to "capture"
        self.calls: list[dict] = []
        self.flag_calls: list[Any] = []  # BusFlags passed per dispatch

    async def trusted(self, args: dict, *, flags: Any = None):
        self.calls.append(args)
        self.flag_calls.append(flags)
        # Mirror the real ask_agent's job_capture write-back on the flags sink.
        cap = getattr(flags, "job_capture", None)
        if isinstance(cap, dict) and args.get("name") in self.job_ids:
            cap["job_id"] = self.job_ids[args["name"]]
        ok, text = self.replies.get(args.get("name"), (False, f"no reply for {args.get('name')!r}"))
        out: dict = {"content": [{"type": "text", "text": text}]}
        if not ok:
            out["is_error"] = True
        return out


def _tool(daemon, owner, fake_aa):
    return C.make_consensus_tools(daemon, owner, fake_aa)[0]


def _payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


def _names(aa: _FakeAskAgent) -> list[str]:
    return [c["name"] for c in aa.calls]


def _pair_daemon() -> _FakeDaemon:
    """scanner (running) + deepseek_scanner (running shadow) + caller."""
    d = _FakeDaemon()
    d.all_cfgs = {
        "scanner": {},
        "deepseek_scanner": {"substitute_for": "scanner"},
        "caller": {},
    }
    d.runners = {
        "scanner": _Runner(),
        "deepseek_scanner": _Runner(),
        "caller": _Runner(),
    }
    return d


class ConsensusMigrationTests(unittest.IsolatedAsyncioTestCase):
    """New validation behaviors from the @bus_tool migration: the judge enum is
    now ENFORCED (was a lenient `.lower()` + fallback), and judge_agent's blank →
    'counsel' normalization moved into the model (single source of truth)."""

    async def test_judge_non_enum_value_is_rejected(self):
        # Pre-migration `.lower()` + `if not in: 'auto'` silently accepted junk;
        # the Literal now returns a friendly validation error naming the field.
        d = _pair_daemon()
        aa = _FakeAskAgent({"scanner": (True, "x"), "deepseek_scanner": (True, "y")})
        out = await _tool(d, "caller", aa).handler(
            {"name": "scanner", "prompt": "p", "judge": "SOMETIMES"}
        )
        self.assertTrue(out.get("is_error"))
        self.assertIn("judge", out["content"][0]["text"])

    def test_judge_agent_blank_normalizes_to_counsel(self):
        # The field_validator is the single source of the strip + fallback the
        # handler used to do inline.
        self.assertEqual(C._AskConsensusArgs(name="a", prompt="b").judge_agent, "counsel")
        self.assertEqual(
            C._AskConsensusArgs(name="a", prompt="b", judge_agent="   ").judge_agent, "counsel"
        )
        self.assertEqual(
            C._AskConsensusArgs(name="a", prompt="b", judge_agent=" gpt ").judge_agent, "gpt"
        )

    def test_agents_default_is_empty_list_not_none(self):
        # default_factory gives [] (behavior-identical to old None via resolve_panel's
        # `if explicit:`), and each instance gets its own list.
        self.assertEqual(C._AskConsensusArgs(name="a", prompt="b").agents, [])


class ConsensusHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_pair_resolution_and_dispatch_flags(self):
        d = _pair_daemon()
        aa = _FakeAskAgent(
            {
                "scanner": (True, "10.0.0.5 22/tcp 80/tcp"),
                "deepseek_scanner": (True, "10.0.0.5 22/tcp"),
            }
        )
        out = await _tool(d, "caller", aa).handler(
            {"name": "scanner", "prompt": "scan it", "judge": "off"}
        )
        p = _payload(out)
        self.assertEqual(set(p["panel"]), {"scanner", "deepseek_scanner"})
        self.assertEqual(len(p["per_agent"]), 2)
        self.assertEqual(len(aa.calls), 2)
        for call in aa.calls:
            self.assertIs(call["prefer_primary"], True)
            self.assertNotIn("_skip_redispatch_gate", call)  # moved to typed flags
        for f in aa.flag_calls:
            self.assertTrue(f.skip_redispatch_gate)
        self.assertEqual(p["corroborated"]["port"], ["22"])
        self.assertEqual(p["divergent"]["port"]["80"], "scanner")
        self.assertEqual(p["agreement_score"], round(2 / 3, 4))
        self.assertIsNone(p["judge"])

    async def test_resolves_pair_from_shadow_name(self):
        d = _pair_daemon()
        aa = _FakeAskAgent({"scanner": (True, "x"), "deepseek_scanner": (True, "x")})
        out = await _tool(d, "caller", aa).handler(
            {"name": "deepseek_scanner", "prompt": "p", "judge": "off"}
        )
        self.assertEqual(set(_payload(out)["panel"]), {"scanner", "deepseek_scanner"})

    async def test_caller_excluded_from_panel(self):
        d = _pair_daemon()
        aa = _FakeAskAgent({"scanner": (True, "x")})
        out = await _tool(d, "deepseek_scanner", aa).handler({"name": "scanner", "prompt": "p"})
        self.assertTrue(out.get("is_error"))
        self.assertIn("needs ≥2", out["content"][0]["text"])
        self.assertEqual(aa.calls, [])

    async def test_explicit_n_way_panel(self):
        d = _pair_daemon()
        d.all_cfgs["msf"] = {}
        d.runners["msf"] = _Runner()
        aa = _FakeAskAgent(
            {
                "scanner": (True, "10.0.0.5"),
                "deepseek_scanner": (True, "10.0.0.5"),
                "msf": (True, "10.0.0.5"),
            }
        )
        out = await _tool(d, "caller", aa).handler(
            {
                "name": "scanner",
                "prompt": "p",
                "judge": "off",
                "agents": ["scanner", "deepseek_scanner", "msf"],
            }
        )
        self.assertEqual(set(_payload(out)["panel"]), {"scanner", "deepseek_scanner", "msf"})
        self.assertEqual(len(aa.calls), 3)

    async def test_needs_two_live_agents(self):
        d = _pair_daemon()
        d.runners["deepseek_scanner"] = _Runner("stopped")
        aa = _FakeAskAgent({"scanner": (True, "x")})
        out = await _tool(d, "caller", aa).handler({"name": "scanner", "prompt": "p"})
        self.assertTrue(out.get("is_error"))
        self.assertIn("needs ≥2", out["content"][0]["text"])

    async def test_one_leg_error_still_returns(self):
        d = _pair_daemon()
        aa = _FakeAskAgent(
            {
                "scanner": (True, "10.0.0.5 22/tcp"),
                "deepseek_scanner": (False, "boom"),
            }
        )
        out = await _tool(d, "caller", aa).handler(
            {"name": "scanner", "prompt": "p", "judge": "off"}
        )
        p = _payload(out)
        self.assertFalse(p["ok"])  # <2 successful
        self.assertTrue(any(not r["ok"] for r in p["per_agent"]))
        self.assertIn("warnings", p)

    async def test_restrict_swarm_tools_refused(self):
        d = _pair_daemon()
        d.all_cfgs["caller"] = {"restrict_swarm_tools": True}
        aa = _FakeAskAgent({})
        out = await _tool(d, "caller", aa).handler({"name": "scanner", "prompt": "p"})
        self.assertTrue(out.get("is_error"))
        self.assertIn("restrict_swarm_tools", out["content"][0]["text"])
        self.assertEqual(aa.calls, [])


class SemanticAgreementTests(unittest.IsolatedAsyncioTestCase):
    class _FakeEmbedder:
        """Deterministic 2-D embeddings keyed by a substring, so cosine is
        predictable: texts sharing the tag point the same way."""

        def __init__(self, mapping: dict[str, list[float]]):
            self.mapping = mapping

        async def embed(self, texts):
            return [self.mapping.get(t) for t in texts]

    async def test_none_without_embedder(self):
        self.assertIsNone(await C.semantic_agreement({"a": "x", "b": "y"}, None))

    async def test_none_with_single_answer(self):
        emb = self._FakeEmbedder({"only": [1.0, 0.0]})
        self.assertIsNone(await C.semantic_agreement({"a": "only"}, emb))

    async def test_high_for_aligned_vectors(self):
        emb = self._FakeEmbedder({"a": [1.0, 0.0], "b": [1.0, 0.0]})
        score = await C.semantic_agreement({"x": "a", "y": "b"}, emb)
        self.assertAlmostEqual(score, 1.0, places=5)

    async def test_low_for_orthogonal_vectors(self):
        emb = self._FakeEmbedder({"a": [1.0, 0.0], "b": [0.0, 1.0]})
        score = await C.semantic_agreement({"x": "a", "y": "b"}, emb)
        self.assertAlmostEqual(score, 0.0, places=5)

    async def test_negative_cosine_clamped(self):
        emb = self._FakeEmbedder({"a": [1.0, 0.0], "b": [-1.0, 0.0]})
        score = await C.semantic_agreement({"x": "a", "y": "b"}, emb)
        self.assertEqual(score, 0.0)

    async def test_embed_failure_returns_none(self):
        emb = self._FakeEmbedder({"a": [1.0, 0.0]})  # "b" missing → None in batch
        self.assertIsNone(await C.semantic_agreement({"x": "a", "y": "b"}, emb))

    async def test_zero_vector_answer_excluded(self):
        # A garbage leg that embeds to all-zeros must be dropped, not averaged
        # in as cosine 0 (which would drag two aligned answers below threshold).
        emb = self._FakeEmbedder({"a": [1.0, 0.0], "b": [1.0, 0.0], "!!!": [0.0, 0.0]})
        score = await C.semantic_agreement({"x": "a", "y": "b", "z": "!!!"}, emb)
        self.assertAlmostEqual(score, 1.0, places=5)

    async def test_single_nonzero_vector_returns_none(self):
        emb = self._FakeEmbedder({"a": [1.0, 0.0], "!!!": [0.0, 0.0]})
        self.assertIsNone(await C.semantic_agreement({"x": "a", "z": "!!!"}, emb))


class TraceCaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_per_agent_trace_populated_from_context(self):
        d = _pair_daemon()
        d.context = _FakeContext(
            [
                {
                    "agent": "scanner",
                    "kind": "tool_call",
                    "tool": "probe",
                    "ts": 1e12,
                    "content": {"text": "ran probe"},
                },
                {
                    "agent": "scanner",
                    "kind": "user_message",
                    "ts": 1e12,
                    "content": {"text": "the prompt echo"},
                },
                {
                    "agent": "deepseek_scanner",
                    "kind": "thinking",
                    "ts": 1e12,
                    "content": {"text": "considering"},
                },
            ]
        )
        aa = _FakeAskAgent({"scanner": (True, "10.0.0.5"), "deepseek_scanner": (True, "10.0.0.5")})
        out = await _tool(d, "caller", aa).handler(
            {"name": "scanner", "prompt": "p", "judge": "off"}
        )
        p = _payload(out)
        by = {r["name"]: r for r in p["per_agent"]}
        kinds = [s["kind"] for s in by["scanner"]["trace"]]
        self.assertIn("tool_call", kinds)
        self.assertNotIn("user_message", kinds)  # prompt echo dropped
        self.assertEqual(by["deepseek_scanner"]["trace"][0]["kind"], "thinking")

    async def test_trace_isolated_by_job_id(self):
        # Two events for the same agent in the window — only the one belonging
        # to THIS dispatch's job id may appear in the leg trace.
        d = _pair_daemon()
        d.context = _FakeContext(
            [
                {
                    "agent": "scanner",
                    "kind": "tool_call",
                    "tool": "probe",
                    "ts": 1e12,
                    "job_id": 7,
                    "content": {"text": "our job"},
                },
                {
                    "agent": "scanner",
                    "kind": "tool_call",
                    "tool": "probe",
                    "ts": 1e12,
                    "job_id": 99,
                    "content": {"text": "concurrent unrelated job"},
                },
            ]
        )
        aa = _FakeAskAgent(
            {"scanner": (True, "10.0.0.5"), "deepseek_scanner": (True, "10.0.0.5")},
            job_ids={"scanner": 7},
        )
        out = await _tool(d, "caller", aa).handler(
            {"name": "scanner", "prompt": "p", "judge": "off"}
        )
        by = {r["name"]: r for r in _payload(out)["per_agent"]}
        texts = [s["text"] for s in by["scanner"]["trace"]]
        self.assertEqual(texts, ["our job"])
        # job_id must not leak into the wire payload.
        self.assertNotIn("job_id", by["scanner"])

    async def test_trace_empty_without_context(self):
        d = _pair_daemon()  # context stays None
        aa = _FakeAskAgent({"scanner": (True, "x"), "deepseek_scanner": (True, "x")})
        out = await _tool(d, "caller", aa).handler(
            {"name": "scanner", "prompt": "p", "judge": "off"}
        )
        for r in _payload(out)["per_agent"]:
            self.assertEqual(r["trace"], [])


class ConsensusSemanticHandlerTests(unittest.IsolatedAsyncioTestCase):
    """The semantic-score path THROUGH the handler — payload field + the
    semantic-driven judge decision. All other handler tests run with no
    embedder (profile={}), so this wiring is otherwise never exercised."""

    class _FakeEmbedder:
        def __init__(self, mapping: dict[str, list[float]]):
            self.mapping = mapping

        async def embed(self, texts):
            return [self.mapping.get(t) for t in texts]

    def _patch_embedder(self, mapping):
        # Patch the name _consensus imported, so the handler's
        # get_embedder(daemon.profile) resolves to our fake.
        fake = self._FakeEmbedder(mapping)
        patcher = mock.patch.object(C, "get_embedder", lambda profile: fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_semantic_score_present_and_rounded(self):
        d = _pair_daemon()
        self._patch_embedder({"same answer": [1.0, 0.0]})
        aa = _FakeAskAgent(
            {"scanner": (True, "same answer"), "deepseek_scanner": (True, "same answer")}
        )
        out = await _tool(d, "caller", aa).handler(
            {"name": "scanner", "prompt": "p", "judge": "off"}
        )
        self.assertEqual(_payload(out)["semantic_score"], 1.0)

    async def test_semantic_drives_want_judge(self):
        # Atom agreement is PERFECT (identical texts) but the embeddings are
        # orthogonal → semantic 0.0 < its threshold. judge=auto must fire off
        # the semantic trigger even though the atom score alone wouldn't judge.
        d = _pair_daemon()
        d.all_cfgs["counsel"] = {}
        d.runners["counsel"] = _Runner()
        self._patch_embedder(
            {"10.0.0.5 22/tcp open": [1.0, 0.0], "10.0.0.5 22/tcp filtered": [0.0, 1.0]}
        )
        aa = _FakeAskAgent(
            {
                "scanner": (True, "10.0.0.5 22/tcp open"),
                "deepseek_scanner": (True, "10.0.0.5 22/tcp filtered"),
                "counsel": (True, "JUDGED"),
            }
        )
        out = await _tool(d, "caller", aa).handler({"name": "scanner", "prompt": "p"})
        p = _payload(out)
        self.assertAlmostEqual(p["semantic_score"], 0.0)
        self.assertGreater(p["agreement_score"], 0.9)  # atoms agree...
        self.assertEqual(p["judge"], "JUDGED")  # ...yet the judge fired

    async def test_high_semantic_cannot_suppress_atom_judge(self):
        # Atoms diverge (score < threshold) while the embeddings are aligned
        # (semantic 1.0). The semantic trigger is additive only: the atom
        # divergence must still invoke the judge.
        d = _pair_daemon()
        d.all_cfgs["counsel"] = {}
        d.runners["counsel"] = _Runner()
        self._patch_embedder({"alpha unique": [1.0, 0.0], "omega distinct": [1.0, 0.0]})
        aa = _FakeAskAgent(
            {
                "scanner": (True, "alpha unique"),
                "deepseek_scanner": (True, "omega distinct"),
                "counsel": (True, "JUDGED"),
            }
        )
        out = await _tool(d, "caller", aa).handler({"name": "scanner", "prompt": "p"})
        p = _payload(out)
        self.assertEqual(p["semantic_score"], 1.0)  # prose reads aligned...
        self.assertLess(p["agreement_score"], 0.6)  # ...but atoms diverge
        self.assertEqual(p["judge"], "JUDGED")  # → judge still fires

    async def test_semantic_none_falls_back_to_atoms(self):
        # No embedder → semantic None → auto-judge decides off the atom score.
        d = _pair_daemon()
        d.all_cfgs["counsel"] = {}
        d.runners["counsel"] = _Runner()
        aa = _FakeAskAgent(
            {
                "scanner": (True, "alpha unique"),
                "deepseek_scanner": (True, "omega distinct"),
                "counsel": (True, "JUDGED"),
            }
        )
        out = await _tool(d, "caller", aa).handler({"name": "scanner", "prompt": "p"})
        p = _payload(out)
        self.assertIsNone(p["semantic_score"])
        self.assertEqual(p["judge"], "JUDGED")


class ConsensusJudgeTests(unittest.IsolatedAsyncioTestCase):
    def _judge_daemon(self) -> _FakeDaemon:
        d = _pair_daemon()
        d.all_cfgs["counsel"] = {}
        d.runners["counsel"] = _Runner()
        return d

    async def test_judge_off_never_calls_counsel(self):
        d = self._judge_daemon()
        aa = _FakeAskAgent(
            {"scanner": (True, "a"), "deepseek_scanner": (True, "z"), "counsel": (True, "JUDGED")}
        )
        out = await _tool(d, "caller", aa).handler(
            {"name": "scanner", "prompt": "p", "judge": "off"}
        )
        self.assertIsNone(_payload(out)["judge"])
        self.assertNotIn("counsel", _names(aa))

    async def test_judge_on_always_calls_counsel(self):
        d = self._judge_daemon()
        aa = _FakeAskAgent(
            {
                "scanner": (True, "10.0.0.5 22/tcp"),
                "deepseek_scanner": (True, "10.0.0.5 22/tcp"),
                "counsel": (True, "JUDGED"),
            }
        )
        out = await _tool(d, "caller", aa).handler(
            {"name": "scanner", "prompt": "p", "judge": "on"}
        )
        self.assertEqual(_payload(out)["judge"], "JUDGED")
        self.assertIn("counsel", _names(aa))

    async def test_judge_auto_fires_below_threshold(self):
        d = self._judge_daemon()
        aa = _FakeAskAgent(
            {
                "scanner": (True, "alpha unique"),
                "deepseek_scanner": (True, "omega distinct"),
                "counsel": (True, "JUDGED"),
            }
        )
        out = await _tool(d, "caller", aa).handler({"name": "scanner", "prompt": "p"})
        self.assertEqual(_payload(out)["judge"], "JUDGED")

    async def test_judge_auto_skipped_on_high_agreement(self):
        d = self._judge_daemon()
        aa = _FakeAskAgent(
            {
                "scanner": (True, "10.0.0.5 22/tcp"),
                "deepseek_scanner": (True, "10.0.0.5 22/tcp"),
                "counsel": (True, "JUDGED"),
            }
        )
        out = await _tool(d, "caller", aa).handler({"name": "scanner", "prompt": "p"})
        self.assertIsNone(_payload(out)["judge"])
        self.assertNotIn("counsel", _names(aa))

    async def test_judge_skipped_when_counsel_not_running(self):
        d = _pair_daemon()
        d.all_cfgs["counsel"] = {}  # configured but not running
        aa = _FakeAskAgent({"scanner": (True, "a"), "deepseek_scanner": (True, "z")})
        out = await _tool(d, "caller", aa).handler(
            {"name": "scanner", "prompt": "p", "judge": "on"}
        )
        p = _payload(out)
        self.assertIsNone(p["judge"])
        self.assertTrue(any("counsel" in w for w in p.get("warnings", [])))
        self.assertNotIn("counsel", _names(aa))

    async def test_judge_skipped_when_counsel_is_caller(self):
        d = self._judge_daemon()
        aa = _FakeAskAgent(
            {"scanner": (True, "a"), "deepseek_scanner": (True, "z"), "counsel": (True, "JUDGED")}
        )
        out = await _tool(d, "counsel", aa).handler(
            {"name": "scanner", "prompt": "p", "judge": "on"}
        )
        p = _payload(out)
        self.assertIsNone(p["judge"])
        # The warning must state the ACTUAL reason (caller-excluded), not the
        # generic "not running / unavailable" — the judge IS running here.
        self.assertTrue(any("caller" in w for w in p.get("warnings", [])))

    async def test_custom_judge_agent(self):
        d = _pair_daemon()
        d.all_cfgs["arbiter"] = {}
        d.runners["arbiter"] = _Runner()
        aa = _FakeAskAgent(
            {"scanner": (True, "a"), "deepseek_scanner": (True, "z"), "arbiter": (True, "ARB")}
        )
        out = await _tool(d, "caller", aa).handler(
            {"name": "scanner", "prompt": "p", "judge": "on", "judge_agent": "arbiter"}
        )
        self.assertEqual(_payload(out)["judge"], "ARB")
        self.assertIn("arbiter", _names(aa))
        self.assertNotIn("counsel", _names(aa))

    async def test_default_judge_still_counsel(self):
        d = self._judge_daemon()
        aa = _FakeAskAgent(
            {"scanner": (True, "a"), "deepseek_scanner": (True, "z"), "counsel": (True, "JUDGED")}
        )
        out = await _tool(d, "caller", aa).handler(
            {"name": "scanner", "prompt": "p", "judge": "on"}
        )
        self.assertEqual(_payload(out)["judge"], "JUDGED")


if __name__ == "__main__":
    unittest.main()
