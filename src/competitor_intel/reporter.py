"""渲染 Markdown 对比表与变更摘要（TDD 4.2）。

对外契约：render_report(rows, diffs, analyses, *, out=None, out_dir) -> Path（副作用：写文件）。
Git 友好、便于 diff 与代码评审；渲染成本为零。
"""

from datetime import date
from pathlib import Path

from .diff import DiffResult
from .models import CompetitorSnapshot, PricingTier


def _price_cell(t: PricingTier) -> str:
    """按量计费把多个维度压进一行，订阅制保持单一价格。"""
    if t.components:
        parts = []
        for c in t.components:
            cond = f"{c.condition} " if c.condition else ""
            parts.append(f"{cond}{c.amount} {c.currency}/{c.unit}")
        return "；".join(parts)
    return f"{t.amount} {t.currency or ''}".strip() if t.amount is not None else "免费/面议"


def _pricing_table(rows: list[CompetitorSnapshot]) -> list[str]:
    lines = [
        "| 竞品 | 页面 | 套餐 | 价格 | 周期 | 关键限制 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for snap in rows:
        if not snap.pricing_tiers:
            lines.append(
                f"| {snap.competitor_name} | {snap.page_url} | （未抽取到套餐） | | | |"
            )
            continue
        for t in snap.pricing_tiers:
            limits = "; ".join(t.key_limits)
            period = t.billing_period or t.pricing_model or ""
            lines.append(
                f"| {snap.competitor_name} | {snap.page_url} | {t.name} "
                f"| {_price_cell(t)} | {period} | {limits} |"
            )
    return lines


def render_report(
    rows: list[CompetitorSnapshot],
    diffs: list[DiffResult],
    analyses: list[str] | None = None,
    *,
    out: str | Path | None = None,
    out_dir: str | Path = "data/reports",
) -> Path:
    """生成 Markdown 对比报告并返回文件路径（副作用：写文件）。

    analyses 与 diffs 一一对应；缺省或缺位时用占位文本，保证报告不缺席。
    """
    today = date.today().isoformat()
    analyses = analyses or []
    changed = [d for d in diffs if d.changed]

    lines: list[str] = [
        f"# 竞品情报报告（{today}）",
        "",
        "## 概要",
        "",
        f"- 监控页面快照：{len(rows)} 个",
        f"- 发生变更：{len(changed)} 个页面",
        "",
        "## 变更明细",
        "",
    ]

    if not changed:
        lines += ["本期无字段级变更。", ""]
    for i, d in enumerate(changed):
        lines += [
            f"### {d.competitor} — {d.page_url}",
            "",
            f"基线 {d.baseline_date} → 当前 {d.current_date}",
            "",
            "| 字段 | 旧值 | 新值 |",
            "| --- | --- | --- |",
        ]
        for c in d.changes:
            old = c.old if c.old is not None else "（无）"
            new = c.new if c.new is not None else "（已移除）"
            lines.append(f"| {c.field} | {old} | {new} |")
        lines += ["", "**变更解读**：" + (analyses[i] if i < len(analyses) else "（解读暂缺）"), ""]

    lines += ["## 当前定价快照", ""] + _pricing_table(rows) + [""]

    lines += [
        "## 指标说明",
        "",
        "| 字段 | 中文 | 含义与计算方式 |",
        "| --- | --- | --- |",
        "| skip_rate | 跳过率 | 指纹未变、直接跳过不重抓的页数 ÷ 处理页数 |",
        "| fallback_rate | 兜底率 | LLM 抽取失败后改用规则解析的页数 ÷ 处理页数 |",
        "| extraction_error_rate | 抽取失败率 | 抽取失败页数 ÷（抽取成功＋抽取失败） |",
        "",
    ]

    if out is not None:
        path = Path(out)
    else:
        path = Path(out_dir) / f"{today}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
