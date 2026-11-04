"""origin 列：scheduled 覆盖 adhoc，adhoc 不覆盖 scheduled（日报口径）。"""

import aiosqlite
import pytest

from competitor_intel.core.config import Target
from competitor_intel.core.models import CompetitorSnapshot, PricingTier
from competitor_intel.core.store import init_db, save_snapshot, snapshots_since


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "snapshots.db"


@pytest.fixture
def target():
    return Target(
        competitor="acme",
        page_type="pricing",
        url="https://acme.example/pricing",
        main_selector="main",
    )


def _snap(date: str, amount: float) -> CompetitorSnapshot:
    return CompetitorSnapshot(
        competitor_name="acme",
        page_url="https://acme.example/pricing",
        snapshot_date=date,
        page_type="pricing",
        pricing_tiers=[PricingTier(name="Pro", amount=amount, currency="CNY")],
    )


async def _amount(db_path, target) -> float:
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT payload_json FROM snapshots WHERE page_url = ?", (target.url,)
        ) as cur:
            row = await cur.fetchone()
    return CompetitorSnapshot.model_validate_json(row[0]).pricing_tiers[0].amount


async def test_adhoc_does_not_overwrite_scheduled(db_path, target):
    await init_db(db_path)
    await save_snapshot(
        db_path, target, _snap("2026-11-01", 99.0),
        content_hash="h1", status="extracted", origin="scheduled",
    )
    await save_snapshot(
        db_path, target, _snap("2026-11-01", 79.0),
        content_hash="h2", status="extracted", origin="adhoc",
    )
    assert await _amount(db_path, target) == 99.0


async def test_scheduled_overwrites_adhoc(db_path, target):
    await init_db(db_path)
    await save_snapshot(
        db_path, target, _snap("2026-11-01", 79.0),
        content_hash="h1", status="extracted", origin="adhoc",
    )
    await save_snapshot(
        db_path, target, _snap("2026-11-01", 99.0),
        content_hash="h2", status="extracted", origin="scheduled",
    )
    assert await _amount(db_path, target) == 99.0


async def test_daily_report_reads_scheduled_only(db_path, target):
    await init_db(db_path)
    await save_snapshot(
        db_path, target, _snap("2026-11-01", 79.0),
        content_hash="h1", status="extracted", origin="adhoc",
    )
    await save_snapshot(
        db_path, target, _snap("2026-11-02", 99.0),
        content_hash="h2", status="extracted", origin="scheduled",
    )
    rows = await snapshots_since(db_path, "2026-11-01")
    assert [row.snapshot_date for row in rows] == ["2026-11-02"]


async def test_old_db_gets_origin_column(db_path):
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(
            """
            CREATE TABLE snapshots (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              competitor TEXT NOT NULL, page_url TEXT NOT NULL,
              page_type TEXT NOT NULL, snapshot_date TEXT NOT NULL,
              content_hash TEXT NOT NULL, status TEXT NOT NULL,
              payload_json TEXT, error_detail TEXT,
              created_at TEXT DEFAULT (datetime('now'))
            );
            INSERT INTO snapshots
              (competitor, page_url, page_type, snapshot_date, content_hash, status)
            VALUES ('acme', 'https://acme.example/pricing', 'pricing',
                    '2026-10-01', 'h0', 'extracted');
            """
        )
        await db.commit()

    await init_db(db_path)

    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT origin FROM snapshots") as cur:
            row = await cur.fetchone()
    assert row[0] == "scheduled"
