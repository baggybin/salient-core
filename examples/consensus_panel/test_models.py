"""Model-catalog helper: live path when the SDK is configured, static fallback
otherwise. Uses a fake ``anthropic`` module so the live path is exercised
without a network or a credential.
"""

from __future__ import annotations

import datetime
import sys
import types
import unittest

import models as M


class _CacheIsolatedTest(unittest.TestCase):
    """list_models() memoizes for the process lifetime; each test starts cold."""

    def setUp(self):
        M._CACHE = None
        self.addCleanup(lambda: setattr(M, "_CACHE", None))


class FallbackTests(_CacheIsolatedTest):
    def test_fallback_is_nonempty_and_current(self):
        cat = M.fallback_catalog()
        ids = {c.id for c in cat}
        self.assertIn("claude-fable-5", ids)
        self.assertIn("claude-opus-4-8", ids)
        self.assertTrue(all(not c.live for c in cat))

    def test_list_models_degrades_without_sdk(self):
        # No `anthropic` module installed in this env → static fallback.
        self.assertEqual([c.id for c in M.list_models()], [c.id for c in M.fallback_catalog()])


class _FakeModel:
    def __init__(self, id, display_name, max_input_tokens, created_at=None):
        self.id = id
        self.display_name = display_name
        self.max_input_tokens = max_input_tokens
        if created_at is not None:
            self.created_at = created_at


class LiveTests(_CacheIsolatedTest):
    def _install_fake_anthropic(self, *, models, raises=None):
        mod = types.ModuleType("anthropic")
        list_calls = []

        class _Client:
            def __init__(self, *a, **k):
                if raises is not None:
                    raise raises

            class _Models:
                @staticmethod
                def list():
                    list_calls.append(1)
                    return iter(models)

            models = _Models()

        mod.Anthropic = _Client
        sys.modules["anthropic"] = mod
        self.addCleanup(lambda: sys.modules.pop("anthropic", None))
        return list_calls

    def test_live_path_maps_fields(self):
        self._install_fake_anthropic(
            models=[_FakeModel("claude-fable-5", "Claude Fable 5", 1_000_000)]
        )
        out = M.list_models()
        self.assertEqual(out[0].id, "claude-fable-5")
        self.assertEqual(out[0].context_tokens, 1_000_000)
        self.assertTrue(out[0].live)

    def test_live_error_falls_back(self):
        self._install_fake_anthropic(models=[], raises=RuntimeError("no creds"))
        self.assertEqual([c.id for c in M.list_models()], [c.id for c in M.fallback_catalog()])

    def test_empty_live_falls_back(self):
        self._install_fake_anthropic(models=[])
        self.assertEqual([c.id for c in M.list_models()], [c.id for c in M.fallback_catalog()])

    def test_live_order_newest_first_ties_by_id(self):
        newer = datetime.datetime(2026, 6, 1, tzinfo=datetime.UTC)
        older = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
        self._install_fake_anthropic(
            models=[
                _FakeModel("m-undated", "Undated", None),
                _FakeModel("m-old", "Old", None, created_at=older),
                _FakeModel("m-new-b", "New B", None, created_at=newer),
                _FakeModel("m-new-a", "New A", None, created_at=newer),
            ]
        )
        self.assertEqual(
            [c.id for c in M.list_models()], ["m-new-a", "m-new-b", "m-old", "m-undated"]
        )

    def test_non_datetime_created_at_degrades_not_raises(self):
        # A future/alternate SDK shape where created_at isn't a datetime must
        # fall back to the static catalog, honoring the never-raises contract.
        self._install_fake_anthropic(
            models=[_FakeModel("m-weird", "Weird", None, created_at="2026-06-01")]
        )
        self.assertEqual([c.id for c in M.list_models()], [c.id for c in M.fallback_catalog()])

    def test_catalog_is_fetched_once(self):
        calls = self._install_fake_anthropic(
            models=[_FakeModel("claude-fable-5", "Claude Fable 5", 1_000_000)]
        )
        first = M.list_models()
        second = M.list_models()
        self.assertEqual(len(calls), 1)
        self.assertEqual([c.id for c in first], [c.id for c in second])


if __name__ == "__main__":
    unittest.main()
