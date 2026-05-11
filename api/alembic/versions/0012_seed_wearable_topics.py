"""Seed wearable lenses (Sleep / Heart / Activity / Body Metrics).

Revision ID: 0012_seed_wearable_topics
Revises: 0011_brief_messages
Create Date: 2026-05-09

Bridge migration. The 41k+ Auto Export facts ingested via the iOS
Health Auto Export REST push had nowhere to be visible — they
matched no existing topic. This adds four system-suggested topics
whose `label_patterns` line up with the labels the Auto Export
parser emits, so the facts immediately surface on dossier views.

Per docs/06: **topics are lenses, not the destination.** The real
home for wearable / quantified-self data is the global timeline +
Discover surface + per-metric layers (#44–#46). These four topics
are a 30-minute bridge to make the data visible until that lands.
Treat them as system-suggested, not authoritative IA.

The patterns are POSIX regex matched case-insensitively via `~*`
(see retrieval/topics.py). `+`, `(`, `)` are escaped where present.
Patterns are anchored with `^` so they only fire on auto-export-
shaped labels; clinical mentions of "heart rate" mid-sentence in
a CCDA narrative wouldn't accidentally pull into the Heart topic.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0012_seed_wearable_topics"
down_revision: Union[str, None] = "0011_brief_messages"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_SEED_SQL = r"""
INSERT INTO topics (id, name, slug, aliases, label_patterns, description, related_concepts, created_at, updated_at)
VALUES
    (
        gen_random_uuid(),
        'Sleep',
        'sleep',
        ARRAY['sleep','insomnia','sleep apnea','sleep quality'],
        ARRAY[
            '^Sleep:',
            '^Sleep session'
        ],
        'Sleep duration, stages, and patterns. Bridge lens for Apple Health / wearable data; the global timeline is the long-term home.',
        ARRAY['sleep duration','REM','deep sleep','sleep efficiency'],
        now(), now()
    ),
    (
        gen_random_uuid(),
        'Heart',
        'heart',
        ARRAY['heart','cardiac','heart rate','blood pressure','hypertension','arrhythmia'],
        ARRAY[
            '^Heart rate:',
            '^Resting HR:',
            '^HRV ',
            '^VO',
            '^Respiratory rate:',
            '^SpO'
        ],
        'Heart rate, resting HR, HRV, VO₂ max, and related cardiovascular metrics. Bridge lens for wearable data.',
        ARRAY['heart rate','HRV','VO2 max','blood pressure','arrhythmia'],
        now(), now()
    ),
    (
        gen_random_uuid(),
        'Activity',
        'activity',
        ARRAY['activity','exercise','fitness','workouts','steps','movement'],
        ARRAY[
            '^Daily steps',
            '^Active energy',
            '^Resting energy',
            '^Exercise time',
            '^Stand hours',
            '^Stand time',
            '^Flights climbed',
            '^Walking \+ running',
            '^(Running|Cycling|Walking|Hiking|Swimming|Yoga|Strength|Pilates|HIIT|Rowing|Functional Strength|Core Training|Elliptical|Climbing|Mixed Cardio|Cooldown|Other):'
        ],
        'Steps, energy expenditure, exercise time, and individual workout sessions. Bridge lens for wearable data.',
        ARRAY['steps','energy expenditure','workout','VO2 max'],
        now(), now()
    ),
    (
        gen_random_uuid(),
        'Body metrics',
        'body-metrics',
        ARRAY['body composition','weight','bmi','body fat','lean mass','waist'],
        ARRAY[
            '^Weight:',
            '^BMI:',
            '^Body fat:',
            '^Lean body mass:',
            '^Waist:'
        ],
        'Weight, BMI, body composition, waist circumference. Bridge lens for wearable / scale data.',
        ARRAY['weight','BMI','body composition','waist circumference'],
        now(), now()
    )
ON CONFLICT (slug) DO NOTHING
"""


def upgrade() -> None:
    op.execute(_SEED_SQL)


def downgrade() -> None:
    op.execute(
        "DELETE FROM topics WHERE slug IN ('sleep','heart','activity','body-metrics')"
    )
