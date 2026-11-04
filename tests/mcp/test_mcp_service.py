"""查询层契约：结构化快照、字段级 diff、复核队列、报告。"""

from __future__ import annotations

import pytest

from competitor_intel.adapters.mcp import queries as service
from competitor_intel.core.errors import CiError


async def test_list_targets_reads_yaml(settings):
    payload = service.list_targets(settings)

    assert payload["count"] == 1
    assert payload["targets"][0]["competitor"] == "acme"
    assert payload["targets"][0]["page_type"] == "pricing"


async def test_get_snapshot_returns_latest_structured_payload(settings):
    payload = await service.get_snapshot(settings, competitor="acme")

    assert payload["count"] == 1
    item = payload["items"][0]
    assert item["snapshot_date"] == "2026-10-27"
    assert item["snapshot"]["pricing_tiers"][0]["amount"] == 29.0


async def test_get_snapshot_can_travel_back_in_time(settings):
    payload = await service.get_snapshot(settings, competitor="acme", as_of="2026-10-21")

    assert payload["items"][0]["snapshot_date"] == "2026-10-20"


async def test_diff_reports_field_level_price_change(settings):
    result = await service.diff_snapshots(settings, competitor="acme")

    assert result["changed"] is True
    assert result["baseline_date"] == "2026-10-20"
    assert result["current_date"] == "2026-10-27"
    assert {"field": "pricing_tiers[Pro].amount", "old": "25.0", "new": "29.0"} in result[
        "changes"
    ]


async def test_diff_honours_explicit_dates(settings):
    result = await service.diff_snapshots(
        settings, competitor="acme", from_date="2026-10-20", to_date="2026-10-27"
    )

    assert result["baseline_date"] == "2026-10-20"
    assert result["changed"] is True


async def test_diff_to_date_is_as_of_not_exact_match(settings):
    """to_date 表示「截至某天」：传今天也应落到最近一条快照，而不是报错。"""
    result = await service.diff_snapshots(
        settings, competitor="acme", from_date="2026-10-20", to_date="2026-11-03"
    )

    assert result["current_date"] == "2026-10-27"
    assert result["changed"] is True


async def test_diff_without_baseline_explains_why(settings):
    with pytest.raises(CiError) as excinfo:
        await service.diff_snapshots(
            settings, competitor="acme", from_date="2026-10-19", to_date="2026-10-20"
        )

    assert excinfo.value.code == "NOT_FOUND"
    assert "2026-10-20" in (excinfo.value.hint or "")


async def test_list_changes_covers_all_competitors_in_one_call(settings):
    result = await service.list_changes(settings, since="2026-10-20")

    assert result["count"] == 2
    by_competitor = {item["competitor"]: item for item in result["items"]}
    assert by_competitor["acme"]["status"] == "changed"
    assert by_competitor["beta"]["baseline_date"] == "2026-10-20"
    assert by_competitor["beta"]["changes"][0]["field"] == "pricing_tiers[Pro].amount"


async def test_list_changes_hides_unchanged_pages_by_default(settings):
    assert (await service.list_changes(settings, since="2026-10-27"))["count"] == 0

    full = await service.list_changes(
        settings, since="2026-10-27", include_unchanged=True
    )
    assert full["count"] == 2
    assert {item["status"] for item in full["items"]} == {"unchanged"}


async def test_list_changes_marks_pages_without_baseline(settings):
    result = await service.list_changes(settings, since="2026-10-19")

    assert {item["status"] for item in result["items"]} == {"new"}
    assert all(item["baseline_date"] is None for item in result["items"])


async def test_review_queue_is_never_silently_empty(settings):
    queue = await service.get_review_queue(settings)

    assert queue["count"] == 1
    assert queue["items"][0]["error_detail"] == "缺少币种"


async def test_report_prefers_run_report_then_newest_markdown(settings):
    report = settings.report_dir / "2026-10-27.md"
    report.write_text("# 报告\n\nPro 涨价 16%", encoding="utf-8")

    payload = await service.get_report(settings)

    assert "Pro 涨价" in payload["markdown"]
    assert payload["path"].endswith("2026-10-27.md")


async def test_unknown_competitor_is_an_actionable_error(settings):
    with pytest.raises(CiError) as excinfo:
        await service.get_snapshot(settings, competitor="nobody")

    assert excinfo.value.code == "NOT_FOUND"
    assert excinfo.value.hint
