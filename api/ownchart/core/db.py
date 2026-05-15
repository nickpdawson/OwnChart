from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import get_settings

_settings = get_settings()

# 2026-05-13: bumped from SQLAlchemy defaults (5 + 10 overflow = 15)
# after iOS HK sync hit `QueuePool limit ... timeout 30.00` during
# parallel uploads. iOS fires 12-20 concurrent /api/healthkit/sync POSTs
# via URLSession with no client serialization; each one holds a
# connection while it does the per-sample EvidenceAnchor + ExtractedFact
# inserts. The failure manifested as a 500 from
# get_user_from_device_token_or_session() (auth dep runs before the
# route body, outside the route's try/except envelope).
#
# 20 + 40 overflow = 60 max concurrent on a Postgres default of 100
# max_connections — plenty of headroom on a single-tenant instance.
# pool_recycle dodges stale connections after a Postgres restart or
# long-idle conn that Postgres dropped on its side. pool_timeout=10
# fails fast on real saturation instead of letting clients hang 30s.
engine = create_async_engine(
    _settings.database_url,
    pool_pre_ping=True,
    pool_size=20,
    max_overflow=40,
    pool_recycle=3600,
    pool_timeout=10,
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
