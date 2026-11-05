"""飞书推送：webhook 卡片消息，run 结束后推送摘要（P1 推送增强）。

设计要点：
- 只做 webhook 机器人（不接企业应用 open-apis），凭证就一条 FEISHU_WEBHOOK；
- 任何网络/权限失败静默吞掉，绝不影响 run 主流程的状态与返回值；
- 推送内容用飞书 interactive card（摘要 + 关键指标 + 变更数），
  不需要卡片用纯 text 也行，但卡片视觉更干净；
- webhook 配了关键词校验时，自动在标题里加上关键词（默认"竞品分析"）。
"""

import json
import re
import urllib.request
from collections.abc import Callable
from datetime import date
from pathlib import Path
from urllib.parse import quote

import structlog

log = structlog.get_logger(__name__)

DEFAULT_KEYWORD = "竞品分析"
MAX_CHANGE_ROWS = 8  # 卡片只放摘要，完整明细看报告

STATUS_EMOJI = {
    "succeeded": "✅",
    "partial": "⚠️",
    "failed": "❌",
}

STATUS_LABEL = {
    "succeeded": "成功",
    "partial": "部分成功",
    "failed": "失败",
    "queued": "排队中",
    "running": "运行中",
}

# 字段路径是给程序看的，卡片里换成中文标签，避免满屏 pricing_tiers[...]
_TIER_ATTRS = {
    "amount": "价格",
    "currency": "货币",
    "billing_period": "计费周期",
    "pricing_model": "计费模式",
    "key_limits": "关键限制",
    "components": "计费项",
}
_FEATURE_ATTRS = {"name": "名称", "description": "描述", "category": "分类"}


def _field_label(field: str) -> str:
    """pricing_tiers[Pro].amount → 套餐 Pro · 价格；认不出就原样返回。"""
    match = re.fullmatch(r"pricing_tiers\[(.+?)\](?:\.(\w+))?", field)
    if match:
        tier, attr = match.groups()
        if not attr:
            return f"套餐 {tier}"
        return f"套餐 {tier} · {_TIER_ATTRS.get(attr, attr)}"

    match = re.fullmatch(r"feature_items\[(.+?)\](?:\.(\w+))?", field)
    if match:
        name, attr = match.groups()
        if not attr:
            return f"功能 {name}"
        return f"功能 {name} · {_FEATURE_ATTRS.get(attr, attr)}"

    return field.replace("_", " ").strip() or field


def _http_post_json(url: str, payload: dict, *, timeout: float = 8.0) -> tuple[int, str]:
    """无 requests 依赖的极简 JSON POST（urllib stdlib 就够用）。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "ignore")
            return resp.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "ignore") if exc.fp else ""
        return exc.code, raw
    except Exception as exc:
        raise RuntimeError(str(exc)) from exc


def _clip(value: object, limit: int = 36) -> str:
    if value is None or value == "":
        return "—"
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _grid(cells: list[tuple[str, int]]) -> dict:
    """一行多列：飞书卡片没有原生表格元素，用 column_set 拼出表格观感。"""
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "horizontal_spacing": "default",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": weight,
                "elements": [
                    {"tag": "div", "text": {"tag": "lark_md", "content": content}}
                ],
            }
            for content, weight in cells
        ],
    }


def _change_rows(differences: list[dict]) -> tuple[list[dict], int]:
    """摊平成「竞品 / 字段 / 旧 → 新」三列，返回 (行元素, 被省略的行数)。"""
    rows: list[dict] = []
    total = 0
    for diff in differences:
        competitor = diff.get("competitor") or "unknown"
        page_url = diff.get("page_url") or ""
        label = f"[{competitor}]({page_url})" if page_url else competitor
        for change in diff.get("changes") or []:
            total += 1
            if len(rows) >= MAX_CHANGE_ROWS:
                continue
            rows.append(
                _grid(
                    [
                        (label, 2),
                        (_field_label(str(change.get("field", ""))), 4),
                        (f"{_clip(change.get('old'))} → {_clip(change.get('new'))}", 4),
                    ]
                )
            )
    return rows, max(total - len(rows), 0)


def _report_url(
    report_path: str | None,
    base_url: str | None,
    report_root: str | Path | None = None,
) -> str | None:
    if not report_path or not base_url:
        return None
    path = Path(str(report_path))
    # adhoc 报告在 reports/adhoc/ 下，只取文件名会让按钮 404
    rel = Path(path.name)
    if report_root:
        root = Path(report_root)
        try:
            rel = path.relative_to(root)
        except ValueError:
            try:
                rel = path.resolve().relative_to(root.resolve())
            except ValueError:
                pass
    return f"{base_url.rstrip('/')}/reports/{quote(rel.as_posix())}"


def _build_card(
    run_result: dict,
    *,
    keyword: str = DEFAULT_KEYWORD,
    report_base_url: str | None = None,
    report_root: str | Path | None = None,
) -> dict:
    """把 run 摘要渲染成飞书 interactive card。

    run_result 是 service.execute_run 的返回值：
      {run_id, status, processed, extracted, skipped, blocked,
       extraction_error, fallback, errors, metrics, report_path, differences}

    卡片包含三块：指标摘要 / 变更明细表 / 完整报告按钮。
    明细表用 column_set 拼三列，竞品名链到被监控的原页面。
    """
    status = run_result.get("status", "unknown")
    emoji = STATUS_EMOJI.get(status, "🔔")
    today = date.today().isoformat()

    counts = run_result
    metrics = counts.get("metrics", {})

    rates = (
        f"skip_rate **{metrics.get('skip_rate', 0.0):.1%}**　"
        f"fallback_rate **{metrics.get('fallback_rate', 0.0):.1%}**　"
        f"extraction_error_rate **{metrics.get('extraction_error_rate', 0.0):.1%}**"
    )

    lines = [
        f"{emoji} **{keyword} · {today}**",
        f"状态：**{STATUS_LABEL.get(status, status)}**　处理 **{counts.get('processed', 0)}** 页　抽取成功 **{counts.get('extracted', 0)}**",
        f"跳过 {counts.get('skipped', 0)}　兜底 {counts.get('fallback', 0)}　抽取失败 {counts.get('extraction_error', 0)}　运行异常 {counts.get('errors', 0)}",
        "",
        f"比率：{rates}",
    ]
    if status == "failed" or counts.get("errors"):
        lines.append("⚠️ 有异常，请查看 run 记录或 review_queue。")

    elements: list[dict] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}},
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "lark_md", "content": "**变更明细**"}},
    ]

    rows, hidden = _change_rows(run_result.get("differences") or [])
    if rows:
        elements.append(_grid([("**竞品**", 2), ("**字段**", 4), ("**旧 → 新**", 4)]))
        elements.extend(rows)
        if hidden:
            elements.append(
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"还有 {hidden} 条未展示，见完整报告",
                    },
                }
            )
    else:
        elements.append(
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": "本期无字段级变化"},
            }
        )

    url = _report_url(run_result.get("report_path"), report_base_url, report_root)
    if url:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "查看完整报告"},
                        "type": "default",
                        "url": url,
                    }
                ],
            }
        )

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"{emoji} {keyword} {today}"},
            "template": {
                "succeeded": "green",
                "partial": "orange",
                "failed": "red",
            }.get(status, "blue"),
        },
        "elements": elements,
    }


def send_feishu(
    run_result: dict,
    *,
    webhook: str,
    keyword: str = DEFAULT_KEYWORD,
    report_base_url: str | None = None,
    report_root: str | Path | None = None,
) -> bool:
    """发送飞书卡片，任何错误返回 False 并写 warning，**绝不抛异常**。

    返回 True 仅代表 HTTP 200 且飞书回包 code=0。
    """
    if not webhook:
        return False

    payload = {
        "msg_type": "interactive",
        "card": _build_card(
            run_result,
            keyword=keyword,
            report_base_url=report_base_url,
            report_root=report_root,
        ),
    }

    try:
        status, raw = _http_post_json(webhook, payload)
    except Exception as exc:
        log.warning("feishu.send_failed", error=str(exc))
        return False

    if status != 200:
        log.warning("feishu.http_error", status=status, body=raw[:300])
        return False

    try:
        body = json.loads(raw)
    except Exception:
        log.warning("feishu.bad_json", body=raw[:200])
        return False

    if body.get("code", -1) != 0:
        log.warning("feishu.api_error", code=body.get("code"), msg=body.get("msg"))
        return False

    log.info("feishu.sent", status=run_result.get("status"))
    return True


def build_notifier(settings) -> Callable[[dict], None] | None:
    """把 Settings 绑成 core.orchestrator 需要的推送钩子；未配置则返回 None。

    FEISHU_NOTIFY_MODE 决定推送频率：
    - changes（默认）：无变化且 run 正常才静音，避免每天刷屏；
      failed / partial 仍然推送，故障不能被静音；
    - always：每次 run 都推，适合想每天看一次健康度的人。
    """
    if not settings.feishu_webhook:
        return None

    mode = getattr(settings, "feishu_notify_mode", "changes")

    def _send(run_result: dict) -> None:
        # 无变化且一切正常才静音；采集失败/部分失败属于「需要人看」，必须推
        quiet = not run_result.get("differences") and run_result.get("status") == "succeeded"
        if mode == "changes" and quiet:
            log.info("feishu.skipped", reason="no_changes", run_id=run_result.get("run_id"))
            return
        send_feishu(
            run_result,
            webhook=settings.feishu_webhook,
            keyword=settings.feishu_keyword,
            report_base_url=settings.report_base_url,
            report_root=settings.report_dir,
        )

    return _send
