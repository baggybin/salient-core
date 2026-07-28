"""`_pick_substitute` — substitute-routing resolver.

Pins the union-of(all_cfgs, runners) behavior so a RUNTIME-materialized shadow
(doppelganger / the per-primary shadow selector), whose cfg lives only on
`runner.cfg` and never in `all_cfgs`, is still enumerated and routed.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import salient_core.bus._delegation as deleg
from salient_core.bus._delegation import _pick_substitute


@pytest.fixture(autouse=True)
def _reset_disabled_checker():
    saved = deleg._agent_disabled_checker
    deleg._agent_disabled_checker = None
    yield
    deleg._agent_disabled_checker = saved


def _runner(cfg, status="running"):
    return SimpleNamespace(cfg=cfg, status=status)


def test_static_shadow_in_all_cfgs_routes():
    all_cfgs = {
        "scanner": {"name": "scanner"},
        "ds_scanner": {"name": "ds_scanner", "substitute_for": "scanner"},
    }
    runners = {"ds_scanner": _runner(all_cfgs["ds_scanner"])}
    assert _pick_substitute(all_cfgs, runners, "scanner") == "ds_scanner"


def test_runners_only_materialized_shadow_routes():
    # sh_scanner is materialized at runtime: present in `runners`, NOT `all_cfgs`.
    all_cfgs = {"scanner": {"name": "scanner"}}
    runners = {"sh_scanner": _runner({"name": "sh_scanner", "substitute_for": "scanner"})}
    assert _pick_substitute(all_cfgs, runners, "scanner") == "sh_scanner"


def test_stopped_runners_only_shadow_not_picked():
    all_cfgs = {"scanner": {"name": "scanner"}}
    runners = {
        "sh_scanner": _runner({"name": "sh_scanner", "substitute_for": "scanner"}, "stopped")
    }
    assert _pick_substitute(all_cfgs, runners, "scanner") is None


def test_no_substitute_returns_none():
    assert _pick_substitute({"scanner": {"name": "scanner"}}, {}, "scanner") is None


def test_name_stable_lowest_named_wins():
    all_cfgs = {"scanner": {"name": "scanner"}}
    runners = {
        "z_scanner": _runner({"name": "z_scanner", "substitute_for": "scanner"}),
        "a_scanner": _runner({"name": "a_scanner", "substitute_for": "scanner"}),
    }
    assert _pick_substitute(all_cfgs, runners, "scanner") == "a_scanner"


def test_disabled_checker_skips_candidate():
    all_cfgs = {"scanner": {"name": "scanner"}}
    runners = {"sh_scanner": _runner({"name": "sh_scanner", "substitute_for": "scanner"})}
    deleg._agent_disabled_checker = lambda _profile, name: name == "sh_scanner"
    assert _pick_substitute(all_cfgs, runners, "scanner") is None
