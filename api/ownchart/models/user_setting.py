import uuid

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, new_uuid


class UserSetting(Base, TimestampMixin):
    """One per (user, registry key). Absence = use the registry default.

    The schema for every setting lives in
    `api/ownchart/settings/registry.yaml`. This table only carries
    the user-chosen values; types/labels/admin-locks come from the
    registry at read time.
    """

    __tablename__ = "user_settings"
    __table_args__ = (
        UniqueConstraint("user_id", "key", name="uq_user_settings_user_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    value: Mapped[dict | bool | str | int | float | list | None] = mapped_column(
        JSONB, nullable=False
    )
