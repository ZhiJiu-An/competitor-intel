# 竞品情报系统 · MVP 产品文档

| 项 | 值 |
| --- | --- |
| 版本 | v0.1（MVP） |
| 日期 | 2026-11-01 |
| 依据 | 《竞品情报系统 · 产品技术设计文档》v1.0（已评审 · 可实施） |
| 技术主干 | DeepSeek 主干 + Qianwen 出口 |
| 交付周期 | 4 周（W1–W4） |

---

## 1. 背景与问题

电商 / 3C 品类的竞品定价与功能变更频繁，人工盯梢成本高、易遗漏；而通用的"爬虫 + AI 总结"方案存在三个结构性缺陷：

1. **输出无契约** —— 自由文本不可校验、不可比对、不可回溯；
2. **错误静默** —— 抽取失败被丢弃或错误填充，下游定价决策被污染；
3. **成本不可控** —— 每次运行全量调 LLM，token 消耗随竞品数线性增长。

## 2. MVP 目标

> **一句话：交付一条"采集 → 结构化抽取 → 增量 diff → Markdown 报告"的可审计竞品情报管道，用 4 周验证 Schema 契约 + 业务级增量在真实竞品页面上的可靠性。**

MVP 的量化目标：

- 支持 **3 个竞品**、**pricing + changelog** 两类公开页面；
- 周频运行，**指纹未变的页面零 LLM 消耗**（目标 skip_rate > 60%）；
- 所有产出受 **Pydantic Schema 强校验**，失败 100% 入库可见、进人工复核队列；
- 输出 **Git 友好的 Markdown 对比报告**，可直接挂仓库做代码评审。

## 3. 目标用户（MVP 阶段）

| 优先级 | 用户 | 场景 |
| --- | --- | --- |
| 主要 | 产品 / 市场分析 | 每周查看竞品定价与功能变更，作为定价策略与路线图讨论的输入 |
| 次要 | 销售 / 售前 | 快速获取竞品套餐限制与目标客群描述，用于话术准备 |
| 工程 | 数据 / 平台团队 | 作为可复用的采集抽取底座（MVP 不承诺对外复用接口） |

## 4. 范围边界

| 本版本纳入（In Scope） | 明确排除（Out of Scope） |
| --- | --- |
| 公开可访问的竞品定价页、changelog 页 | 登录态、付费墙、需授权的内容 |
| 结构化抽取：套餐、价格、功能项、目标用户 | 非结构化自由文本的语义摘要 |
| 内容指纹增量 + 字段级 diff | 全站爬取、站内链接递归发现 |
| 基于 diff 的变更解读 + Markdown 报告 | 实时推送（飞书/邮件 webhook，延至 v0.2） |
| 单条 LangGraph 图、失败兜底、断点恢复 | 多 Agent 协作、可视化前端 |
| CLI 单次运行 / 定时运行 | SaaS 多租户、权限体系 |

**范围纪律：上表右列任何一项进入开发，即视为范围蔓延事故。**

## 5. 核心功能清单（P0）

| # | 功能 | 说明 | 对应模块 |
| --- | --- | --- | --- |
| F1 | 站点配置化 | 竞品、页面、CSS 选择器外置 `targets.yaml`，新增竞品不改代码 | `config` |
| F2 | 抓取与结构化抽取 | Crawl4AI 主路径 + Playwright 兜底（仅直取 HTML，复用同一抽取策略） | `collector` |
| F3 | Schema 校验与复核队列 | 抽取结果强校验；失败写 `extraction_error` 入复核视图，不静默丢弃 | `models` / `store` |
| F4 | 内容指纹增量 | 目标容器可见文本归一化 sha256；未变则整条 LLM 链路短路 | `fingerprint` |
| F5 | 快照存储 | SQLite 单文件，快照、指纹、状态、错误详情落库 | `store` |
| F6 | 字段级 diff | 两次快照字段级对比，产出 `DiffResult` | `diff` |
| F7 | 变更解读 | 仅基于 diff（不传全文）生成中文变更分析 | `analyst` |
| F8 | Markdown 报告 | 对比表 + 变更摘要，Git 友好 | `reporter` |
| F9 | CLI | `init-db` / `run` / `review` / `report` 四个子命令 | `cli` |

## 6. 技术方案与项目结构

**一句话选型：Crawl4AI 抽、Pydantic 卡、SQLite 存、LangGraph 编、Playwright 兜、Markdown 报。**

五层架构（依赖只向下，领域层为纯函数）：接入层（Typer CLI）→ 编排层（LangGraph + SQLite checkpoint）→ 领域层（Schema 校验 / diff / 指纹）→ 采集层（Crawl4AI / Playwright / targets.yaml）→ 存储与产出（snapshots.db / Markdown 报告）。

```
competitor-intel/
├── pyproject.toml               # 依赖与 ci 命令入口
├── README.md                    # 合规声明、快速开始
├── MVP.md                       # 本文档
├── config/
│   └── targets.yaml             # 竞品、页面、CSS 选择器
├── src/competitor_intel/
│   ├── config.py                # 加载 targets.yaml 与环境变量
│   ├── models.py                # Pydantic 领域模型（最先写、反复打磨）
│   ├── fingerprint.py           # 容器文本归一化 + sha256
│   ├── collector.py             # Crawl4AI 主路径 + Playwright 兜底
│   ├── store.py                 # 快照读写、review_queue 视图
│   ├── diff.py                  # 字段级对比
│   ├── analyst.py               # 基于 diff 的变更解读
│   ├── reporter.py              # Markdown 报告渲染
│   ├── graph.py                 # StateGraph 组装、条件边、重试
│   └── cli.py                   # 命令入口
├── tests/
│   ├── fixtures/                # mock HTML 页面
│   ├── test_models.py           # 契约测试
│   ├── test_fingerprint.py      # 指纹单测（成本控制的根基）
│   ├── test_diff.py
│   └── test_collector.py
├── data/
│   ├── snapshots.db             # 运行时生成
│   └── reports/
└── Dockerfile                   # 镜像化，内置 Chromium
```

## 7. 数据契约摘要

**领域模型**：`CompetitorSnapshot`（竞品、页面、日期、页面类型）→ `PricingTier[]`（name / amount / currency / billing_period / key_limits）+ `FeatureItem[]`（name / description / category）+ `target_user_note`。`Field(description)` 即抽取指令。

**存储模型**：`snapshots` 表（competitor + page_url + snapshot_date 唯一），状态机为 `skipped | extracted | extraction_error`；失败记录永不消失，只进 `review_queue` 视图复核。

**关键取舍**：`amount` 用 float 换可比较性；"面议/免费"显式 `null`，不臆造 0。

## 8. 关键机制（可靠性 & 成本）

| 机制 | 设计 | 可观测指标 |
| --- | --- | --- |
| 双层缓存增量 | L1 Crawl4AI 框架缓存（默认 `bypass`：框架缓存无 TTL，会把一次错误渲染永久固化）；L2 业务指纹（是否重新调 LLM） | `skip_rate` |
| 失败兜底 | Crawl4AI 异常/空 → Playwright 直取 HTML 重走同一抽取策略（最多 1 次） | `fallback_rate` |
| 节点级重试 | 网络超时/5xx 指数退避（max_attempts=2, initial=1s, factor=2, jitter）；**非全局重试** | `errors` |
| 断点恢复 | LangGraph SqliteSaver checkpoint，中断后从最后成功节点续跑 | — |
| 三段校验 | ① Field description 入 prompt ② Pydantic 强类型 ③ 业务断言（套餐数 ≥1、价格币种成对） | `extraction_error_rate` |
| 三层成本控制 | 指纹短路 / 已知字段 CSS 定位仅多变字段走 LLM / 分析层只传 diff | `tokens_per_extraction` |
| 合规内置 | robots.txt 禁止则跳过记 `blocked`；请求间隔 ≥ 2s；仅公开页面 | `blocked_count` |

**核心判断：出错的数据比假装没出错的数据更危险。失败必须可见、可追、可复核。**

## 9. 里程碑与验收标准

| 周 | 主题 | 关键交付 | 验收标准 |
| --- | --- | --- | --- |
| W1 | 契约与单点跑通 | Pydantic 模型 + 单站抽取脚本 | HTML → JSON → 校验全链路跑通，记录三类失败案例 |
| W2 | 增量与兜底 | 指纹模块 + SQLite + Playwright 兜底 | mock 页面验证"变则抽、不变则跳"；断网触发兜底成功 |
| W3 | LangGraph 编排 | 5 节点图 + RetryPolicy + checkpointer | 中途 kill 后可从断点续跑，不重跑成功节点 |
| W4 | 报告与交付 | Markdown 对比表 + README + 合规声明 | README 含 Schema 理由、增量节省数据、兜底触发条件 |

**里程碑锚点**：W2 末 = 可重复运行、不全量烧钱的采集脚本；W3 末 = 可断点恢复的编排图；W4 末 = 可挂 GitHub 的完整 v0.1。

## 10. MVP 成功指标

| 指标 | 定义 | 健康阈值 |
| --- | --- | --- |
| `skip_rate` | 指纹未变 / 总目标数 | **> 60%**（增量生效的直接证据） |
| `extraction_error_rate` | 校验失败 / 实际抽取数 | < 10% |
| `fallback_rate` | 走 Playwright / 总抓取 | < 15% |
| `p95_latency_s` | 单目标耗时 P95 | < 90s |
| `tokens_per_extraction` | 单次抽取 token 均值 | 持续观察趋势 |
| `blocked_count` | robots 拦截数 | 记录，非越权 |

测试策略配套：指纹与 diff 单测为核心逻辑 100% 覆盖目标；集成测试用本地 mock 页面 + 假 LLM；真实站点冒烟测试由 `CI_SMOKE=1` 开关控制，默认跳过。

## 11. 主要风险与缓解

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| 页面改版导致选择器失效 | 指纹取错容器、抽取缺项 | 容器缺失告警并降级整页指纹；多选择器回退链 |
| LLM 输出结构不稳 | 校验失败率上升 | 优化 Field description；失败入复核；必要时字段二次抽取 |
| Crawl4AI / Chromium 版本冲突 | 部署失败 | 兼容上界约束；镜像构建期锁定 |
| 反爬封禁 | 采集中断 | 间隔 ≥ 2s、限速、仅公开页；优先抓 changelog RSS |
| 范围蔓延 | 无法按期交付 | 严控 In/Out of Scope；服务化延至 v0.2 |

## 12. 非目标与后续演进

| 版本 | 能力 | 触发条件 |
| --- | --- | --- |
| **v0.1（本 MVP）** | CLI、单品类 3 竞品、Markdown 报告 | 当前 |
| v0.2 | FastAPI + Celery 定时、飞书/邮件推送 | 采集稳定后 |
| v0.3 | Firecrawl change-tracking 增量备选、HTML 报告 | 需要更强的变更发现 |
| v1.0 | 多品类配置化、快照可视化仪表盘 | 有真实团队使用 |
