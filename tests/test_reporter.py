"""报告渲染测试（TDD 4.2）。"""

from competitor_intel.diff import diff_snapshots
from competitor_intel.models import CompetitorSnapshot, PriceComponent, PricingTier
from competitor_intel.reporter import render_report


def _snap(date: str, amount: float) -> CompetitorSnapshot:
    return CompetitorSnapshot(
        competitor_name="acme",
        page_url="https://acme.example/pricing",
        snapshot_date=date,
        page_type="pricing",
        pricing_tiers=[PricingTier(name="Pro", amount=amount, currency="CNY")],
    )


def test_render_report_with_changes(tmp_path):
    old, new = _snap("2026-10-25", 99.0), _snap("2026-11-01", 129.0)
    diff = diff_snapshots(old, new)
    assert diff.changed

    path = render_report([new], [diff], ["Pro 套餐涨价 30 元。"], out_dir=tmp_path)
    text = path.read_text(encoding="utf-8")

    assert path.parent == tmp_path
    assert "竞品情报报告" in text
    assert "pricing_tiers[Pro].amount" in text
    assert "99.0" in text and "129.0" in text
    assert "Pro 套餐涨价 30 元。" in text
    # 当前定价快照表
    assert "| acme | https://acme.example/pricing | Pro | 129.0 CNY |" in text


def test_render_usage_based_tier_as_single_row(tmp_path):
    """按量计费的多维价格压进一行，不再出现每个单元格一行。"""
    snap = CompetitorSnapshot(
        competitor_name="deepseek",
        page_url="https://api-docs.deepseek.com/quick_start/pricing",
        snapshot_date="2026-11-02",
        page_type="pricing",
        pricing_tiers=[
            PricingTier(
                name="deepseek-flash",
                pricing_model="usage_based",
                components=[
                    PriceComponent(
                        unit="1M tokens", amount=0.3, currency="USD", condition="输入·高峰"
                    ),
                    PriceComponent(
                        unit="1M tokens", amount=0.15, currency="USD", condition="输入·错峰"
                    ),
                ],
            )
        ],
    )
    path = render_report([snap], [], out=tmp_path / "usage.md")
    text = path.read_text(encoding="utf-8")

    row = next(line for line in text.splitlines() if "deepseek-flash" in line)
    assert "输入·高峰 0.3 USD/1M tokens" in row
    assert "输入·错峰 0.15 USD/1M tokens" in row
    assert text.count("| deepseek-flash |") == 1


def test_render_report_no_changes(tmp_path):
    snap = _snap("2026-11-01", 99.0)
    path = render_report([snap], [], out=tmp_path / "latest.md")
    text = path.read_text(encoding="utf-8")

    assert path.name == "latest.md"
    assert "本期无字段级变更" in text
    assert "（解读暂缺）" not in text  # 无变更时不出现解读占位
