"""编排层集成测试（TDD 4.1 / 9.1）。

mock 采集与 LLM，验证：plan->fetch->diff->analyze->report 全链路、
失败目标记 errors 不拖垮整批、仅 changed 进入报告。
"""

from datetime import date
from pathlib import Path

import pytest

from competitor_intel import graph as graph_mod
from competitor_intel.collector import FetchResult
from competitor_intel.config import Settings, Target
from competitor_intel.graph import build_graph
from competitor_intel.models import CompetitorSnapshot, PricingTier
from competitor_intel.store import init_db, save_snapshot

TODAY = date.today().isoformat()


def _snap(competitor: str, url: str, day: str, amount: float) -> CompetitorSnapshot:
    return CompetitorSnapshot(
        competitor_name=competitor,
        page_url=url,
        snapshot_date=day,
        page_type="pricing",
        pricing_tiers=[PricingTier(name="Pro", amount=amount, currency="CNY")],
    )


@pytest.fixture
def settings(tmp_path):
    return Settings(
        llm_api_key="x",
        db_path=str(tmp_path / "snapshots.db"),
        report_dir=str(tmp_path / "reports"),
    )


@pytest.fixture
def config_path(tmp_path):
    p = tmp_path / "targets.yaml"
    p.write_text(
        """defaults:
  request_interval_s: 0
  respect_robots: false
targets:
  - competitor: acme
    pages:
      - type: pricing
        url: https://acme.example/pricing
        main_selector: main
  - competitor: beta
    pages:
      - type: pricing
        url: https://beta.example/pricing
        main_selector: main
""",
        encoding="utf-8",
    )
    return str(p)


def _initial(config_path: str) -> dict:
    return {"config_path": config_path, "competitor_filter": None, "force": False}


async def test_pipeline_end_to_end(monkeypatch, settings, config_path):
    """acme 有基线且涨价 -> 进 diff；beta 首次采集 -> 无基线不进 diff。"""
    await init_db(settings.db_path)
    acme_url = "https://acme.example/pricing"
    await save_snapshot(
        settings.db_path,
        Target(competitor="acme", page_type="pricing", url=acme_url, main_selector="main"),
        _snap("acme", acme_url, "2026-10-25", 99.0),
        content_hash="h0",
        status="extracted",
    )

    async def fake_fetch(target, *, force=False, settings=None, defaults=None):
        return FetchResult(
            target=target,
            status="extracted",
            content_hash="h1",
            snapshot=_snap(target.competitor, target.url, TODAY, 129.0),
        )

    async def fake_analyze(diff, context=None, *, settings=None):
        return "Pro 套餐涨价。"

    monkeypatch.setattr(graph_mod, "fetch_and_extract", fake_fetch)
    monkeypatch.setattr(graph_mod, "analyze_diff", fake_analyze)

    state = await build_graph(settings).ainvoke(_initial(config_path))

    assert len(state["results"]) == 2
    assert state["errors"] == []
    assert len(state["diffs"]) == 1
    assert state["diffs"][0].competitor == "acme"
    assert state["analyses"] == ["Pro 套餐涨价。"]

    text = Path(state["report_path"]).read_text(encoding="utf-8")
    assert "acme" in text and "涨价" in text


async def test_fetch_failure_does_not_break_batch(monkeypatch, settings, config_path):
    """单个目标异常只记 errors，其余目标照常完成并出报告。"""

    async def fake_fetch(target, *, force=False, settings=None, defaults=None):
        if target.competitor == "acme":
            raise RuntimeError("网络不可达")
        return FetchResult(target=target, status="skipped", content_hash="h0")

    monkeypatch.setattr(graph_mod, "fetch_and_extract", fake_fetch)

    state = await build_graph(settings).ainvoke(_initial(config_path))

    assert len(state["errors"]) == 1 and "acme" in state["errors"][0]
    assert state["skipped"] == ["beta https://beta.example/pricing"]
    # tenacity 重试 2 次后放弃：fake_fetch 对 acme 被调用 2 次（隐含验证不抛栈）
    assert state["report_path"]
