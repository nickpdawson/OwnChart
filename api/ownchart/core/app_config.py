"""Declarative, non-secret application config loaded from YAML.

Three-tier config shape for OwnChart (open-source self-hosted posture):

  Tier 1 — Secrets (env vars from infra/.env):
            session_secret, token_dek, db_password, anthropic_api_key,
            *_CLIENT_ID, smtp_password, etc. Already handled by
            `core.config.Settings` (BaseSettings).

  Tier 2 — Non-secret app config (this module, infra/config.yaml):
            instance branding, auth posture, smtp settings sans password,
            llm model defaults, ingest caps, privacy defaults.

  Tier 3 — Domain seed data (infra/connectors.seed.yaml,
            api/ownchart/prompts/*.yaml): provider connectors, LLM prompts.

`get_app_config()` is the single read entry point. Defaults are reasonable
when no config.yaml is present (env-only deployments work). When config.yaml
is mounted at /app/infra/config.yaml, its values take effect.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from .logger import get_logger

log = get_logger("ownchart.core.app_config")

DEFAULT_CONFIG_PATH = "/app/infra/config.yaml"


class InstanceConfig(BaseModel):
    name: str = "OwnChart"
    public_base_url: str = "http://localhost:8080"
    owner_contact: str | None = None


class AuthConfig(BaseModel):
    allow_self_registration: bool = False  # V1 single-tenant
    require_email_verification: bool = False  # only meaningful with smtp.enabled
    session_max_age_days: int = 14
    session_cookie_name: str = "ownchart_session"


class SmtpConfig(BaseModel):
    """When `enabled=False` (default), password reset / email verification UI
    affordances are hidden and any backend code that would email is a no-op.
    Password is sourced from OWNCHART_SMTP_PASSWORD env var (Tier 1)."""

    enabled: bool = False
    host: str | None = None
    port: int = 587
    use_tls: bool = True
    username: str | None = None
    from_address: str | None = None


class LlmConfig(BaseModel):
    provider: str = "anthropic"  # only "anthropic" supported in V1
    default_model: str = "claude-opus-4-7"
    vision_model: str = "claude-opus-4-7"
    max_concurrent_calls: int = 4


class PrivacyConfig(BaseModel):
    """OWNCHART_DEBUG_PAYLOADS env var still wins when set (operational override)."""

    debug_payloads_default: bool = False
    exif_strip_gps_default: bool = False  # V1.1


class IngestConfig(BaseModel):
    max_attachment_bytes: int = 50 * 1024 * 1024  # 50 MB per FHIR attachment
    max_pdf_pages: int = 200
    vision_extraction_enabled: bool = True
    cost_warning_pages: int = 20


class RetentionConfig(BaseModel):
    """Most retention is "forever" in OwnChart — patient owns the record by
    definition. The transient values are the only knobs."""

    oauth_state_ttl_minutes: int = 10


class AppConfig(BaseModel):
    instance: InstanceConfig = Field(default_factory=InstanceConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    smtp: SmtpConfig = Field(default_factory=SmtpConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)
    ingest: IngestConfig = Field(default_factory=IngestConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)


@lru_cache(maxsize=1)
def get_app_config() -> AppConfig:
    path_str = os.environ.get("OWNCHART_CONFIG_PATH", DEFAULT_CONFIG_PATH)
    path = Path(path_str)
    if not path.exists():
        log.info("app_config_using_defaults", reason="config.yaml not present", path=str(path))
        return AppConfig()
    try:
        raw = yaml.safe_load(path.read_text()) or {}
        cfg = AppConfig.model_validate(raw)
        log.info("app_config_loaded", path=str(path), instance=cfg.instance.name)
        return cfg
    except Exception as e:  # noqa: BLE001
        # Don't crash the api on a malformed yaml — log and fall back to
        # defaults so the operator can still log in and fix it.
        log.warning("app_config_load_failed", path=str(path), error=str(e))
        return AppConfig()
