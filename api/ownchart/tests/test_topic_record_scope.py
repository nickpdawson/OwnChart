"""Topic + TopicBrief per-record scope tests (Beta 1 M02, migration 0032).

Pure-function tests for the model shape change. The actual UNIQUE
constraint behavior is exercised by the migration integration test
in CI; here we just pin that the SQLAlchemy model carries the
`person_record_id` column at the expected nullability and that
existing fields are preserved.

PM resolution A-1 (2026-05-17): topics become per-record;
UNIQUE(person_record_id, slug) and UNIQUE(person_record_id, name)
replace the global uniques. Two records can each have a "Knee"
dossier without collision.
"""

from __future__ import annotations

# Import the full models package so the PersonRecord table is
# registered in metadata before Topic + TopicBrief FKs are resolved.
import ownchart.models  # noqa: F401
from ownchart.models.topic import Topic
from ownchart.models.topic_brief import TopicBrief


def test_topic_carries_person_record_id_not_null():
    """The model column must be NOT NULL — that's the M02 contract.
    Pre-0032 Topic rows had no `person_record_id`; this test ensures
    the SQLAlchemy class declaration is aligned with the post-0032
    schema."""
    col = Topic.__table__.c["person_record_id"]
    assert col.nullable is False
    # FK to person_records.id with ON DELETE CASCADE.
    fks = list(col.foreign_keys)
    assert len(fks) == 1
    assert fks[0].column.table.name == "person_records"
    assert fks[0].ondelete == "CASCADE"


def test_topic_name_and_slug_lost_global_unique_flag():
    """The per-column `unique=True` flags were removed in the model
    because uniqueness is now composite (per-record). The migration
    drops the legacy column-level uniques; the model must not
    re-emit them."""
    name_col = Topic.__table__.c["name"]
    slug_col = Topic.__table__.c["slug"]
    assert name_col.unique is not True
    assert slug_col.unique is not True


def test_topic_brief_carries_person_record_id_not_null():
    """TopicBrief denormalizes person_record_id from its parent
    topic. NOT NULL post-0032."""
    col = TopicBrief.__table__.c["person_record_id"]
    assert col.nullable is False
    fks = list(col.foreign_keys)
    assert len(fks) == 1
    assert fks[0].column.table.name == "person_records"
    assert fks[0].ondelete == "CASCADE"


def test_topic_existing_columns_preserved():
    """Defensive: the migration adds a column without removing the
    existing label_patterns / aliases / description / created_by
    columns. If a future hand-edit accidentally drops one, this
    test fails."""
    cols = {c.name for c in Topic.__table__.columns}
    expected_preserved = {
        "id", "name", "slug", "aliases", "label_patterns",
        "description", "related_concepts", "created_by",
        "created_at", "updated_at",
    }
    missing = expected_preserved - cols
    assert not missing, f"Topic lost columns: {missing}"
    # And the new column is there too.
    assert "person_record_id" in cols
