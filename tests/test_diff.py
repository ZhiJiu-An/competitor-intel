"""字段级 diff 单测（TDD 9.1）。"""

import pytest

from competitor_intel.diff import diff_snapshots
from competitor_intel.models import (
    CompetitorSnapshot,
    FeatureItem,
    PriceComponent,
    PricingTier,
)


def _snap(
    date: str,
    tiers: list[PricingTier] | None = None,
    features: list[FeatureItem] | None = None,
    note: str | None = None,
) -> CompetitorSnapshot:
    return CompetitorSnapshot(
        competitor_name="acme",
        page_url="https://acme.example/pricing",
        snapshot_date=date,
        page_type="pricing",
        pricing_tiers=tiers or [],
        feature_items=features or [],
        target_user_note=note,
    )


def test_price_change_detected():
    old = _snap("2026-10-25", [PricingTier(name="Pro", amount=99.0, currency="CNY")])
    new = _snap("2026-11-01", [PricingTier(name="Pro", amount=129.0, currency="CNY")])
    diff = diff_snapshots(old, new)
    assert diff.changed
    assert diff.changes[0].field == "pricing_tiers[Pro].amount"
    assert diff.changes[0].old == "99.0"
    assert diff.changes[0].new == "129.0"


def test_no_change():
    tiers = [PricingTier(name="Pro", amount=99.0, currency="CNY", key_limits=["10 席位"])]
    diff = diff_snapshots(_snap("2026-10-25", tiers), _snap("2026-11-01", tiers))
    assert not diff.changed


def test_tier_added_and_removed():
    old = _snap("2026-10-25", [PricingTier(name="Free"), PricingTier(name="Pro", amount=99.0)])
    new = _snap("2026-11-01", [PricingTier(name="Pro", amount=99.0), PricingTier(name="Team", amount=299.0)])
    diff = diff_snapshots(old, new)
    fields = {c.field for c in diff.changes}
    assert "pricing_tiers[Free]" in fields
    assert "pricing_tiers[Team]" in fields


def test_key_limits_change():
    old = _snap("2026-10-25", [PricingTier(name="Pro", key_limits=["10 席位"])])
    new = _snap("2026-11-01", [PricingTier(name="Pro", key_limits=["20 席位"])])
    diff = diff_snapshots(old, new)
    assert diff.changes[0].field == "pricing_tiers[Pro].key_limits"


def test_feature_and_note_changes():
    old = _snap(
        "2026-10-25",
        features=[FeatureItem(name="AI 助手", description="自动生成回复", category="AI")],
        note="中小团队",
    )
    new = _snap(
        "2026-11-01",
        features=[
            FeatureItem(name="AI 助手", description="自动生成回复与摘要", category="AI"),
            FeatureItem(name="SSO", description="单点登录", category="安全"),
        ],
        note="中大型团队",
    )
    diff = diff_snapshots(old, new)
    fields = {c.field for c in diff.changes}
    assert "feature_items[AI 助手].description" in fields
    assert "feature_items[SSO]" in fields
    assert "target_user_note" in fields


def test_component_price_change_is_field_level():
    """按量计费：只改一个维度，应精确报出该维度数值，而非整串文本变化。"""

    def tier(peak: float) -> PricingTier:
        return PricingTier(
            name="deepseek-flash",
            pricing_model="usage_based",
            components=[
                PriceComponent(
                    unit="1M tokens", amount=peak, currency="USD", condition="输入·高峰"
                ),
                PriceComponent(
                    unit="1M tokens", amount=0.15, currency="USD", condition="输入·错峰"
                ),
            ],
        )

    diff = diff_snapshots(_snap("2026-10-25", [tier(0.3)]), _snap("2026-11-01", [tier(0.35)]))
    assert len(diff.changes) == 1
    assert diff.changes[0].field == "pricing_tiers[deepseek-flash].components[1M tokens·输入·高峰].amount"
    assert diff.changes[0].old == "0.3"
    assert diff.changes[0].new == "0.35"


def test_component_added_and_pricing_model_change():
    old = _snap("2026-10-25", [PricingTier(name="Pro", amount=99.0, currency="CNY")])
    new = _snap(
        "2026-11-01",
        [
            PricingTier(
                name="Pro",
                amount=99.0,
                currency="CNY",
                pricing_model="subscription",
                components=[
                    PriceComponent(unit="席位/月", amount=10.0, currency="CNY", condition="增购")
                ],
            )
        ],
    )
    fields = {c.field for c in diff_snapshots(old, new).changes}
    assert "pricing_tiers[Pro].pricing_model" in fields
    assert "pricing_tiers[Pro].components[席位/月·增购]" in fields


def test_cross_competitor_rejected():
    other = _snap("2026-11-01").model_copy(update={"competitor_name": "beta"})
    with pytest.raises(ValueError):
        diff_snapshots(_snap("2026-10-25"), other)
