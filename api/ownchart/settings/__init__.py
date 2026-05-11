"""Settings registry — docs/09 implementation.

The registry (registry.yaml) is the schema; values live in the
`user_settings` table. `effective(...)` resolves a key through the
precedence stack: hard default → registry default → admin lock →
user override.
"""

from .registry import (
    Setting,
    SettingsError,
    coerce_value,
    effective,
    effective_all,
    get_registry,
    list_settings,
)

__all__ = [
    "Setting",
    "SettingsError",
    "coerce_value",
    "effective",
    "effective_all",
    "get_registry",
    "list_settings",
]
