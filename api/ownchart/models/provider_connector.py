import uuid

from sqlalchemy import Boolean, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, new_uuid


class ProviderConnector(Base, TimestampMixin):
    """Registry of EHR endpoints OwnChart can authenticate against.

    Seeded from `infra/connectors.seed.yaml` on deploy. One row per provider.
    Includes the `client_id` registered with that EHR vendor (or null until
    OwnChart is registered with them).
    """

    __tablename__ = "provider_connectors"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    # epic | athena | cerner | unknown
    ehr_vendor: Mapped[str | None] = mapped_column(String(32))

    # FHIR base URL — e.g. https://mychart.stanfordhealthcare.org/MyChart/api/FHIR/R4
    fhir_base: Mapped[str] = mapped_column(String(1024), nullable=False)

    # Discovery URL: ${fhir_base}/.well-known/smart-configuration. Cached after first hit.
    smart_config_url: Mapped[str | None] = mapped_column(String(1024))
    authorize_endpoint: Mapped[str | None] = mapped_column(String(1024))
    token_endpoint: Mapped[str | None] = mapped_column(String(1024))

    # Registered with the EHR. Null until OwnChart is registered.
    client_id: Mapped[str | None] = mapped_column(String(255))

    # Space-separated SMART scopes. Epic 2026-05-09 registration used
    # SMART v1 syntax — `patient/*.read` (not `.rs`). offline_access dropped
    # because USCDI v3 auto-download forbids refresh tokens unless we upload
    # client credentials per Epic customer.
    scopes: Mapped[str] = mapped_column(
        String(1024),
        default="openid fhirUser launch/patient patient/*.read",
        nullable=False,
    )

    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    raw_config: Mapped[dict | None] = mapped_column(JSONB)
