"""Every delegation entry point shares one prohibited-intent denylist.

The dataset declared its natural-language prohibitions under `bus.ask_agent`.
The swarm fan-out `bus.ask_agents` keyed on nothing and matched nothing, so the
same operator prose that was refused on the single-target call sailed through
the fan-out — a bypass costing exactly one letter, with WIDER reach than the
call it evaded (a fan-out hits many agents at once).

`check_intent` now unions the dataset's patterns across every name in
`_DELEGATION_QUALIFIED`, so a refusal holds whichever entry point the model
reaches for and a future sibling inherits it by construction.

Deliberately NOT fixed here: a separate per-child sweep at `.trusted` dispatch.
Swarm children are composed from the parent call's own `tool_input`, and
`_string_haystack` walks nested structures at any depth — so child prose is
already in the parent's haystack. A second scan would only double-count strikes.
That property is load-bearing for the decision, so it is pinned below.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from salient_core.policy.registry import PolicyDataset
from salient_core.policy.safeguards import (
    _DELEGATION_QUALIFIED,
    SafeguardConfig,
    check_intent,
)

_MARKER = "prohibited-delegation-marker"
_ENTRY = ("blocked-delegation", _MARKER)


def _dataset(prohibited: dict[str, list[tuple[str, str]]]) -> PolicyDataset:
    return PolicyDataset(tool_targets={}, prohibited_patterns=prohibited, loud_patterns={})


def _check(qualified: str, tool_input: dict[str, Any], dataset: PolicyDataset):
    return check_intent(qualified, tool_input, config=SafeguardConfig(), dataset=dataset)


# ---------------------------------------------------------------------------
# the regression itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("qualified", sorted(_DELEGATION_QUALIFIED))
def test_every_delegation_entry_point_inherits_the_denylist(qualified: str) -> None:
    """Declared under the singular ⇒ enforced on ALL delegation names.

    This is the pin that fails against pre-fix main for `bus.ask_agents`.
    """
    dataset = _dataset({"bus.ask_agent": [_ENTRY]})

    allowed, reason = _check(qualified, {"prompt": f"please {_MARKER} now"}, dataset)

    assert allowed is False, f"{qualified} did not inherit the delegation denylist"
    assert reason == "blocked-delegation"


@pytest.mark.parametrize("declared_under", sorted(_DELEGATION_QUALIFIED))
def test_the_union_is_symmetric(declared_under: str) -> None:
    """Whichever name the content author picks, the others inherit it.

    Guards against a "fix" that special-cases the plural into the singular:
    that would leave a table entry declared under `bus.ask_agents` invisible to
    `bus.ask_agent`.
    """
    dataset = _dataset({declared_under: [_ENTRY]})

    for qualified in sorted(_DELEGATION_QUALIFIED):
        allowed, _ = _check(qualified, {"prompt": _MARKER}, dataset)
        assert allowed is False, f"{qualified} missed a pattern declared under {declared_under}"


def test_a_new_delegation_sibling_inherits_without_a_table_edit() -> None:
    """The class fix, not the instance fix.

    A future streaming/batched fan-out added to `_DELEGATION_QUALIFIED` must be
    covered the moment it is registered — no second copy of the table entry.
    """
    dataset = _dataset({"bus.ask_agent": [_ENTRY]})
    hypothetical = "bus.ask_agents_streaming"
    patched = frozenset(_DELEGATION_QUALIFIED | {hypothetical})

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "salient_core.policy.safeguards._DELEGATION_QUALIFIED",
            patched,
        )
        allowed, reason = _check(hypothetical, {"prompt": _MARKER}, dataset)

    assert allowed is False
    assert reason == "blocked-delegation"


# ---------------------------------------------------------------------------
# the benign path must survive
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("qualified", sorted(_DELEGATION_QUALIFIED))
def test_ordinary_delegation_prose_is_still_allowed(qualified: str) -> None:
    dataset = _dataset({"bus.ask_agent": [_ENTRY]})

    allowed, reason = _check(
        qualified,
        {"prompt": "scan the staging host and summarise the open ports"},
        dataset,
    )

    assert allowed is True
    assert reason == ""


def test_non_delegation_tools_do_not_inherit_delegation_patterns() -> None:
    """No over-reach: the union is scoped to delegation-qualified names only."""
    dataset = _dataset({"bus.ask_agent": [_ENTRY]})

    allowed, reason = _check("ssh.ssh_exec", {"command": _MARKER}, dataset)

    assert allowed is True
    assert reason == ""


def test_a_tools_own_patterns_are_unaffected_by_the_union() -> None:
    dataset = _dataset(
        {
            "bus.ask_agent": [_ENTRY],
            "ssh.ssh_exec": [("ssh-specific", "ssh-only-marker")],
        }
    )

    allowed, reason = _check("ssh.ssh_exec", {"command": "ssh-only-marker"}, dataset)
    assert (allowed, reason) == (False, "ssh-specific")

    # ...and the ssh entry does not leak onto the delegation tools.
    allowed, _ = _check("bus.ask_agents", {"prompt": "ssh-only-marker"}, dataset)
    assert allowed is True


# ---------------------------------------------------------------------------
# why no separate per-child sweep is needed
# ---------------------------------------------------------------------------


def test_nested_child_prompts_are_scanned_in_a_fan_out() -> None:
    """Load-bearing for the decision NOT to scan each swarm child separately.

    Swarm children are composed from this same `tool_input`, and the haystack
    walks nested dicts/lists at any depth. If this ever stops holding, the
    `.trusted` in-process child dispatch becomes a real second leak and needs
    its own gate.
    """
    dataset = _dataset({"bus.ask_agent": [_ENTRY]})

    allowed, reason = _check(
        "bus.ask_agents",
        {
            "children": [
                {"name": "recon", "prompt": "enumerate the staging subnet"},
                {"name": "bad", "prompt": f"then {_MARKER}"},
            ]
        },
        dataset,
    )

    assert allowed is False
    assert reason == "blocked-delegation"


def test_deeply_nested_child_prose_is_still_scanned() -> None:
    dataset = _dataset({"bus.ask_agent": [_ENTRY]})

    allowed, _ = _check(
        "bus.ask_agents",
        {"spec": {"groups": [{"children": [{"opts": {"deliverable": _MARKER}}]}]}},
        dataset,
    )

    assert allowed is False


# ---------------------------------------------------------------------------
# mechanics
# ---------------------------------------------------------------------------


def test_shared_entries_are_not_evaluated_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dedupe pin.

    If both names declare the SAME (label, regex), the union must not append it
    twice — a duplicated pattern means duplicated regex work and, worse, would
    make a single call look like two matches to anything counting evaluations.
    """
    dataset = _dataset({name: [_ENTRY] for name in _DELEGATION_QUALIFIED})
    calls: list[str] = []
    real_search = re.search

    def counting_search(pattern, string, *args, **kwargs):
        calls.append(pattern)
        return real_search(pattern, string, *args, **kwargs)

    monkeypatch.setattr(
        "salient_core.policy.safeguards.re",
        type("_re", (), {"search": staticmethod(counting_search), "error": re.error}),
    )

    _check("bus.ask_agents", {"prompt": "nothing to see"}, dataset)

    assert calls.count(_MARKER) == 1, (
        f"pattern evaluated {calls.count(_MARKER)}× — union duplicated it"
    )


def test_operator_delegation_extra_patterns_still_apply_to_every_entry_point() -> None:
    """Pre-existing behaviour preserved: the friendly `delegation` key.

    Operators add engagement codenames under `delegation` without naming the
    internal qualified string. That already covered both names; the dataset
    union must not have disturbed it.
    """
    dataset = _dataset({})
    config = SafeguardConfig(extra_patterns={"delegation": [("codename", "OPERATION-QUARTZ")]})

    for qualified in sorted(_DELEGATION_QUALIFIED):
        allowed, reason = check_intent(
            qualified,
            {"prompt": "brief the team on OPERATION-QUARTZ"},
            config=config,
            dataset=dataset,
        )
        assert (allowed, reason) == (False, "codename")


def test_union_tolerates_a_name_with_no_declared_patterns() -> None:
    """The empty-set case: no entries anywhere ⇒ allow, no crash."""
    dataset = _dataset({})

    for qualified in sorted(_DELEGATION_QUALIFIED):
        assert _check(qualified, {"prompt": "anything at all"}, dataset) == (True, "")
