"""Settings registry loader + effective-value resolution.

The registry describes every configurable option. Values live in
the user_settings table. `effective(...)` resolves a key through the
precedence stack defined in docs/09 §99:

  1. Hard product default (None — falls through to registry default)
  2. Instance config file (V1: not implemented; admin locks live here later)
  3. Admin policy / lock (V1: not implemented)
  4. User setting
  5. Person/record setting (V1: not implemented)
  6. Source/dossier override (V1: not implemented)
  7. Runtime override (n/a here)

V1 collapses to: registry default → user_settings row.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

import yaml
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.user import User
from ..models.user_setting import UserSetting


class SettingsError(RuntimeError):
    pass


class Setting(BaseModel):
    key: str
    label: str
    description: str
    section: str
    scope: str
    storage: str
    type: str
    default: Any
    choices: list[Any] = Field(default_factory=list)
    ui_writable: bool = True
    file_writable: bool = False
    roles_allowed: list[str] = Field(default_factory=lambda: ["owner", "admin", "member"])
    requires_restart: bool = False
    phi_sensitive: bool = False
    admin_lockable: bool = False


class _Registry:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._by_key: dict[str, Setting] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            raise SettingsError(f"Settings registry not found: {self.path}")
        with self.path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        for entry in raw.get("settings", []):
            try:
                setting = Setting.model_validate(entry)
            except Exception as e:  # noqa: BLE001
                raise SettingsError(
                    f"Invalid settings registry entry {entry.get('key')!r}: {e}"
                ) from e
            if setting.key in self._by_key:
                raise SettingsError(f"Duplicate settings key: {setting.key}")
            self._by_key[setting.key] = setting

    def get(self, key: str) -> Setting:
        if key not in self._by_key:
            raise SettingsError(f"Unknown settings key: {key}")
        return self._by_key[key]

    def all(self) -> list[Setting]:
        return list(self._by_key.values())


@lru_cache(maxsize=1)
def get_registry() -> _Registry:
    here = Path(__file__).resolve().parent / "registry.yaml"
    return _Registry(here)


def list_settings() -> list[Setting]:
    return get_registry().all()


def coerce_value(setting: Setting, value: Any) -> Any:
    """Validate and coerce an incoming value against the registry shape.

    Raises SettingsError on type/choice mismatch.
    """
    if value is None:
        if setting.type == "boolean":
            return False
        return setting.default
    t = setting.type
    if t == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in {"true", "false"}:
            return value.lower() == "true"
        raise SettingsError(f"{setting.key} expects boolean, got {value!r}")
    if t == "integer":
        try:
            return int(value)
        except (TypeError, ValueError) as e:
            raise SettingsError(f"{setting.key} expects integer, got {value!r}") from e
    if t == "string":
        return str(value)
    if t == "enum":
        if value not in setting.choices:
            raise SettingsError(
                f"{setting.key} must be one of {setting.choices}, got {value!r}"
            )
        return value
    raise SettingsError(f"{setting.key} has unsupported type: {t}")


async def effective(db: AsyncSession, user: User, key: str) -> Any:
    """Return the effective value of one setting for a user."""
    setting = get_registry().get(key)
    row = (await db.execute(
        select(UserSetting)
        .where(UserSetting.user_id == user.id)
        .where(UserSetting.key == key)
    )).scalar_one_or_none()
    if row is None:
        return setting.default
    return row.value


async def effective_all(
    db: AsyncSession,
    user: User,
    *,
    keys: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Return every setting's effective value for a user.

    `keys`, if given, restricts to that subset.
    """
    settings = (
        [get_registry().get(k) for k in keys]
        if keys is not None
        else list_settings()
    )
    overrides_q = select(UserSetting).where(UserSetting.user_id == user.id)
    if keys is not None:
        overrides_q = overrides_q.where(UserSetting.key.in_(list(keys)))
    overrides = {row.key: row.value for row in (await db.execute(overrides_q)).scalars().all()}
    return {s.key: overrides.get(s.key, s.default) for s in settings}
