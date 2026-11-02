"""变更解读：仅基于 diff 生成分析，不传全文（TDD 6.4 分析层成本控制）。

对外契约：analyze_diff(diff, context, settings) -> str（副作用：LLM 调用）。
模型调用失败时抛异常，由编排层降级为占位文本，保证报告不缺席。
"""

import litellm

from .config import Settings
from .diff import DiffResult

_PROMPT = """你是竞品情报分析师。下面是一次竞品页面的字段级变更（JSON），
请用 2-4 句中文给出变更解读，聚焦：价格策略变化、功能增减、对目标用户的影响。
只基于给定变更，不要臆测未出现的字段。"""


async def analyze_diff(
    diff: DiffResult, context: str | None = None, *, settings: Settings | None = None
) -> str:
    """对字段级差异生成中文变更解读（副作用：LLM 调用）。"""
    settings = settings or Settings.from_env()
    if not settings.llm_api_key:
        raise RuntimeError("缺少 LLM_API_KEY，无法生成变更解读")

    model = settings.llm_model if "/" in settings.llm_model else f"openai/{settings.llm_model}"
    user = diff.model_dump_json()
    if context:
        user = f"背景：{context}\n\n变更：{user}"

    resp = await litellm.acompletion(
        model=model,
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        messages=[
            {"role": "system", "content": _PROMPT},
            {"role": "user", "content": user},
        ],
        temperature=0.2,
    )
    return resp.choices[0].message.content.strip()
