"""HTTP 契约测试（P1 / TDD 7.4）。

用 dispatcher 注入替代 Celery，专注验证协议层：幂等、状态码、鉴权与查询。
"""

import pytest
from fastapi.testclient import TestClient

from competitor_intel.api import create_app
from competitor_intel.config import Settings, Target
from competitor_intel.models import CompetitorSnapshot, PricingTier
from competitor_intel.store import init_db, save_snapshot


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        llm_api_key="x",
        db_path=str(tmp_path / "api.db"),
        report_dir=str(tmp_path / "reports"),
    )


@pytest.fixture
def dispatched() -> list[tuple]:
    return []


@pytest.fixture
def client(settings, dispatched) -> TestClient:
    def fake_dispatch(run_id, config_path, competitor, force):
        dispatched.append((run_id, config_path, competitor, force))

    app = create_app(settings, dispatcher=fake_dispatch)
    with TestClient(app) as c:
        yield c


def test_post_runs_is_idempotent_per_day(client, dispatched):
    first = client.post("/runs", json={"competitor": "acme"})
    assert first.status_code == 202
    run_id = first.json()["run_id"]
    assert first.json()["idempotent"] is False

    second = client.post("/runs", json={"competitor": "acme"})
    assert second.status_code == 200
    assert second.json()["run_id"] == run_id
    assert second.json()["idempotent"] is True
    # 只投递一次，避免同日重复抓取
    assert len(dispatched) == 1

    other = client.post("/runs", json={"competitor": "beta"})
    assert other.status_code == 202
    assert other.json()["run_id"] != run_id
    assert len(dispatched) == 2


def test_get_run_and_404(client):
    run_id = client.post("/runs", json={}).json()["run_id"]
    body = client.get(f"/runs/{run_id}").json()
    assert body["status"] == "queued"
    assert body["counts"] == {}

    missing = client.get("/runs/deadbeef")
    assert missing.status_code == 404


def test_failed_run_is_redispatched(client, dispatched, settings):
    import asyncio

    from competitor_intel.store import update_run

    run_id = client.post("/runs", json={"competitor": "acme"}).json()["run_id"]
    asyncio.run(update_run(settings.db_path, run_id, status="failed", error_summary="llm 401"))

    retry = client.post("/runs", json={"competitor": "acme"})
    assert retry.status_code == 202
    assert retry.json()["run_id"] == run_id
    assert retry.json()["idempotent"] is False
    assert len(dispatched) == 2
    assert client.get(f"/runs/{run_id}").json()["status"] == "queued"



def test_snapshots_query_filters(client, settings):
    import asyncio

    target = Target(
        competitor="acme",
        page_type="pricing",
        url="https://acme.example/pricing",
        main_selector="main",
    )
    snap = CompetitorSnapshot(
        competitor_name="acme",
        page_url=target.url,
        snapshot_date="2026-11-01",
        page_type="pricing",
        pricing_tiers=[PricingTier(name="Pro", amount=99.0, currency="CNY")],
    )
    asyncio.run(init_db(settings.db_path))
    asyncio.run(
        save_snapshot(
            settings.db_path, target, snap, content_hash="h1", status="extracted"
        )
    )

    body = client.get("/snapshots", params={"competitor": "acme"}).json()
    assert body["count"] == 1
    assert body["items"][0]["snapshot_date"] == "2026-11-01"
    assert "payload_json" not in body["items"][0]

    none = client.get("/snapshots", params={"competitor": "beta"}).json()
    assert none["count"] == 0


def test_latest_report_404_then_markdown(client, settings):
    assert client.get("/reports/latest").status_code == 404

    from pathlib import Path

    report_dir = Path(settings.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "2026-11-02.md").write_text("# 报告\n\n- Pro 99 -> 129", encoding="utf-8")

    resp = client.get("/reports/latest")
    assert resp.status_code == 200
    assert "Pro 99 -> 129" in resp.text
    assert resp.headers["content-type"].startswith("text/markdown")


def test_api_token_required_when_configured(settings, dispatched):
    settings.api_token = "s3cret"
    app = create_app(settings, dispatcher=lambda *a: dispatched.append(a))
    with TestClient(app) as c:
        assert c.get("/healthz").status_code == 200  # 探活不鉴权
        assert c.post("/runs", json={}).status_code == 401
        ok = c.post("/runs", json={}, headers={"X-API-Token": "s3cret"})
        assert ok.status_code == 202
        assert len(dispatched) == 1
