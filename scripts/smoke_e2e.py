"""端到端冒烟：本地 http.server 伺服 fixtures + 假 LLM，跑通 run -> report。

用法：
    python scripts/smoke_e2e.py

流程：
1. 把 fixture 复制到 data/smoke/site，起本地 HTTP 服务器（不修改仓库内文件）；
2. 生成临时 targets.yaml 指向本地页面；
3. monkeypatch litellm.completion 返回定价 JSON（不真实调用 LLM）；
4. 调 run_pipeline（真实 Crawl4AI 抓取本地页）；
5. 修改副本页面再跑一次，验证指纹/变更检测与报告生成。

需要：pip install -e ".[dev]" 且 playwright install chromium。
"""

import asyncio
import json
import os
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
WORK = ROOT / "data" / "smoke"
SITE = WORK / "site"

ORIGINAL = FIXTURES.joinpath("sample_pricing.html").read_text(encoding="utf-8")
CHANGED = ORIGINAL.replace("¥99/月", "¥129/月").replace("10 个席位", "20 个席位")

FAKE_PAYLOAD = {
    "pricing_tiers": [
        {"name": "Free", "amount": None, "currency": None, "billing_period": None,
         "key_limits": ["5GB 存储", "1 个席位"]},
        {"name": "Pro", "amount": 99.0, "currency": "CNY", "billing_period": "monthly",
         "key_limits": ["100GB 存储", "10 个席位"]},
    ],
    "feature_items": [{"name": "AI 助手", "description": "自动生成回复", "category": "AI"}],
    "target_user_note": "中小型团队与个人开发者",
}


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args):  # 静默
        pass


def _serve(directory: Path) -> tuple[ThreadingHTTPServer, int]:
    import functools

    server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Quiet, directory=str(directory)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def _patch_litellm():
    """让 LLMExtractionStrategy 不联网：直接返回定价 JSON。"""
    import litellm

    class _Msg:
        def __init__(self, content):
            self.content = content

    class _Choice:
        def __init__(self, content):
            self.message = _Msg(content)

    class _Usage:
        completion_tokens = 10
        prompt_tokens = 100
        total_tokens = 110
        completion_tokens_details = None
        prompt_tokens_details = None

    class _Resp:
        def __init__(self, content):
            self.choices = [_Choice(content)]
            self.usage = _Usage()

    def fake_completion(*args, **kwargs):
        """按 prompt 中出现的价格返回对应套餐价，模拟页面变更后的真实抽取。"""
        messages = kwargs.get("messages") or []
        prompt = messages[0].get("content", "") if messages else ""
        amount = 129.0 if "129" in prompt else 99.0
        tiers = [dict(t) for t in FAKE_PAYLOAD["pricing_tiers"]]
        tiers[1] = {**tiers[1], "amount": amount}
        payload = {**FAKE_PAYLOAD, "pricing_tiers": tiers}
        return _Resp(json.dumps([{"content": payload, "error": False}], ensure_ascii=False))

    async def fake_acompletion(*args, **kwargs):
        return _Resp("Pro 套餐月费由 99 元上调至 129 元，涨幅约 30%，对价格敏感的中小团队影响较大。")

    litellm.completion = fake_completion
    litellm.acompletion = fake_acompletion
    return fake_completion


async def main() -> None:
    from datetime import date, timedelta

    from competitor_intel.core.config import Settings, Target
    from competitor_intel.core.diff import diff_snapshots
    from competitor_intel.core.graph import run_pipeline
    from competitor_intel.core.models import CompetitorSnapshot, FeatureItem, PricingTier
    from competitor_intel.core.reporter import render_report
    from competitor_intel.core.store import (
        init_db,
        latest_snapshot,
        save_snapshot,
        snapshots_since,
    )

    WORK.mkdir(parents=True, exist_ok=True)
    SITE.mkdir(parents=True, exist_ok=True)
    page = SITE / "sample_pricing.html"
    page.write_text(ORIGINAL, encoding="utf-8")
    server, port = _serve(SITE)
    config_path = WORK / "targets.yaml"
    url = f"http://127.0.0.1:{port}/sample_pricing.html"
    config_path.write_text(
        f"""defaults:
  request_interval_s: 0
  respect_robots: false
  timeout_s: 15
  cache_mode: bypass
targets:
  - competitor: acme
    pages:
      - type: pricing
        url: {url}
        main_selector: "main"
        pricing_selector: ".pricing-table"
""",
        encoding="utf-8",
    )
    settings = Settings(
        llm_api_key="fake",
        db_path=str(WORK / "snapshots.db"),
        report_dir=str(WORK / "reports"),
        browser_channel=os.getenv("CRAWLER_BROWSER_CHANNEL", "chromium"),
    )

    # 清掉上次冒烟残留（端口会变，残留行会让报告出现重复 URL）
    for suffix in ("", "-wal", "-shm"):
        stale = Path(str(WORK / "snapshots.db") + suffix)
        if stale.exists():
            stale.unlink()

    # 注入昨日基线，使当天运行的 diff 有比较对象
    await init_db(settings.db_path)
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    await save_snapshot(
        settings.db_path,
        Target(
            competitor="acme",
            page_type="pricing",
            url=url,
            main_selector="main",
            pricing_selector=".pricing-table",
        ),
        CompetitorSnapshot(
            competitor_name="acme",
            page_url=url,
            snapshot_date=yesterday,
            page_type="pricing",
            pricing_tiers=[
                PricingTier(
                    name="Free", amount=None, currency=None,
                    key_limits=["5GB 存储", "1 个席位"],
                ),
                PricingTier(
                    name="Pro", amount=99.0, currency="CNY", billing_period="monthly",
                    key_limits=["100GB 存储", "10 个席位"],
                ),
            ],
            feature_items=[
                FeatureItem(name="AI 助手", description="自动生成回复", category="AI")
            ],
            target_user_note="中小型团队与个人开发者",
        ),
        content_hash="seed",
        status="extracted",
    )
    _patch_litellm()

    print("== 第 1 次运行（全量抽取，价格与昨日相同 -> 无变更）")
    state1 = await run_pipeline(str(config_path), settings=settings)
    for r in state1.get("results", []):
        print(f"   {r.target.competitor} {r.status} fallback={r.used_fallback}")
    assert state1["results"] and state1["results"][0].status == "extracted", state1.get("errors")
    assert state1["diffs"] == [], f"价格未变不应产生变更: {state1['diffs']}"

    print("== 第 2 次运行（指纹未变，应 skipped）")
    state2 = await run_pipeline(str(config_path), settings=settings)
    assert state2["results"][0].status == "skipped", state2["results"][0].status

    print("== 修改页面后第 3 次运行（应重新抽取并识别涨价）")
    page.write_text(CHANGED, encoding="utf-8")
    state3 = await run_pipeline(str(config_path), settings=settings)
    assert state3["results"][0].status == "extracted"
    assert len(state3["diffs"]) == 1 and state3["diffs"][0].competitor == "acme", state3["diffs"]
    assert state3["analyses"], "变更解读缺失"
    assert "129" in state3["analyses"][0]

    print("== 校验报告内容")
    report = Path(state3["report_path"]).read_text(encoding="utf-8")
    assert "涨价" in report or "129" in report, "报告未体现本次变更"
    print(f"   报告：{state3['report_path']}")

    print("== 从快照重算 diff 并生成报告")
    snaps = await snapshots_since(settings.db_path, "2000-01-01")
    latest = {}
    for s in snaps:
        latest[(s.competitor_name, s.page_url)] = s
    rows = list(latest.values())
    diffs = []
    for s in rows:
        baseline = await latest_snapshot(
            settings.db_path, s.competitor_name, s.page_url, before=s.snapshot_date
        )
        if baseline:
            d = diff_snapshots(baseline, s)
            if d.changed:
                diffs.append(d)
    out = render_report(rows, diffs, out=WORK / "reports" / "smoke.md")
    print(f"   报告：{out}")
    print(out.read_text(encoding="utf-8")[:600])
    server.shutdown()
    print("SMOKE OK")


if __name__ == "__main__":
    asyncio.run(main())
