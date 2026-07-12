"""Transport-independent structural redaction for policy audit data."""

from __future__ import annotations

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


def register_cred_tool_markers(markers: Iterable[str]) -> None:
    """Add tool markers whose generic credential-value fields are secrets."""
    _EXTRA_CRED_TOOL_MARKERS.update(marker.lower() for marker in markers)


def redact_secret_fields(content: Any, *, tool: str | None = None) -> Any:
    """Deep-copy nested content while replacing values of secret-named fields."""
    is_credential_tool = tool is not None and any(
        marker in tool.lower() for marker in (*_CRED_TOOL_MARKERS, *_EXTRA_CRED_TOOL_MARKERS)
    )

    def walk(value: Any) -> Any:
        if isinstance(value, Mapping):
            redacted: dict[Any, Any] = {}
            for key, nested in value.items():
                normalized_key = key.lower() if isinstance(key, str) else key
                is_secret = (
                    normalized_key in _SECRET_FIELD_NAMES
                    or normalized_key in _EXTRA_SECRET_FIELD_NAMES
                    or (is_credential_tool and normalized_key in _SECRET_FIELD_NAMES_CRED_ONLY)
                )
                redacted[key] = _REDACTED_SECRET if is_secret else walk(nested)
            return redacted
        if isinstance(value, list):
            return [walk(item) for item in value]
        if isinstance(value, tuple):
            return tuple(walk(item) for item in value)
        return value

    return walk(content)


_redact_secret_fields = redact_secret_fields


__all__ = [
    "_redact_secret_fields",
    "redact_secret_fields",
    "register_cred_tool_markers",
    "register_secret_fields",
]
