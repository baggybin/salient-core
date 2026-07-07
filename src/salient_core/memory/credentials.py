"""Credential-vocabulary seam.

The kernel ships a GENERIC credential vocabulary (password / ssh_key / api_token)
so its secret-redaction, never-purge-compaction, and text-mode cred_record
fallback have something to run against standalone. A downstream skin that
defines domain-specific credential kinds registers them at startup via
``register_credential_vocab`` — the same call-time-registration idiom as the
policy / alias / bus seams — and the kernel's consumers read the merged set.

Single source of truth for three coupled facts that must not drift:
    kinds       — accepted ``cred_record.kind`` values (validation).
    predicates  — ``has_<x>`` KG predicates that carry a secret (never purged
                  by compaction; redacted from logs).
    kind→pred   — the mapping used when recording a credential fact.
"""

from __future__ import annotations

# Generic defaults — a kernel with no skin still has a working secret vocabulary.
_GENERIC: dict[str, str] = {
    "password": "has_password",
    "ssh_key": "has_ssh_key",
    "api_token": "has_api_token",
}

_kind_to_pred: dict[str, str] = dict(_GENERIC)


def register_credential_vocab(kind_to_predicate: dict[str, str]) -> None:
    """Register (merge in) a downstream skin's credential kinds and their KG
    predicates. Called once at startup. Extends the generic defaults."""
    _kind_to_pred.update(kind_to_predicate)


def cred_kinds() -> frozenset[str]:
    """The accepted ``cred_record.kind`` values (generic + registered)."""
    return frozenset(_kind_to_pred)


def cred_predicates() -> tuple[str, ...]:
    """The ``has_<x>`` predicates that carry a secret — never purged by
    compaction, redacted from logs. Sorted for a stable, comparable order."""
    return tuple(sorted(set(_kind_to_pred.values())))


def predicate_for_kind(kind: str) -> str:
    """The KG predicate for a credential kind; falls back to ``has_<kind>_hash``
    for a registered kind that didn't supply an explicit mapping."""
    return _kind_to_pred.get(kind, f"has_{kind}_hash")


def reset() -> None:
    """Restore the generic default vocabulary. Test-only."""
    _kind_to_pred.clear()
    _kind_to_pred.update(_GENERIC)
