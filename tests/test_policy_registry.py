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


if __name__ == "__main__":
    unittest.main()
