import uuid

from sqlalchemy import ARRAY, Boolean, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, new_uuid


class ModelRun(Base, TimestampMixin):
    """Audit record for every external LLM call.

    Required for the consent / provenance contract — if a fact's
    `extraction_method` references an LLM, the `model_run_id` must
    point here.
    """

    __tablename__ = "model_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)

    provider: Mapped[str] = mapped_column(String(32), nullable=False)   # "anthropic"
    model: Mapped[str] = mapped_column(String(128), nullable=False)     # e.g. "claude-opus-4-7"
    purpose: Mapped[str] = mapped_column(String(64), nullable=False)    # "extract_fax_vision" etc.

    input_source_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), default=list, nullable=False
    )
    input_hash: Mapped[str | None] = mapped_column(String(128))         # sha256 of the rendered prompt+inputs
    output_hash: Mapped[str | None] = mapped_column(String(128))

    # `<prompt_id>@<version>` from prompts/*.yaml
    prompt_version: Mapped[str] = mapped_column(String(255), nullable=False)

    consent_state: Mapped[bool] = mapped_column(Boolean, nullable=False)

    usage: Mapped[dict | None] = mapped_column(JSONB)                   # tokens, latency_ms, etc.
    error: Mapped[str | None] = mapped_column(String)

    # Path under data/model_runs/<id>/ holding the materialized rendered
    # prompt + inputs that were sent. Lets the user audit exactly what
    # left the host. Optional — set when OWNCHART_DEBUG_PAYLOADS is on,
    # or for runs the user explicitly flags for audit.
    prompt_artifact_path: Mapped[str | None] = mapped_column(String(1024))
