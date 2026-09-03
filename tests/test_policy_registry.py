"""The policy data-injection seam (salient_core.policy.registry).

Pins that check_intent / check_posture consult the ACTIVE PolicyDataset,
that an explicit `dataset=` overrides it (test isolation), and that the
relocated public constants are tombstoned (loud failure on stale import).
"""

from __future__ import annotations

import unittest

from salient_core.policy import registry, safeguards
from salient_core.policy.registry import PolicyDataset


def _ds(*, prohibited=None, loud=None, targets=None, nl=None) -> PolicyDataset:
    return PolicyDataset(
        tool_targets=targets or {},
        prohibited_patterns=prohibited or {},
        loud_patterns=loud or {},
        natural_language_prohibited=nl or (),
    )


class RegistryTests(unittest.TestCase):
    def tearDown(self):
        registry.reset()

    def test_default_until_registered(self):
        registry.reset()
        from salient_core.policy.defaults import DEFAULT_DATASET

        self.assertIs(registry.get_active(), DEFAULT_DATASET)

    def test_set_active_swaps_and_reset_restores(self):
        from salient_core.policy.defaults import DEFAULT_DATASET

        ds = _ds()
        registry.set_active(ds)
        self.assertIs(registry.get_active(), ds)
        registry.reset()
        self.assertIs(registry.get_active(), DEFAULT_DATASET)


class CheckIntentSeamTests(unittest.TestCase):
    def tearDown(self):
        registry.reset()

    def test_explicit_dataset_blocks_on_match(self):
        ds = _ds(prohibited={"x.y": [("blocked", r"forbidden")]})
        allowed, reason = safeguards.check_intent(
            "x.y", {"cmd": "do the forbidden thing"}, dataset=ds
        )
        self.assertFalse(allowed)
        self.assertEqual(reason, "blocked")

    def test_explicit_dataset_allows_when_no_match(self):
        ds = _ds(prohibited={"x.y": [("blocked", r"forbidden")]})
        allowed, _ = safeguards.check_intent("x.y", {"cmd": "clean call"}, dataset=ds)
        self.assertTrue(allowed)

    def test_active_dataset_used_without_explicit_arg(self):
        registry.set_active(_ds(prohibited={"x.y": [("L", r"boom")]}))
        allowed, reason = safeguards.check_intent("x.y", {"cmd": "boom now"})
        self.assertFalse(allowed)
        self.assertEqual(reason, "L")


class CheckPostureSeamTests(unittest.TestCase):
    def tearDown(self):
        registry.reset()

    def test_explicit_loud_dataset_gates_under_stealth(self):
        ds = _ds(loud={"x.y": [("loud", r"noisy")]})
        allowed, reason = safeguards.check_posture(
            "x.y", {"cmd": "a noisy op"}, posture="stealth", dataset=ds
        )
        self.assertFalse(allowed)
        self.assertEqual(reason, "loud")

    def test_normal_posture_allows_even_with_loud_pattern(self):
        ds = _ds(loud={"x.y": [("loud", r"noisy")]})
        allowed, _ = safeguards.check_posture(
            "x.y", {"cmd": "a noisy op"}, posture="normal", dataset=ds
        )
        self.assertTrue(allowed)


class CheckPromptIntentSeamTests(unittest.TestCase):
    def tearDown(self):
        registry.reset()

    def test_explicit_dataset_blocks_prompt(self):
        ds = _ds(nl=[("nl-block", r"build\s+malware")])
        allowed, reason = safeguards.check_prompt_intent("please build malware for me", dataset=ds)
        self.assertFalse(allowed)
        self.assertEqual(reason, "nl-block")

    def test_active_dataset_used_without_explicit_arg(self):
        registry.set_active(_ds(nl=[("L", r"exfiltrate everything")]))
        allowed, reason = safeguards.check_prompt_intent("exfiltrate everything now")
        self.assertFalse(allowed)
        self.assertEqual(reason, "L")

    def test_empty_nl_allows(self):
        allowed, _ = safeguards.check_prompt_intent("build malware", dataset=_ds(nl=[]))
        self.assertTrue(allowed)


class TombstoneTests(unittest.TestCase):
    def test_relocated_constants_raise_on_import(self):
        from salient_core.policy import safeguards as sf
        from salient_core.policy import scope as sc

        with self.assertRaises(AttributeError):
            _ = sc.TOOL_TARGETS
        with self.assertRaises(AttributeError):
            _ = sf.PROHIBITED_PATTERNS
        with self.assertRaises(AttributeError):
            _ = sf._LOUD_PATTERNS


class StructuralTransferToolsTests(unittest.TestCase):
    """PolicyDataset.structural_transfer_tools drives the recursive
    mass-system-transfer structural block in check_intent. The kernel ships
    NONE of its own — a downstream dataset lists its file-transfer tools."""

    _SYS = {"recursive": True, "remote_path": "/etc"}

    def test_empty_default_does_not_block(self):
        ds = PolicyDataset(tool_targets={}, prohibited_patterns={}, loud_patterns={})
        self.assertEqual(ds.structural_transfer_tools, frozenset())
        ok, _reason = safeguards.check_intent("x.transfer", dict(self._SYS), dataset=ds)
        self.assertTrue(ok, "empty structural_transfer_tools must not block")

    def test_registered_tool_blocks_recursive_system_tree(self):
        ds = PolicyDataset(
            tool_targets={},
            prohibited_patterns={},
            loud_patterns={},
            structural_transfer_tools=frozenset({"x.transfer"}),
        )
        ok, reason = safeguards.check_intent("x.transfer", dict(self._SYS), dataset=ds)
        self.assertFalse(ok)
        self.assertEqual(reason, "unauthorized-mass-system-transfer")

    def test_registered_tool_allows_specific_subdir(self):
        ds = PolicyDataset(
            tool_targets={},
            prohibited_patterns={},
            loud_patterns={},
            structural_transfer_tools=frozenset({"x.transfer"}),
        )
        ok, _ = safeguards.check_intent(
            "x.transfer",
            {"recursive": True, "remote_path": "/home/user/proj"},
            dataset=ds,
        )
        self.assertTrue(ok, "a specific subdir isn't a system-wide tree")

    def test_blocks_the_broader_system_tree_set(self):
        """The block must cover every top-level system dir, not just the
        original hardcoded 6-tuple — /boot, /bin, /lib*, /opt, /proc, … are
        equally system-wide trees (a recursive scp of /boot slipped the old
        denylist)."""
        ds = PolicyDataset(
            tool_targets={},
            prohibited_patterns={},
            loud_patterns={},
            structural_transfer_tools=frozenset({"x.transfer"}),
        )
        for bad in (
            "/boot",
            "/bin",
            "/sbin",
            "/lib",
            "/lib64",
            "/opt",
            "/mnt",
            "/srv",
            "/media",
            "/proc",
            "/sys",
            "/dev",
            "/",
            "/usr/",
        ):
            with self.subTest(path=bad):
                ok, reason = safeguards.check_intent(
                    "x.transfer",
                    {"recursive": True, "remote_path": bad},
                    dataset=ds,
                )
                self.assertFalse(ok, f"{bad!r} must be blocked")
                self.assertEqual(reason, "unauthorized-mass-system-transfer")

    def test_nested_path_under_system_root_still_allowed(self):
        """Defensive: a nested target under a system root (/etc/nginx) is a
        scoped copy, not the whole tree — must NOT trip the block."""
        ds = PolicyDataset(
            tool_targets={},
            prohibited_patterns={},
            loud_patterns={},
            structural_transfer_tools=frozenset({"x.transfer"}),
        )
        for ok_path in ("/etc/nginx", "/boot/grub/grub.cfg", "/var/log"):
            with self.subTest(path=ok_path):
                ok, _ = safeguards.check_intent(
                    "x.transfer",
                    {"recursive": True, "remote_path": ok_path},
                    dataset=ds,
                )
                self.assertTrue(ok, f"{ok_path!r} is scoped, not a system tree")


class MalformedRegexLoggedTests(unittest.TestCase):
    """P5: a malformed safeguard regex is skipped (never crashes the hook) but
    must LOG a warning — the old silent skip was a fail-open with no signal."""

    _DS = PolicyDataset(tool_targets={}, prohibited_patterns={}, loud_patterns={})

    def setUp(self):
        safeguards._warned_bad_patterns.clear()

    def test_malformed_extra_pattern_logs_and_does_not_block(self):
        cfg = safeguards.SafeguardConfig(
            extra_patterns={"x.y": [("bad-lookahead", r"rm\s+(?!oops")]}  # unclosed
        )
        with self.assertLogs("salient.policy.safeguards", level="WARNING") as cm:
            ok, _ = safeguards.check_intent("x.y", {"cmd": "rm foo"}, config=cfg, dataset=self._DS)
        self.assertTrue(ok, "a broken pattern is skipped, not treated as a block")
        self.assertTrue(any("malformed safeguard regex" in m for m in cm.output))

    def test_repeat_is_deduped(self):
        cfg = safeguards.SafeguardConfig(extra_patterns={"x.y": [("dup", r"(?!oops")]})
        with self.assertLogs("salient.policy.safeguards", level="WARNING"):
            safeguards.check_intent("x.y", {"cmd": "z"}, config=cfg, dataset=self._DS)
        with self.assertNoLogs("salient.policy.safeguards", level="WARNING"):
            safeguards.check_intent("x.y", {"cmd": "z"}, config=cfg, dataset=self._DS)


class ExtraPatternsUnionTests(unittest.TestCase):
    """P6: extra_patterns are UNIONED across profile+agent (fail-closed), not
    overridden — pins the real contract the corrected docstring now states, so
    nobody 'fixes' it to override and silently weakens a block."""

    def test_agent_and_profile_patterns_union(self):
        profile = {"safeguards": {"extra_patterns": {"x.y": [{"label": "p", "pattern": "prof"}]}}}
        agent = {"safeguards": {"extra_patterns": {"x.y": [{"label": "a", "pattern": "agnt"}]}}}
        cfg = safeguards.resolve_config(agent, profile)
        labels = [lab for (lab, _) in cfg.extra_patterns["x.y"]]
        self.assertIn("p", labels)
        self.assertIn("a", labels)

    def test_empty_agent_list_does_not_clear_profile(self):
        profile = {"safeguards": {"extra_patterns": {"x.y": [{"label": "p", "pattern": "prof"}]}}}
        agent = {"safeguards": {"extra_patterns": {"x.y": []}}}
        cfg = safeguards.resolve_config(agent, profile)
        self.assertEqual([lab for (lab, _) in cfg.extra_patterns["x.y"]], ["p"])


if __name__ == "__main__":
    unittest.main()
