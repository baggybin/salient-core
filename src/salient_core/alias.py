"""No-op alias implementation — the kernel default.

A downstream alias skin can provide a real name-mapping layer that renames
tools and rewrites text on the wire. The kernel ships IdentityAlias, which
passes everything through unchanged.

Bus modules import module-level functions (``from ..alias import to_real``)
which delegate to a module-level singleton. An alias skin replaces this
singleton at startup via ``set_active``. For Protocol-injection consumers,
``IdentityAlias()`` is also available as a class.
"""

from __future__ import annotations

# AliasProtocol has one canonical home (protocols.py); re-exported here so
# `from salient_core.alias import AliasProtocol` keeps working.
from .protocols import AliasProtocol

__all__ = [
    "AliasProtocol",
    "IdentityAlias",
    "enabled",
    "mapping",
    "rewrite_inbound",
    "rewrite_outbound",
    "set_active",
    "to_real",
    "to_wire",
]


class IdentityAlias:
    """Passthrough aliaser — no renaming, no rewriting."""

    __slots__ = ()

    def to_wire(self, name: str) -> str:
        return name

    def to_real(self, name: str) -> str:
        return name

    def rewrite_outbound(self, text: str) -> str:
        return text

    def rewrite_inbound(self, text: str) -> str:
        return text

    def mapping(self) -> dict[str, str]:
        return {}

    def enabled(self) -> bool:
        return False


_active: AliasProtocol = IdentityAlias()


def set_active(aliaser: AliasProtocol) -> None:
    """Replace the module-level aliaser. Called by an alias skin at startup."""
    global _active
    _active = aliaser


def to_wire(name: str) -> str:
    """Map a real tool/agent name to its wire form via the active aliaser."""
    return _active.to_wire(name)


def to_real(name: str) -> str:
    """Map a wire tool/agent name back to its real form via the active aliaser."""
    return _active.to_real(name)


def rewrite_outbound(text: str) -> str:
    """Rewrite outbound text (real → wire names) via the active aliaser."""
    return _active.rewrite_outbound(text)


def rewrite_inbound(text: str) -> str:
    """Rewrite inbound text (wire → real names) via the active aliaser."""
    return _active.rewrite_inbound(text)


def mapping() -> dict[str, str]:
    """The active aliaser's real→wire name mapping (empty for IdentityAlias)."""
    return _active.mapping()


def enabled() -> bool:
    """Whether a non-passthrough aliaser is active."""
    return _active.enabled()
