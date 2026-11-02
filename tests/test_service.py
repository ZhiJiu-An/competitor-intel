"""服务层测试（P1）：计数、三态判定、幂等执行记录。"""

from types import SimpleNamespace

import pytest

from competitor_intel import service
from competitor_intel.config import Settings
from competitor_intel.service import decide_status, execute_run, idempotency_key, summarize
from competitor_intel.store import get_run


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

