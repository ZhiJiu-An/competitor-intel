"""飞书推送：webhook 卡片消息，run 结束后推送摘要（P1 推送增强）。

设计要点：
- 只做 webhook 机器人（不接企业应用 open-apis），凭证就一条 FEISHU_WEBHOOK；
- 任何网络/权限失败静默吞掉，绝不影响 run 主流程的状态与返回值；
- 推送内容用飞书 interactive card（摘要 + 关键指标 + 变更数），
  不需要卡片用纯 text 也行，但卡片视觉更干净；
- webhook 配了关键词校验时，自动在标题里加上关键词（默认"竞品分析"）。
"""

import json
from datetime import date

import structlog
import urllib.request

log = structlog.get_logger(__name__)

DEFAULT_KEYWORD = "竞品分析"

STATUS_EMOJI = {
    "succeeded": "✅",
    "partial": "⚠️",
    "failed": "❌",
}


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


def _build_card(run_result: dict, *, keyword: str = DEFAULT_KEYWORD) -> dict:
    """把 run 摘要渲染成飞书 interactive card。

    run_result 是 service.execute_run 的返回值：
      {run_id, status, processed, extracted, skipped, blocked,
       extraction_error, fallback, errors, metrics, report_path}
    """
    status = run_result.get("status", "unknown")
    emoji = STATUS_EMOJI.get(status, "🔔")
    today = date.today().isoformat()

    counts = run_result
    metrics = counts.get("metrics", {})

    lines = [
        f"{emoji} **{keyword} · {today}**",
        f"状态: **{status}**  |  处理 **{counts.get('processed', 0)}** 页  |  抽取成功 **{counts.get('extracted', 0)}**",
        f"跳过: {counts.get('skipped', 0)}  |  兜底: {counts.get('fallback', 0)}  |  抽取失败: {counts.get('extraction_error', 0)}  |  运行异常: {counts.get('errors', 0)}",
        "",
        f"skip_rate: **{metrics.get('skip_rate', 0.0):.2%}**  |  fallback_rate: **{metrics.get('fallback_rate', 0.0):.2%}**  |  extraction_error_rate: **{metrics.get('extraction_error_rate', 0.0):.2%}**",
    ]
    if counts.get("report_path"):
        lines.append(f"报告: `{counts['report_path']}`")
    if status == "failed" or counts.get("errors"):
        lines.append("⚠️ 有异常，请查看 run 记录或 review_queue。")

    content_markdown = "\n".join(lines)

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
        "elements": [
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": content_markdown},
            }
        ],
    }


def send_feishu(run_result: dict, *, webhook: str, keyword: str = DEFAULT_KEYWORD) -> bool:
    """发送飞书卡片，任何错误返回 False 并写 warning，**绝不抛异常**。

    返回 True 仅代表 HTTP 200 且飞书回包 code=0。
    """
    if not webhook:
        return False

    payload = {
        "msg_type": "interactive",
        "card": _build_card(run_result, keyword=keyword),
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
