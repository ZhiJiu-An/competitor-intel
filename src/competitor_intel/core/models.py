"""Pydantic 领域模型。

Schema 是抽取指令与校验标准的双重载体（TDD 5.1）。
本模块最先写、最该反复打磨：字段粒度决定 diff 的可读性。
"""


from pydantic import BaseModel, Field, StrictFloat


class PriceComponent(BaseModel):
    """按量计费的一个价格维度（token / 调用次数 / 席位等）。

    订阅制页面留空；按量计费页面用它承载多维价格，
    这样「一个模型一行」仍然保持数值型，diff 可精确到字段。
    """

    unit: str = Field(description="计费单位，如 1M tokens / 席位每月 / 千次调用")
    amount: StrictFloat = Field(description="该维度的数值价格")
    currency: str = Field(description="ISO 币种代码，如 CNY / USD")
    condition: str | None = Field(
        None, description="适用条件，如 缓存未命中·高峰 / 年付 / 超出配额后"
    )


class PricingTier(BaseModel):
    name: str = Field(description="套餐名称，原样保留，如 Free / Pro / Enterprise")
    # 严格数值：拒绝 "99" 这类字符串，防止 LLM 输出被静默强转（int 仍接受）
    amount: StrictFloat | None = Field(None, description="数值价格；免费或面议填 null")
    currency: str | None = Field(None, description="ISO 币种代码，如 CNY / USD")
    billing_period: str | None = Field(
        None, description="monthly / yearly / one_time / custom"
    )
    key_limits: list[str] = Field(
        default_factory=list, description="核心配额，如 5GB 存储、3 个席位"
    )
    pricing_model: str | None = Field(
        None,
        description="subscription（订阅制）/ usage_based（按量计费）/ one_time / custom",
    )
    components: list[PriceComponent] = Field(
        default_factory=list,
        description="按量计费的多维价格；订阅制套餐保持为空",
    )


class FeatureItem(BaseModel):
    name: str = Field(description="功能点名称，简短名词短语")
    description: str = Field(description="一句话说明该功能做什么")
    category: str | None = Field(None, description="功能归类，如 AI / 协作 / 安全")


class CompetitorSnapshot(BaseModel):
    competitor_name: str
    page_url: str
    snapshot_date: str = Field(description="ISO 日期 YYYY-MM-DD")
    page_type: str = Field(description="pricing / changelog")
    pricing_tiers: list[PricingTier] = Field(default_factory=list)
    feature_items: list[FeatureItem] = Field(default_factory=list)
    target_user_note: str | None = Field(
        None, description="目标用户描述，从页面原文归纳"
    )
