"""Transport-independent structural redaction for policy audit data."""

from __future__ import annotations

import unicodedata
import urllib.parse
from collections.abc import Iterable, Mapping
from typing import Any, Final

_SECRET_FIELD_NAMES: Final = frozenset(
    {
        "password",
        "passwd",
        "pass",
        "pwd",
        "ssh_key",
        "private_key",
        "privkey",
        "api_key",
        "apikey",
        "api_token",
        "apitoken",
        "auth_token",
        "bearer_token",
        "secret",
        "authorization",
        "access_token",
        "refresh_token",
        "session_token",
        "client_secret",
        "secret_key",
        "access_key",
        "x-api-key",
        "x_api_key",
        "jwt",
        "token",
    }
)
_SECRET_FIELD_NAMES_CRED_ONLY: Final = frozenset({"value", "hash", "token", "secret_value"})
_CRED_TOOL_MARKERS: Final = ("cred_record", "cred_search")
_REDACTED_SECRET: Final = "<redacted-secret>"

_EXTRA_SECRET_FIELD_NAMES: set[str] = set()
_EXTRA_CRED_TOOL_MARKERS: set[str] = set()


def register_secret_fields(names: Iterable[str]) -> None:
    """Add field names whose values must be removed from durable records."""
    _EXTRA_SECRET_FIELD_NAMES.update(name.lower() for name in names)


def _normalized_field_name(value: str) -> str:
    decoded = value
    for _ in range(3):
        expanded = urllib.parse.unquote(decoded)
        if expanded == decoded:
            break
        decoded = expanded
    return "".join(
        character
        for character in decoded.lower().replace("-", "_")
        if not unicodedata.category(character).startswith("C")
    )


def register_cred_tool_markers(markers: Iterable[str]) -> None:
    """Add tool markers whose generic credential-value fields are secrets."""
    _EXTRA_CRED_TOOL_MARKERS.update(marker.lower() for marker in markers)


def redact_secret_fields(content: Any, *, tool: str | None = None) -> Any:
    """Deep-copy nested content while replacing values of secret-named fields."""
    is_credential_tool = tool is not None and any(
        marker in tool.lower() for marker in (*_CRED_TOOL_MARKERS, *_EXTRA_CRED_TOOL_MARKERS)
    )

    secret_names = _SECRET_FIELD_NAMES | frozenset(
        _normalized_field_name(name) for name in _EXTRA_SECRET_FIELD_NAMES
    )
    secret_values: set[str] = set()

    def collect_leaves(value: Any) -> None:
        if isinstance(value, Mapping):
            for nested in value.values():
                collect_leaves(nested)
        elif isinstance(value, (list, tuple)):
            for nested in value:
                collect_leaves(nested)
        elif isinstance(value, str) and value:
            secret_values.add(value)

    def collect(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, nested in value.items():
                normalized_key = _normalized_field_name(key) if isinstance(key, str) else key
                is_secret = normalized_key in secret_names or (
                    is_credential_tool and normalized_key in _SECRET_FIELD_NAMES_CRED_ONLY
                )
                if is_secret:
                    collect_leaves(nested)
                else:
                    collect(nested)
        elif isinstance(value, (list, tuple)):
            for nested in value:
                collect(nested)

    collect(content)

    def walk(value: Any) -> Any:
        if isinstance(value, Mapping):
            redacted: dict[Any, Any] = {}
            for key, nested in value.items():
                normalized_key = _normalized_field_name(key) if isinstance(key, str) else key
                is_secret = normalized_key in secret_names or (
                    is_credential_tool and normalized_key in _SECRET_FIELD_NAMES_CRED_ONLY
                )
                redacted[key] = _REDACTED_SECRET if is_secret else walk(nested)
            return redacted
        if isinstance(value, list):
            return [walk(item) for item in value]
        if isinstance(value, tuple):
            return tuple(walk(item) for item in value)
        if isinstance(value, str) and value in secret_values:
            return _REDACTED_SECRET
        return value

    return walk(content)


_redact_secret_fields = redact_secret_fields


__all__ = [
    "_redact_secret_fields",
    "redact_secret_fields",
    "register_cred_tool_markers",
    "register_secret_fields",
]
