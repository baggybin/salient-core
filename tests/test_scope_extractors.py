"""Characterization tests for the scope-gate target extractor.

`scope.extract_targets` is a security boundary (it decides what a tool is
allowed to talk to) with thin coverage. These tests pin its CURRENT behavior
byte-for-byte BEFORE the extractor-kind registry refactor and the extraction of
domain-specific kinds into a downstream skin, so the mechanical move can be
proven behavior-preserving.

Two groups:
  * GenericExtractorTests    — extractor kinds that STAY in the public kernel.
  * SecuritySkinExtractorTests — kinds that MOVE to the downstream security skin
    (msf_cmd / msf_module / session_command / wifi_*). These assertions travel
    with the kinds to salient-security when they relocate; kept here now so the
    disentangling of msf_cmd from the shared raw_argv branch is guarded.
  * UnknownKindTests         — the registry-miss contract (exact error message)
    that the registry refactor must reproduce for an unregistered kind.
"""

from __future__ import annotations

import unittest

from salient_core.policy import scope as _scope
from salient_core.policy.scope import (
    ExtractorError,
    ExtractorSpec,
    Target,
    extract_targets,
    register_extractor,
    unregister_all_extractors,
)


def _kv(targets):
    return [(t.kind, t.value) for t in targets]


class GenericExtractorTests(unittest.TestCase):
    def test_ip_or_host_ip(self):
        ts = extract_targets(ExtractorSpec(fields={"target": "ip_or_host"}), {"target": "10.0.0.5"})
        self.assertEqual(_kv(ts), [("ip", "10.0.0.5")])

    def test_ip_or_host_host(self):
        ts = extract_targets(
            ExtractorSpec(fields={"target": "ip_or_host"}), {"target": "example.com"}
        )
        self.assertEqual(_kv(ts), [("host", "example.com")])

    def test_ip_or_host_cidr_is_network(self):
        ts = extract_targets(
            ExtractorSpec(fields={"target": "ip_or_host"}), {"target": "10.0.0.0/24"}
        )
        self.assertEqual(_kv(ts), [("network", "10.0.0.0/24")])

    def test_ip_or_host_range_expands(self):
        ts = extract_targets(
            ExtractorSpec(fields={"target": "ip_or_host"}), {"target": "10.0.0.5-10"}
        )
        self.assertEqual(_kv(ts), [("ip", f"10.0.0.{n}") for n in range(5, 11)])

    def test_host_rejects_ip(self):
        with self.assertRaises(ExtractorError):
            extract_targets(ExtractorSpec(fields={"target": "host"}), {"target": "10.0.0.5"})

    def test_url_ok_reduces_to_host(self):
        ts = extract_targets(ExtractorSpec(fields={"u": "url"}), {"u": "https://example.com/x"})
        self.assertEqual(_kv(ts), [("host", "example.com")])

    def test_url_rejects_non_url(self):
        with self.assertRaises(ExtractorError):
            extract_targets(ExtractorSpec(fields={"u": "url"}), {"u": "example.com"})

    def test_url_or_host_accepts_both(self):
        self.assertEqual(
            _kv(extract_targets(ExtractorSpec(fields={"x": "url_or_host"}), {"x": "example.com"})),
            [("host", "example.com")],
        )
        self.assertEqual(
            _kv(
                extract_targets(
                    ExtractorSpec(fields={"x": "url_or_host"}), {"x": "https://a.com/p"}
                )
            ),
            [("host", "a.com")],
        )

    def test_endpoint_host_port(self):
        ts = extract_targets(ExtractorSpec(fields={"x": "endpoint"}), {"x": "10.0.0.5:8080"})
        self.assertEqual(_kv(ts), [("ip", "10.0.0.5")])

    def test_cidr_list(self):
        ts = extract_targets(
            ExtractorSpec(fields={"c": "cidr_list"}), {"c": "10.0.0.0/30, 10.0.1.0/30"}
        )
        self.assertEqual(_kv(ts), [("network", "10.0.0.0/30"), ("network", "10.0.1.0/30")])

    def test_binary_argv_synthesizes_command(self):
        ts = extract_targets(
            ExtractorSpec(fields={"binary": "binary_argv"}),
            {"binary": "curl", "args": ["https://a.com"]},
        )
        self.assertEqual(_kv(ts), [("host", "a.com")])

    def test_raw_argv_extracts_ip(self):
        ts = extract_targets(ExtractorSpec(fields={"cmd": "raw_argv"}), {"cmd": "curl 10.0.0.5"})
        self.assertEqual(_kv(ts), [("ip", "10.0.0.5")])

    def test_raw_argv_multiline(self):
        ts = extract_targets(
            ExtractorSpec(fields={"cmd": "raw_argv"}), {"cmd": "curl https://a.com\nping 10.0.0.9"}
        )
        self.assertEqual(_kv(ts), [("host", "a.com"), ("ip", "10.0.0.9")])

    def test_raw_argv_refuses_obfuscation(self):
        with self.assertRaises(ExtractorError) as cm:
            extract_targets(
                ExtractorSpec(fields={"cmd": "raw_argv"}), {"cmd": "curl $(echo 10.0.0.5)"}
            )
        self.assertIn("refused", str(cm.exception))

    def test_raw_argv_no_target_allows(self):
        self.assertEqual(
            extract_targets(ExtractorSpec(fields={"cmd": "raw_argv"}), {"cmd": "ls /tmp"}), []
        )

    def test_optional_kinds_empty_returns_empty(self):
        self.assertEqual(extract_targets(ExtractorSpec(fields={"x": "ip_optional"}), {"x": ""}), [])
        self.assertEqual(extract_targets(ExtractorSpec(fields={"x": "host_optional"}), {}), [])

    def test_none_spec_returns_empty(self):
        self.assertEqual(extract_targets(ExtractorSpec(none=True), {"x": "10.0.0.5"}), [])

    def test_local_only_spec_returns_empty(self):
        self.assertEqual(extract_targets(ExtractorSpec(local_only=True), {"x": "10.0.0.5"}), [])

    def test_at_least_one_raises_when_none_present(self):
        with self.assertRaises(ExtractorError):
            extract_targets(
                ExtractorSpec(fields={"a": "ip_optional", "b": "host_optional"}, at_least_one=True),
                {},
            )

    def test_at_least_one_passes_when_one_present(self):
        ts = extract_targets(
            ExtractorSpec(fields={"a": "ip_optional", "b": "host_optional"}, at_least_one=True),
            {"b": "x.com"},
        )
        self.assertEqual(_kv(ts), [("host", "x.com")])


# NOTE: characterization tests for the offensive extractor kinds
# (msf_cmd / msf_module / session_command / wifi_*) live in the salient-security
# package (tests/test_scope_extractors.py), which owns those kinds. They are
# verified there — before and after the kinds relocate — so the behavior is
# pinned in the package responsible for it.


class UnknownKindTests(unittest.TestCase):
    """The registry-miss contract: an unregistered kind raises ExtractorError
    with this exact message. The extractor-kind registry refactor MUST
    reproduce it for a kind no skin has registered."""

    def test_unknown_kind_raises_exact_message(self):
        with self.assertRaises(ExtractorError) as cm:
            extract_targets(ExtractorSpec(fields={"z": "bogus_kind"}), {"z": "x"})
        self.assertEqual(str(cm.exception), "unknown extractor kind: 'bogus_kind'")


class ExtractorRegistryTests(unittest.TestCase):
    """The seam: a downstream skin registers domain-specific extractor kinds;
    generic kinds are reserved; a registry miss reproduces the historical
    unknown-kind error."""

    def tearDown(self):
        unregister_all_extractors()

    def test_registered_kind_is_dispatched(self):
        def _skin_extractor(ctx):
            return [Target(kind="host", value=str(ctx.raw).upper(), source_field=ctx.field)]

        register_extractor("skin_thing", _skin_extractor)
        ts = extract_targets(ExtractorSpec(fields={"x": "skin_thing"}), {"x": "abc"})
        self.assertEqual(_kv(ts), [("host", "ABC")])

    def test_registered_extractor_can_read_sibling_args(self):
        def _skin_extractor(ctx):
            return [Target(kind="ip", value=ctx.args["sibling"], source_field=ctx.field)]

        register_extractor("skin_sibling", _skin_extractor)
        ts = extract_targets(
            ExtractorSpec(fields={"x": "skin_sibling"}), {"x": "present", "sibling": "10.0.0.9"}
        )
        self.assertEqual(_kv(ts), [("ip", "10.0.0.9")])

    def test_cannot_register_reserved_core_kind(self):
        with self.assertRaises(ExtractorError) as cm:
            register_extractor("raw_argv", lambda ctx: [])
        self.assertIn("reserved core extractor kind", str(cm.exception))

    def test_duplicate_registration_rejected(self):
        register_extractor("dup", lambda ctx: [])
        with self.assertRaises(ExtractorError):
            register_extractor("dup", lambda ctx: [])

    def test_override_allows_replacement(self):
        register_extractor("ov", lambda ctx: [])
        register_extractor(
            "ov",
            lambda ctx: [Target(kind="host", value="new", source_field=ctx.field)],
            override=True,
        )
        ts = extract_targets(ExtractorSpec(fields={"x": "ov"}), {"x": "y"})
        self.assertEqual(_kv(ts), [("host", "new")])

    def test_reserved_kinds_include_generic_kinds(self):
        # Sanity: every generic kind the kernel handles inline is reserved.
        for k in ("raw_argv", "ip_or_host", "cidr_list", "url", "none"):
            self.assertIn(k, _scope._CORE_KINDS)


if __name__ == "__main__":
    unittest.main()
