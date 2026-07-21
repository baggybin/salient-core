"""Fail-closed recognition of unresolved scope-bearing placeholders."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Final

# Recipes are prompt-instructed to carry operator infrastructure coordinates as
# `<lhost>`/`<lport>` (own listener) and `<rhost>`/`<rport>` (remote target)
# placeholders instead of real values (see the salient skin's
# prompts/recipe_discipline.md). If those tokens survive into a live invocation
# the token sweep finds no target and the call would be treated as targetless
# -> allowed; recognizing them here fails the call closed instead.
# NOTE: this pattern is bound to that prompt convention — if the convention
# gains tokens, widen this regex in lockstep.
_OPERATOR_INFRA: Final = re.compile(r"<(?:[lr]host|[lr]port)>", re.IGNORECASE)

# Structural scan depth cap. Set far above any legitimate tool-arg nesting so a
# breach only ever reflects hostile / malformed input, never a real payload.
_MAX_SCAN_DEPTH: Final = 64


def unresolved_operator_infra_placeholder(value: object, _depth: int = 0) -> str | None:
    """Return the first infrastructure placeholder still present in a value.

    ``value`` is deliberately typed ``object``: it arrives from untyped tool
    args (``dict[str, Any]``), so this function must total-cover every runtime
    type without raising on the ones it cannot scan.

    - Types that can hold the literal token (``str`` / ``bytes``) are scanned.
    - Containers are recursed, bounded by ``_MAX_SCAN_DEPTH``.
    - Any other type returns ``None`` — the ``<lhost>``/``<rhost>`` family is
      literal text and structurally cannot live in a non-string scalar, so
      "no placeholder detectable here" is the honest answer. This is deferral,
      NOT trust: the kind-specific extractor still validates the fields it
      actually consumes.
    - An aborted scan (depth exceeded) fails CLOSED via ``ExtractorError``: a
      partial walk of a container that *can* hold strings cannot rule out a
      hidden placeholder. The depth counter also bounds self-referential
      (cyclic) containers, which would otherwise raise ``RecursionError`` — an
      exception the scope gate does not catch.
    """
    if _depth > _MAX_SCAN_DEPTH:
        # Local import avoids a module-load cycle: scope.py imports this module.
        from .scope import ExtractorError

        raise ExtractorError(
            f"placeholder scan exceeded max depth {_MAX_SCAN_DEPTH}; "
            "refusing to evaluate arbitrarily nested tool arguments"
        )
    match value:
        case str():
            regex_match = _OPERATOR_INFRA.search(value)
            return regex_match.group(0) if regex_match is not None else None
        case bytes() | bytearray():
            regex_match = _OPERATOR_INFRA.search(value.decode("utf-8", errors="replace"))
            return regex_match.group(0) if regex_match is not None else None
        case Mapping():
            for item in value.values():
                if placeholder := unresolved_operator_infra_placeholder(item, _depth + 1):
                    return placeholder
            return None
        case list() | tuple():
            for item in value:
                if placeholder := unresolved_operator_infra_placeholder(item, _depth + 1):
                    return placeholder
            return None
        case _:
            # Unscannable type (int/float/bool/None, set, datetime, custom
            # object, …): no string to search -> no placeholder possible.
            return None
