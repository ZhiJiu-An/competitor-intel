"""快照存储集成测试（TDD 5.2 / 9.1）。"""

import pytest

from competitor_intel.config import Target
from competitor_intel.models import CompetitorSnapshot, PricingTier
from competitor_intel.store import (
    init_db,
    latest_hash,
    latest_snapshot,
    list_review_queue,
    save_snapshot,
    snapshots_since,
)


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


def _snap(date: str, amount: float = 99.0) -> CompetitorSnapshot:
    return CompetitorSnapshot(
        competitor_name="acme",
        page_url="https://acme.example/pricing",
        snapshot_date=date,
        page_type="pricing",
        pricing_tiers=[PricingTier(name="Pro", amount=amount, currency="CNY")],
    )


async def test_roundtrip_and_latest(db_path, target):
    await init_db(db_path)
    await save_snapshot(db_path, target, _snap("2026-10-25"), content_hash="h1", status="extracted")
    await save_snapshot(db_path, target, _snap("2026-11-01", 129.0), content_hash="h2", status="extracted")

    latest = await latest_snapshot(db_path, "acme", target.url)
    assert latest is not None and latest.snapshot_date == "2026-11-01"
    assert latest.pricing_tiers[0].amount == 129.0

    baseline = await latest_snapshot(db_path, "acme", target.url, before="2026-11-01")
    assert baseline is not None and baseline.snapshot_date == "2026-10-25"

    assert await latest_hash(db_path, "acme", target.url) == "h2"


async def test_same_day_upsert(db_path, target):
    await init_db(db_path)
    await save_snapshot(db_path, target, _snap("2026-11-01"), content_hash="h1", status="extracted")
    await save_snapshot(db_path, target, _snap("2026-11-01", 88.0), content_hash="h2", status="extracted")
    latest = await latest_snapshot(db_path, "acme", target.url)
    assert latest is not None and latest.pricing_tiers[0].amount == 88.0


async def test_extraction_error_goes_to_review_queue(db_path, target):
    await init_db(db_path)
    await save_snapshot(
        db_path, target, None,
        content_hash="h9", status="extraction_error", error_detail="schema: amount 类型错误",
    )
    queue = await list_review_queue(db_path)
    assert len(queue) == 1
    assert "amount" in queue[0]["error_detail"]
    # 失败记录不污染基线
    assert await latest_snapshot(db_path, "acme", target.url) is None


async def test_skipped_not_returned_as_baseline(db_path, target):
    await init_db(db_path)
    await save_snapshot(db_path, target, _snap("2026-10-25"), content_hash="h1", status="extracted")
    await save_snapshot(db_path, target, None, content_hash="h1", status="skipped")
    latest = await latest_snapshot(db_path, "acme", target.url)
    assert latest is not None and latest.snapshot_date == "2026-10-25"


async def test_invalid_status_rejected(db_path, target):
    await init_db(db_path)
    with pytest.raises(ValueError):
        await save_snapshot(db_path, target, None, content_hash="h", status="unknown")


async def test_snapshots_since(db_path, target):
    await init_db(db_path)
    await save_snapshot(db_path, target, _snap("2026-10-20"), content_hash="h0", status="extracted")
    await save_snapshot(db_path, target, _snap("2026-11-01"), content_hash="h1", status="extracted")
    snaps = await snapshots_since(db_path, "2026-10-25")
    assert [s.snapshot_date for s in snaps] == ["2026-11-01"]
