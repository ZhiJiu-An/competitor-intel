"""字段级 diff（TDD 4.2 / 7.1）。

对外契约：diff_snapshots(old, new) -> DiffResult（纯函数）。
字段粒度决定 diff 的可读性：价格拆 amount/currency，功能项拆 name/category。
"""

from pydantic import BaseModel, Field

from .models import CompetitorSnapshot, FeatureItem, PriceComponent, PricingTier


class FieldChange(BaseModel):
    field: str = Field(description="点分路径，如 pricing_tiers[Pro].amount")
    old: str | None = None
    new: str | None = None


class DiffResult(BaseModel):
    competitor: str
    page_url: str
    baseline_date: str
    current_date: str
    changes: list[FieldChange] = Field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.changes)


def _fmt(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _component_label(c: PriceComponent) -> str:
    """components 的匹配键：同一单位 + 同一条件视为同一个价格维度。"""
    return f"{c.unit}·{c.condition}" if c.condition else c.unit


def _tier_repr(t: PricingTier) -> str:
    if t.components:
        price = "；".join(
            f"{_component_label(c)} {c.amount} {c.currency}" for c in t.components
        )
    elif t.amount is not None:
        price = f"{t.amount} {t.currency or ''}/{t.billing_period or ''}".strip()
    else:
        price = "免费/面议"
    limits = f"（{'; '.join(t.key_limits)}）" if t.key_limits else ""
    return f"{t.name}: {price}{limits}"


def _feature_repr(f: FeatureItem) -> str:
    cat = f"[{f.category}] " if f.category else ""
    return f"{cat}{f.name}: {f.description}"


def _diff_tiers(old: list[PricingTier], new: list[PricingTier]) -> list[FieldChange]:
    changes: list[FieldChange] = []
    old_by_name = {t.name: t for t in old}
    new_by_name = {t.name: t for t in new}

    for name in old_by_name.keys() - new_by_name.keys():
        changes.append(
            FieldChange(
                field=f"pricing_tiers[{name}]",
                old=_tier_repr(old_by_name[name]),
                new=None,
            )
        )
    for name in new_by_name.keys() - old_by_name.keys():
        changes.append(
            FieldChange(
                field=f"pricing_tiers[{name}]",
                old=None,
                new=_tier_repr(new_by_name[name]),
            )
        )
    for name in old_by_name.keys() & new_by_name.keys():
        o, n = old_by_name[name], new_by_name[name]
        for attr in ("amount", "currency", "billing_period"):
            ov, nv = getattr(o, attr), getattr(n, attr)
            if ov != nv:
                changes.append(
                    FieldChange(
                        field=f"pricing_tiers[{name}].{attr}",
                        old=_fmt(ov),
                        new=_fmt(nv),
                    )
                )
        if o.key_limits != n.key_limits:
            changes.append(
                FieldChange(
                    field=f"pricing_tiers[{name}].key_limits",
                    old="; ".join(o.key_limits) or None,
                    new="; ".join(n.key_limits) or None,
                )
            )
        if o.pricing_model != n.pricing_model:
            changes.append(
                FieldChange(
                    field=f"pricing_tiers[{name}].pricing_model",
                    old=_fmt(o.pricing_model),
                    new=_fmt(n.pricing_model),
                )
            )
        changes += _diff_components(name, o.components, n.components)
    return changes


def _diff_components(
    tier_name: str, old: list[PriceComponent], new: list[PriceComponent]
) -> list[FieldChange]:
    """按（单位, 条件）匹配价格维度，逐维比较数值，避免退化为文本比对。"""
    changes: list[FieldChange] = []
    old_by_key = {_component_label(c): c for c in old}
    new_by_key = {_component_label(c): c for c in new}

    for key in old_by_key.keys() - new_by_key.keys():
        changes.append(
            FieldChange(
                field=f"pricing_tiers[{tier_name}].components[{key}]",
                old=_fmt(old_by_key[key].amount),
                new=None,
            )
        )
    for key in new_by_key.keys() - old_by_key.keys():
        changes.append(
            FieldChange(
                field=f"pricing_tiers[{tier_name}].components[{key}]",
                old=None,
                new=_fmt(new_by_key[key].amount),
            )
        )
    for key in old_by_key.keys() & new_by_key.keys():
        o, n = old_by_key[key], new_by_key[key]
        for attr in ("amount", "currency"):
            ov, nv = getattr(o, attr), getattr(n, attr)
            if ov != nv:
                changes.append(
                    FieldChange(
                        field=f"pricing_tiers[{tier_name}].components[{key}].{attr}",
                        old=_fmt(ov),
                        new=_fmt(nv),
                    )
                )
    return changes


def _diff_features(old: list[FeatureItem], new: list[FeatureItem]) -> list[FieldChange]:
    changes: list[FieldChange] = []
    old_by_name = {f.name: f for f in old}
    new_by_name = {f.name: f for f in new}

    for name in old_by_name.keys() - new_by_name.keys():
        changes.append(
            FieldChange(
                field=f"feature_items[{name}]",
                old=_feature_repr(old_by_name[name]),
                new=None,
            )
        )
    for name in new_by_name.keys() - old_by_name.keys():
        changes.append(
            FieldChange(
                field=f"feature_items[{name}]",
                old=None,
                new=_feature_repr(new_by_name[name]),
            )
        )
    for name in old_by_name.keys() & new_by_name.keys():
        o, n = old_by_name[name], new_by_name[name]
        for attr in ("description", "category"):
            ov, nv = getattr(o, attr), getattr(n, attr)
            if ov != nv:
                changes.append(
                    FieldChange(
                        field=f"feature_items[{name}].{attr}",
                        old=_fmt(ov),
                        new=_fmt(nv),
                    )
                )
    return changes


def diff_snapshots(old: CompetitorSnapshot, new: CompetitorSnapshot) -> DiffResult:
    """字段级对比两次快照（纯函数）。"""
    if old.competitor_name != new.competitor_name or old.page_url != new.page_url:
        raise ValueError("diff 的两次快照必须属于同一竞品同一页面")

    changes = _diff_tiers(old.pricing_tiers, new.pricing_tiers)
    changes += _diff_features(old.feature_items, new.feature_items)

    if old.target_user_note != new.target_user_note:
        changes.append(
            FieldChange(
                field="target_user_note",
                old=old.target_user_note,
                new=new.target_user_note,
            )
        )

    return DiffResult(
        competitor=new.competitor_name,
        page_url=new.page_url,
        baseline_date=old.snapshot_date,
        current_date=new.snapshot_date,
        changes=changes,
    )
