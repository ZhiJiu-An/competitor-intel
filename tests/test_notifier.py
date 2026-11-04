"""飞书卡片构建测试（P1 推送增强）。

只验证卡片 JSON 的形状与内容，不发真实请求。
"""

import json
from types import SimpleNamespace

from competitor_intel.adapters.schedule import notifier
from competitor_intel.adapters.schedule.notifier import MAX_CHANGE_ROWS, _build_card, send_feishu

BASE = "http://127.0.0.1:8000"


def _count(node, tag: str) -> int:
    if isinstance(node, dict):
        return (1 if node.get("tag") == tag else 0) + sum(
            _count(value, tag) for value in node.values()
        )
    if isinstance(node, list):
        return sum(_count(item, tag) for item in node)
    return 0


def _run_result(changes: int = 2, *, report_path: str | None = "data/reports/2026-11-03.md") -> dict:
    return {
        "run_id": "r1",
        "status": "succeeded",
        "processed": 1,
        "extracted": 1,
        "skipped": 0,
        "blocked": 0,
        "extraction_error": 0,
        "fallback": 0,
        "errors": 0,
        "metrics": {
            "skip_rate": 0.25,
            "fallback_rate": 0.5,
            "extraction_error_rate": 0.1,
        },
        "report_path": report_path,
        "differences": [
            {
                "competitor": "acme",
                "page_url": "https://acme.example/pricing",
                "baseline_date": "2026-10-27",
                "current_date": "2026-11-03",
                "changes": [
                    {"field": "pricing_tiers[Pro].amount", "old": "99.0", "new": "129.0"}
                    for _ in range(changes)
                ],
            }
        ],
    }


def test_card_has_change_table_links_and_report_button():
    card = json.loads(json.dumps(_build_card(_run_result(), report_base_url=BASE)))

    # 表头 + 2 行数据
    assert _count(card, "column_set") == 3
    assert card["header"]["template"] == "green"

    text = json.dumps(card, ensure_ascii=False)
    assert "[acme](https://acme.example/pricing)" in text  # 竞品名可点击
    assert "套餐 Pro · 价格" in text  # 字段路径翻成人话
    assert "`pricing_tiers" not in text  # lark_md 不认反引号，别原样漏出去
    assert "99.0 → 129.0" in text
    # 比率用原始英文 key，中文解释放在报告里
    assert "skip_rate **25.0%**" in text
    assert "fallback_rate **50.0%**" in text
    assert "extraction_error_rate **10.0%**" in text

    actions = [e for e in card["elements"] if e.get("tag") == "action"]
    assert actions[0]["actions"][0]["url"] == f"{BASE}/reports/2026-11-03.md"


def test_card_without_changes_says_so():
    card = _build_card(_run_result(changes=0), report_base_url=BASE)
    assert _count(card, "column_set") == 0
    assert "本期无字段级变化" in json.dumps(card, ensure_ascii=False)


def test_card_truncates_long_change_list():
    card = _build_card(_run_result(changes=MAX_CHANGE_ROWS + 4), report_base_url=BASE)
    # 表头 1 行 + 最多 MAX_CHANGE_ROWS 行
    assert _count(card, "column_set") == 1 + MAX_CHANGE_ROWS
    assert "还有 4 条未展示" in json.dumps(card, ensure_ascii=False)


def test_card_omits_button_without_report_path_or_base_url():
    no_path = _build_card(_run_result(report_path=None), report_base_url=BASE)
    assert not [e for e in no_path["elements"] if e.get("tag") == "action"]

    no_base = _build_card(_run_result(), report_base_url=None)
    assert not [e for e in no_base["elements"] if e.get("tag") == "action"]


def test_send_feishu_without_webhook_is_noop():
    assert send_feishu(_run_result(), webhook="") is False


def _notifier_settings(**overrides) -> SimpleNamespace:
    values = {
        "feishu_webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/test",
        "feishu_keyword": "竞品分析",
        "report_base_url": BASE,
        "feishu_notify_mode": "changes",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _spy_send(monkeypatch) -> list[dict]:
    sent: list[dict] = []

    def _fake(run_result, **_kwargs):
        sent.append(run_result)
        return True

    monkeypatch.setattr(notifier, "send_feishu", _fake)
    return sent


def test_build_notifier_none_without_webhook():
    assert notifier.build_notifier(_notifier_settings(feishu_webhook="")) is None


def test_changes_mode_sends_only_when_differences_exist(monkeypatch):
    sent = _spy_send(monkeypatch)
    notify = notifier.build_notifier(_notifier_settings())

    notify({**_run_result(), "differences": []})
    assert sent == []

    with_changes = _run_result()
    notify(with_changes)
    assert sent == [with_changes]


def test_always_mode_sends_even_without_changes(monkeypatch):
    sent = _spy_send(monkeypatch)
    notify = notifier.build_notifier(_notifier_settings(feishu_notify_mode="always"))

    quiet = {**_run_result(), "differences": []}
    notify(quiet)
    assert sent == [quiet]
