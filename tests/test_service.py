"""服务层测试（P1）：计数、三态判定、幂等执行记录。"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from competitor_intel.core import orchestrator as service
from competitor_intel.core.collector import FetchResult
from competitor_intel.core.config import Settings, Target
from competitor_intel.core.models import CompetitorSnapshot, PricingTier
from competitor_intel.core.orchestrator import (
    decide_status,
    execute_run,
    idempotency_key,
    summarize,
)
from competitor_intel.core.store import get_run, save_snapshot

PAGE = "https://acme.example/pricing"


def _snapshot(day: str, amount: float) -> CompetitorSnapshot:
    return CompetitorSnapshot(
        competitor_name="acme",
        page_url=PAGE,
        snapshot_date=day,
        page_type="pricing",
        pricing_tiers=[PricingTier(name="Pro", amount=amount, currency="USD")],
    )


def _result(status: str, fallback: bool = False) -> SimpleNamespace:
    return SimpleNamespace(status=status, used_fallback=fallback)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        llm_api_key="x",
        db_path=str(tmp_path / "runs.db"),
        report_dir=str(tmp_path / "reports"),
    )


def test_summarize_counts_and_metrics():
    state = {
        "results": [
            _result("extracted", fallback=True),
            _result("skipped"),
            _result("skipped"),
            _result("extraction_error"),
        ],
        "errors": ["beta: timeout"],
    }
    counts = summarize(state)
    assert counts["extracted"] == 1
    assert counts["skipped"] == 2
    assert counts["errors"] == 1
    assert counts["metrics"]["skip_rate"] == 0.5
    assert counts["metrics"]["fallback_rate"] == 0.25
    assert counts["metrics"]["extraction_error_rate"] == 0.5


def test_decide_status_three_states():
    ok = {"processed": 3, "extracted": 3, "errors": 0, "extraction_error": 0, "blocked": 0}
    assert decide_status(ok) == "succeeded"
    assert decide_status({**ok, "extracted": 2, "errors": 1}) == "partial"
    assert decide_status({**ok, "extracted": 2, "extraction_error": 1}) == "partial"
    assert decide_status({**ok, "extracted": 2, "blocked": 1}) == "partial"
    assert decide_status({"processed": 2, "extracted": 0, "errors": 2, "extraction_error": 0, "blocked": 0}) == "failed"
    assert decide_status({"processed": 0, "extracted": 0, "errors": 0, "extraction_error": 0, "blocked": 0}) == "failed"
    # 全部 skipped（页面无变化）是正常结果，不能误判为失败
    assert decide_status({"processed": 3, "extracted": 0, "errors": 0, "extraction_error": 0, "blocked": 0}) == "succeeded"


def test_idempotency_key_is_daily_and_scoped():
    assert idempotency_key("acme", "2026-11-02") == "2026-11-02:acme"
    assert idempotency_key(None, "2026-11-02") == "2026-11-02:all"
    assert idempotency_key("acme", "2026-11-03") != idempotency_key("acme", "2026-11-02")


async def test_execute_run_records_counts(monkeypatch, settings):
    async def fake_pipeline(config_path, *, competitor=None, force=False, settings=None):
        return {
            "results": [_result("extracted"), _result("skipped")],
            "errors": [],
            "report_path": "data/reports/2026-11-02.md",
        }

    monkeypatch.setattr(service, "run_pipeline", fake_pipeline)

    record, created = await service.enqueue_run(settings, competitor="acme")
    assert created

    outcome = await execute_run(settings, run_id=record["id"], competitor="acme")
    assert outcome["status"] == "succeeded"
    assert outcome["extracted"] == 1
    assert outcome["skipped"] == 1

    stored = await get_run(settings.db_path, record["id"])
    assert stored["status"] == "succeeded"
    assert stored["report_path"] == "data/reports/2026-11-02.md"
    assert '"skipped": 1' in stored["counts_json"]


async def test_execute_run_marks_failed_on_exception(monkeypatch, settings):
    async def boom(*args, **kwargs):
        raise RuntimeError("crawl crashed")

    monkeypatch.setattr(service, "run_pipeline", boom)

    record, _ = await service.enqueue_run(settings, competitor="beta")
    with pytest.raises(RuntimeError):
        await execute_run(settings, run_id=record["id"], competitor="beta")

    stored = await get_run(settings.db_path, record["id"])
    assert stored["status"] == "failed"
    assert "crawl crashed" in stored["error_summary"]


async def test_failed_run_can_be_retried_same_day(monkeypatch, settings):
    async def boom(*args, **kwargs):
        raise RuntimeError("llm 401")

    monkeypatch.setattr(service, "run_pipeline", boom)
    record, _ = await service.enqueue_run(settings, competitor="acme")
    with pytest.raises(RuntimeError):
        await execute_run(settings, run_id=record["id"], competitor="acme")

    again, should_dispatch = await service.enqueue_run(settings, competitor="acme")
    assert should_dispatch is True
    assert again["id"] == record["id"]  # run_id 复用，便于追踪同一天的多次尝试
    assert again["status"] == "queued"


async def test_force_reruns_succeeded_run_same_day(monkeypatch, settings):
    async def fake_pipeline(config_path, *, competitor=None, force=False, settings=None):
        return {"results": [_result("extracted")], "errors": [], "report_path": None}

    monkeypatch.setattr(service, "run_pipeline", fake_pipeline)

    record, _ = await service.enqueue_run(settings, competitor="acme")
    await execute_run(settings, run_id=record["id"], competitor="acme")
    assert (await get_run(settings.db_path, record["id"]))["status"] == "succeeded"

    reused, created = await service.enqueue_run(settings, competitor="acme")
    assert created is False
    assert reused["status"] == "succeeded"

    forced, created = await service.enqueue_run(settings, competitor="acme", force=True)
    assert created is True
    assert forced["id"] == record["id"]
    assert forced["status"] == "queued"

    # 已经重新排队了，再调一次不能抢占正在跑的 run
    again, created = await service.enqueue_run(settings, competitor="acme")
    assert created is False
    assert again["status"] == "queued"


async def test_crawl_page_initializes_database_on_fresh_home(monkeypatch, settings):
    """临时抓取可能在从未执行 init-db 的新环境发生，不能直接报 no such table。"""
    snapshot = _snapshot("2026-11-04", 129.0)

    async def fake_fetch(target, **kwargs):
        return FetchResult(
            target=target, status="extracted", snapshot=snapshot, content_hash="h1"
        )

    monkeypatch.setattr(service, "fetch_and_extract", fake_fetch)
    monkeypatch.setattr(service, "ensure_allowed", lambda *a, **k: None)

    result = await service.crawl_page(settings, url=PAGE)

    assert result["status"] == "extracted"
    assert result["snapshot"]["pricing_tiers"][0]["amount"] == 129.0
    assert result["report"]["name"].startswith("adhoc/")


async def test_crawl_page_returns_baseline_when_fingerprint_unchanged(monkeypatch, settings):
    """指纹未变时上游不返回快照，但「眼睛」场景仍应拿到结构化内容。"""
    target = Target(competitor="acme", page_type="pricing", url=PAGE, main_selector="body")
    baseline = _snapshot("2026-11-03", 99.0)
    await service.init_db(settings.db_path)
    await save_snapshot(
        settings.db_path, target, baseline, content_hash="same", status="extracted"
    )

    async def fake_fetch(target, **kwargs):
        return FetchResult(target=target, status="skipped", content_hash="same")

    monkeypatch.setattr(service, "fetch_and_extract", fake_fetch)
    monkeypatch.setattr(service, "ensure_allowed", lambda *a, **k: None)

    result = await service.crawl_page(settings, url=PAGE, competitor="acme")

    assert result["status"] == "skipped"
    assert result["snapshot"]["pricing_tiers"][0]["amount"] == 99.0
    assert result["snapshot_date"] == "2026-11-03"
    assert result["report"] is None
    assert "force=true" in result["note"]


async def test_crawl_page_sanitizes_page_type_in_report_path(monkeypatch, settings):
    """page_type 是外部入参，不能靠它把报告写出 report_dir。"""
    snapshot = _snapshot("2026-11-04", 129.0)

    async def fake_fetch(target, **kwargs):
        return FetchResult(
            target=target, status="extracted", snapshot=snapshot, content_hash="h1"
        )

    monkeypatch.setattr(service, "fetch_and_extract", fake_fetch)
    monkeypatch.setattr(service, "ensure_allowed", lambda *a, **k: None)

    result = await service.crawl_page(
        settings, url=PAGE, competitor="acme", page_type="x/../../../evil"
    )

    path = Path(result["report"]["path"]).resolve()
    assert path.parent == (Path(settings.report_dir) / service.ADHOC_SUBDIR).resolve()
    assert path.is_file()

