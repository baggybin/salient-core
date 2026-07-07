"""Pin _compute_ask_agent_timeout — the bus's caller-side wait window
sizing for ask_agent. Pre-fix the timeout was a flat
``target_runner.prompt_timeout + 60`` (~1260s default), which caused
swarm orchestrators doing 12-child fan-out at max_turns=50 to be
abandoned by the caller while still mid-fanout (observed 2026-05-21
on the kernel-reverse swarm).

Three signals contribute, MAX wins, hard-capped at 4h:
  (a) base = target runner's prompt_timeout + 60s slop
  (b) hint = max_turns_hint × 60 + 60 (per-call envelope budget)
  (c) swarm = composition-derived estimate when target is a swarm
      orchestrator (slowest child wall + orchestrator's own budget)

Each branch isolated so a regression points at the right knob.
"""

from __future__ import annotations

import unittest

from salient_core.bus import _compute_ask_agent_timeout


class _StubRunner:
    def __init__(self, prompt_timeout: float = 1200.0, cfg: dict | None = None):
        self.prompt_timeout = prompt_timeout
        self.cfg = cfg or {}


class _StubDaemon:
    def __init__(self, all_cfgs: dict | None = None, swarms: dict | None = None):
        self.all_cfgs = all_cfgs or {}
        self._swarms = swarms or {}


class BaseTimeoutTests(unittest.TestCase):
    """Signal (a): target runner's prompt_timeout + 60s slop. This is
    the pre-fix behaviour; must still apply when no other signal fires."""

    def test_base_uses_prompt_timeout_plus_slop(self):
        d = _StubDaemon(all_cfgs={"scanner": {"name": "scanner"}})
        r = _StubRunner(prompt_timeout=1200)
        timeout = _compute_ask_agent_timeout(
            daemon=d,
            target_name="scanner",
            target_runner=r,
            max_turns_hint=None,
        )
        self.assertEqual(timeout, 1260.0)

    def test_no_signal_returns_none(self):
        """asyncio.wait_for treats None as 'wait forever' — same shape
        as the pre-fix when runner.prompt_timeout was 0."""
        d = _StubDaemon(all_cfgs={"scanner": {"name": "scanner"}})
        r = _StubRunner(prompt_timeout=0)
        timeout = _compute_ask_agent_timeout(
            daemon=d,
            target_name="scanner",
            target_runner=r,
            max_turns_hint=None,
        )
        self.assertIsNone(timeout)


class HintScalingTests(unittest.TestCase):
    """Signal (b): max_turns_hint × 60 + 60. Caller-provided per-call
    envelope budget — extends the wait when the caller knows the work
    will be deep (50+ turn disasm lanes, msf staging, etc.)."""

    def test_hint_scales_per_turn(self):
        d = _StubDaemon(all_cfgs={"scanner": {"name": "scanner"}})
        r = _StubRunner(prompt_timeout=0)  # no base
        # max_turns=50 → 50*60 + 60 = 3060s
        timeout = _compute_ask_agent_timeout(
            daemon=d,
            target_name="scanner",
            target_runner=r,
            max_turns_hint=50,
        )
        self.assertEqual(timeout, 3060.0)

    def test_hint_wins_over_smaller_base(self):
        """max_turns hint = 50 (3060s) > default base (1260s). The
        operator passing a hint MUST extend the wait, not be shadowed
        by the default."""
        d = _StubDaemon(all_cfgs={"scanner": {"name": "scanner"}})
        r = _StubRunner(prompt_timeout=1200)
        timeout = _compute_ask_agent_timeout(
            daemon=d,
            target_name="scanner",
            target_runner=r,
            max_turns_hint=50,
        )
        self.assertEqual(timeout, 3060.0)

    def test_zero_hint_does_not_contribute(self):
        d = _StubDaemon(all_cfgs={"scanner": {"name": "scanner"}})
        r = _StubRunner(prompt_timeout=1200)
        timeout = _compute_ask_agent_timeout(
            daemon=d,
            target_name="scanner",
            target_runner=r,
            max_turns_hint=0,
        )
        self.assertEqual(timeout, 1260.0)

    def test_negative_hint_does_not_contribute(self):
        d = _StubDaemon(all_cfgs={"scanner": {"name": "scanner"}})
        r = _StubRunner(prompt_timeout=1200)
        timeout = _compute_ask_agent_timeout(
            daemon=d,
            target_name="scanner",
            target_runner=r,
            max_turns_hint=-5,
        )
        self.assertEqual(timeout, 1260.0)


class SwarmOrchestratorTimeoutTests(unittest.TestCase):
    """Signal (c): swarm composition-derived estimate.
    wall = max_floor*60 + max(20, len(members))*60 + 120 slop."""

    def test_swarm_orch_uses_composition_floor(self):
        """12-member mixed swarm. Sources are deepseek_playstation
        (no floor → default 30), deepseek_bash (no floor → default).
        max_floor falls back to 30 → child_wall = 1800, orch_budget =
        12*60 = 720, slop 120 → 2640s."""
        d = _StubDaemon(
            all_cfgs={
                "mixed-swarm": {
                    "name": "mixed-swarm",
                    "swarm_orchestrator": True,
                },
                "deepseek_playstation": {"name": "deepseek_playstation"},
                "deepseek_bash": {"name": "deepseek_bash"},
            },
            swarms={
                "mixed-swarm": {
                    "source": "mixed",
                    "members": [f"deepseek_playstation-{i}" for i in range(1, 9)]
                    + [f"deepseek_bash-{i}" for i in range(1, 3)]
                    + [f"deepseek_forensics-{i}" for i in range(1, 3)],
                    "composition": [
                        {
                            "source": "deepseek_playstation",
                            "members": [f"deepseek_playstation-{i}" for i in range(1, 9)],
                        },
                        {
                            "source": "deepseek_bash",
                            "members": [f"deepseek_bash-{i}" for i in range(1, 3)],
                        },
                        {
                            "source": "deepseek_forensics",
                            "members": [f"deepseek_forensics-{i}" for i in range(1, 3)],
                        },
                    ],
                }
            },
        )
        r = _StubRunner(prompt_timeout=1200)
        timeout = _compute_ask_agent_timeout(
            daemon=d,
            target_name="mixed-swarm",
            target_runner=r,
            max_turns_hint=None,
        )
        # max_floor=30 (default), 12 members → 30*60 + max(20,12)*60 + 120
        # = 1800 + 1200 + 120 = 3120
        self.assertEqual(timeout, 3120.0)

    def test_swarm_orch_honors_source_member_max_turns_floor(self):
        """When a source declares swarm_member_max_turns (e.g. pwn=40),
        the orchestrator estimate uses that — the slowest source dominates."""
        d = _StubDaemon(
            all_cfgs={
                "pwn-swarm": {
                    "name": "pwn-swarm",
                    "swarm_orchestrator": True,
                },
                "pwn": {"name": "pwn", "swarm_member_max_turns": 50},
                "bash": {"name": "bash"},  # no floor
            },
            swarms={
                "pwn-swarm": {
                    "source": "mixed",
                    "members": ["pwn-1", "pwn-2", "bash-1"],
                    "composition": [
                        {"source": "pwn", "members": ["pwn-1", "pwn-2"]},
                        {"source": "bash", "members": ["bash-1"]},
                    ],
                }
            },
        )
        r = _StubRunner(prompt_timeout=1200)
        timeout = _compute_ask_agent_timeout(
            daemon=d,
            target_name="pwn-swarm",
            target_runner=r,
            max_turns_hint=None,
        )
        # max_floor=50 (from pwn), members=3 → 50*60 + max(20,3)*60 + 120
        # = 3000 + 1200 + 120 = 4320
        self.assertEqual(timeout, 4320.0)

    def test_swarm_orch_wins_over_base_and_no_hint(self):
        """The operator-incident scenario: caller passes no hint, target
        is mixed-swarm. Base = 1260 (default). Swarm-derived must win."""
        d = _StubDaemon(
            all_cfgs={
                "mixed-swarm": {
                    "name": "mixed-swarm",
                    "swarm_orchestrator": True,
                },
                "deepseek_playstation": {"name": "deepseek_playstation"},
            },
            swarms={
                "mixed-swarm": {
                    "source": "mixed",
                    "members": ["deepseek_playstation-1"] * 12,  # 12 entries
                    "composition": [
                        {
                            "source": "deepseek_playstation",
                            "members": ["deepseek_playstation-1"] * 12,
                        },
                    ],
                }
            },
        )
        r = _StubRunner(prompt_timeout=1200)
        timeout = _compute_ask_agent_timeout(
            daemon=d,
            target_name="mixed-swarm",
            target_runner=r,
            max_turns_hint=None,
        )
        # Result > base 1260 — the actual incident fix.
        self.assertGreater(timeout, 1260.0)

    def test_non_swarm_target_does_not_get_swarm_estimate(self):
        """Regular leaf agents (no swarm_orchestrator flag) must not
        get bumped by accident — keeps timeouts tight for ordinary
        ask_agent calls."""
        d = _StubDaemon(
            all_cfgs={"scanner": {"name": "scanner"}},  # no swarm flag
            swarms={},
        )
        r = _StubRunner(prompt_timeout=1200)
        timeout = _compute_ask_agent_timeout(
            daemon=d,
            target_name="scanner",
            target_runner=r,
            max_turns_hint=None,
        )
        self.assertEqual(timeout, 1260.0)

    def test_swarm_orch_via_runner_cfg_when_not_in_all_cfgs(self):
        """Synthesized swarm orchestrators may not be in all_cfgs
        (they're runtime-spawned). The function falls back to
        target_runner.cfg for the swarm_orchestrator flag."""
        d = _StubDaemon(
            all_cfgs={"scanner": {"name": "scanner"}},  # mixed-swarm absent
            swarms={
                "mixed-swarm": {
                    "source": "mixed",
                    "members": ["x-1", "x-2"],
                    "composition": [
                        {"source": "scanner", "members": ["x-1", "x-2"]},
                    ],
                }
            },
        )
        r = _StubRunner(
            prompt_timeout=1200,
            cfg={"swarm_orchestrator": True},
        )
        timeout = _compute_ask_agent_timeout(
            daemon=d,
            target_name="mixed-swarm",
            target_runner=r,
            max_turns_hint=None,
        )
        # Should kick into the swarm path → > base 1260
        self.assertGreater(timeout, 1260.0)


class HardCapTests(unittest.TestCase):
    """4-hour ceiling so a runaway agent doesn't leak a coroutine
    forever. Beyond this the reaper takes over (flagged_stalled) or
    the operator bus_cancels."""

    def test_hard_cap_at_four_hours(self):
        d = _StubDaemon(all_cfgs={"scanner": {"name": "scanner"}})
        r = _StubRunner(prompt_timeout=0)
        # Hint = 1000 turns → 60060s, far beyond cap
        timeout = _compute_ask_agent_timeout(
            daemon=d,
            target_name="scanner",
            target_runner=r,
            max_turns_hint=1000,
        )
        self.assertEqual(timeout, 14400.0)


class MaxAcrossSignalsTests(unittest.TestCase):
    """The actual selection rule: MAX of the three signals."""

    def test_takes_max_when_all_signals_present(self):
        d = _StubDaemon(
            all_cfgs={
                "mixed-swarm": {
                    "name": "mixed-swarm",
                    "swarm_orchestrator": True,
                },
                "pwn": {"name": "pwn", "swarm_member_max_turns": 50},
            },
            swarms={
                "mixed-swarm": {
                    "source": "mixed",
                    "members": ["pwn-1", "pwn-2"],
                    "composition": [
                        {"source": "pwn", "members": ["pwn-1", "pwn-2"]},
                    ],
                }
            },
        )
        r = _StubRunner(prompt_timeout=1200)
        # base = 1260, hint = 100*60+60 = 6060, swarm = 50*60+20*60+120 = 4320
        # MAX = 6060
        timeout = _compute_ask_agent_timeout(
            daemon=d,
            target_name="mixed-swarm",
            target_runner=r,
            max_turns_hint=100,
        )
        self.assertEqual(timeout, 6060.0)


if __name__ == "__main__":
    unittest.main()
