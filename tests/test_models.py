"""契约测试：合法/非法样例断言通过与否（TDD 9.1）。"""

import pytest
from pydantic import ValidationError

from competitor_intel.models import CompetitorSnapshot, FeatureItem, PricingTier


def test_minimal_snapshot_passes():
    snap = CompetitorSnapshot(
        competitor_name="acme",
        page_url="https://acme.example/pricing",
        snapshot_date="2026-11-01",
        page_type="pricing",
        pricing_tiers=[PricingTier(name="Free")],
        feature_items=[FeatureItem(name="AI 助手", description="自动生成回复")],
    )
    assert snap.pricing_tiers[0].amount is None  # 免费/面议显式 null，不臆造 0


def test_snapshot_requires_core_fields():
    with pytest.raises(ValidationError):
        CompetitorSnapshot(competitor_name="acme")  # type: ignore[call-arg]


def test_pricing_tier_amount_must_be_numeric():
    with pytest.raises(ValidationError):
        PricingTier(name="Pro", amount="99")  # type: ignore[arg-type]
