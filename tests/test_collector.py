"""采集层集成测试（TDD 9.1）。

mock 网络层（_fetch_html / _playwright_html / _extract），验证：
- 指纹短路（同内容第二次 skipped）
- 校验失败记 extraction_error 且入库进复核队列
- Crawl4AI 失败后走 Playwright 兜底且 used_fallback=True
- robots 禁止时记 blocked 且不入库

冒烟测试（真实单站点）需设环境变量 CI_SMOKE=1，默认跳过。
"""

import os

import pytest

from competitor_intel import collector
from competitor_intel.collector import fetch_and_extract
from competitor_intel.config import Defaults, Settings, Target
from competitor_intel.store import init_db, latest_snapshot, list_review_queue

HTML = "<html><body><main>Pro $99/month</main></body></html>"

PAYLOAD = {
    "pricing_tiers": [{"name": "Pro", "amount": 99.0, "currency": "USD"}],
    "feature_items": [{"name": "SSO", "description": "单点登录", "category": "安全"}],
    "target_user_note": "中小团队",
}


@pytest.fixture
def target():
    return Target(
        competitor="acme",
        page_type="pricing",
        url="https://acme.example/pricing",
        main_selector="main",
    )


@pytest.fixture
def settings(tmp_path):
    return Settings(llm_api_key="x", db_path=str(tmp_path / "snapshots.db"))


@pytest.fixture
def defaults():
    return Defaults(respect_robots=False, request_interval_s=0)


def _mock_network(monkeypatch, payload=PAYLOAD, html: str = HTML):
    async def fake_fetch(target, settings, defaults):
        return html

    async def fake_extract(html, url, settings):
        return payload

    monkeypatch.setattr(collector, "_fetch_html", fake_fetch)
    monkeypatch.setattr(collector, "_extract", fake_extract)


async def test_extract_and_fingerprint_shortcircuit(monkeypatch, target, settings, defaults):
    await init_db(settings.db_path)
    _mock_network(monkeypatch)

    first = await fetch_and_extract(target, settings=settings, defaults=defaults)
    assert first.status == "extracted"
    assert first.snapshot is not None
    assert first.snapshot.competitor_name == "acme"  # 身份字段由系统确定
    assert first.snapshot.pricing_tiers[0].name == "Pro"

    # 同内容第二次：指纹短路，不再抽取
    second = await fetch_and_extract(target, settings=settings, defaults=defaults)
    assert second.status == "skipped"
    assert second.snapshot is None

    # force=True 忽略指纹，重新抽取
    third = await fetch_and_extract(target, force=True, settings=settings, defaults=defaults)
    assert third.status == "extracted"


async def test_extraction_error_saved_to_review_queue(
    monkeypatch, target, settings, defaults
):
    await init_db(settings.db_path)
    # pricing 页抽不到套餐 -> 业务断言失败
    _mock_network(monkeypatch, payload={"pricing_tiers": []})

    result = await fetch_and_extract(target, settings=settings, defaults=defaults)
    assert result.status == "extraction_error"
    assert result.error_detail

    queue = await list_review_queue(settings.db_path)
    assert len(queue) == 1
    assert queue[0]["competitor"] == "acme"
    # 失败记录不产生可读快照
    assert await latest_snapshot(settings.db_path, "acme", target.url) is None


async def test_fallback_path(monkeypatch, target, settings, defaults):
    await init_db(settings.db_path)

    async def broken_fetch(target, settings, defaults):
        raise RuntimeError("crawl4ai 崩溃")

    async def fake_playwright(target, defaults, settings):
        return HTML

    async def fake_extract(html, url, settings):
        return PAYLOAD

    monkeypatch.setattr(collector, "_fetch_html", broken_fetch)
    monkeypatch.setattr(collector, "_playwright_html", fake_playwright)
    monkeypatch.setattr(collector, "_extract", fake_extract)

    result = await fetch_and_extract(target, settings=settings, defaults=defaults)
    assert result.status == "extracted"
    assert result.used_fallback is True


async def test_blocked_not_saved(monkeypatch, target, settings, defaults):
    await init_db(settings.db_path)
    _mock_network(monkeypatch)

    async def deny(url, defaults):
        return False

    monkeypatch.setattr(collector, "_allowed_by_robots", deny)
    result = await fetch_and_extract(target, settings=settings, defaults=defaults)
    assert result.status == "blocked"

    # blocked 不入库
    assert await list_review_queue(settings.db_path) == []
    assert await latest_snapshot(settings.db_path, "acme", target.url) is None


@pytest.mark.skipif(os.getenv("CI_SMOKE") != "1", reason="冒烟测试默认关闭")
async def test_smoke_real_site():
    pass
