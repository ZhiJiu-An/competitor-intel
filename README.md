# competitor-intel

**Schema 契约驱动的竞品情报管道**：定期抓取竞品公开的定价页 / 更新日志，用 LLM 抽取成**可校验的结构化对象**，与历史快照做字段级 diff，产出可审计的 Markdown 报告。

> **Crawl4AI 抽、Pydantic 卡、SQLite 存、LangGraph 编、Playwright 兜、Markdown 报。**

---

## 为什么不是"爬虫 + AI 总结"

| 常见做法的问题 | 本项目的做法 |
| --- | --- |
| 输出是自由文本，无法比对、无法回溯 | 输出是 Pydantic 模型，字段级 diff 精确到 `pricing_tiers[Pro].amount` |
| 抽取失败被静默丢弃或错误填充 | 失败落库为 `extraction_error` 进复核队列，**永不消失** |
| 每次运行全量调 LLM，成本随竞品数线性涨 | 内容指纹未变 → 整条 LLM 链路短路，**零消耗** |

核心判断：**出错的数据比假装没出错的数据更危险。**

## 特性

- **契约即指令** —— Pydantic `Field(description)` 同时是抽取 prompt 和校验规则，改 Schema 即改行为
- **增量成本控制** —— 目标容器文本归一化后 sha256 指纹，未变则跳过（`skip_rate` 长期 > 60%）
- **失败可见** —— 三段校验（prompt 约束 / 强类型 / 业务断言），失败进 `review_queue`
- **抓取兜底** —— Crawl4AI 异常或空页面时，Playwright 直取 HTML 复用同一套抽取策略（最多 1 次）
- **断点恢复** —— LangGraph checkpoint，中断后从下一个目标续跑
- **配置化** —— 新增竞品只改 `targets.yaml`，不写代码
- **两种入口** —— CLI 单次运行 / HTTP + Celery 定时调度
- **飞书推送** —— run 结束自动推卡片摘要，可开关

## 工作原理

```
targets.yaml
    │
    ▼
┌────────┐   ┌──────────────────────────────────────────┐   ┌──────┐   ┌─────────┐   ┌────────┐
│  plan  │──▶│  fetch（每个目标循环一次，逐目标 checkpoint） │──▶│ diff │──▶│ analyze │──▶│ report │
└────────┘   └──────────────────────────────────────────┘   └──────┘   └─────────┘   └────────┘
                        │
                        ├─ robots.txt 检查 → 限速 ≥2s
                        ├─ Crawl4AI 抓取 HTML
                        ├─ 指纹比对 ── 未变 ──▶ skipped（不调 LLM）
                        ├─ 变更 ──▶ LLM 结构化抽取（schema 约束）
                        └─ 三段校验 ── 失败 ──▶ extraction_error 落库
```

## 快速开始

需要 Python ≥ 3.11。

```bash
git clone https://github.com/ZhiJiu-An/competitor-intel.git
cd competitor-intel

python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium

cp .env.example .env          # 填入 LLM_API_KEY
```

**最小可运行示例**

```bash
ci init-db
ci run --config config/targets.yaml
```

第二次运行若页面没变，你会看到 `skipped` 而非重新抽取——这就是增量生效。

## 配置

`config/targets.yaml`：

```yaml
defaults:
  request_interval_s: 2      # 合规：请求间隔 ≥ 2s
  respect_robots: true       # 遵守 robots.txt
  timeout_s: 30
  cache_mode: bypass         # Crawl4AI L1 缓存无 TTL，默认绕开；增量交给业务指纹

targets:
  - competitor: deepseek
    pages:
      - type: pricing        # pricing / changelog
        url: https://api-docs.deepseek.com/quick_start/pricing
        main_selector: "main"    # 指纹容器：只比较正文区域，忽略导航/页脚
```

新增竞品只需追加一段，无需改代码。

## 使用

### CLI

```bash
ci init-db                                   # 初始化 SQLite
ci run                                       # 全量运行（走指纹短路）
ci run --competitor deepseek --force         # 指定竞品并强制忽略指纹
ci review                                    # 查看复核队列
ci review --retry-failed                     # 强制重跑失败页面
ci report --since 2026-10-01 --out data/reports/latest.md   # 从历史快照重新出报告
ci serve --port 8000                         # 启动 HTTP 服务
```

### 输出示例

报告（`data/reports/2026-11-02.md`，示例数据）：

```markdown
# 竞品情报报告（2026-11-02）

## 概要

- 监控页面快照：3 个
- 发生变更：1 个页面

## 变更明细

### acme-cloud — https://acme-cloud.example/pricing

基线 2026-10-26 → 当前 2026-11-02

| 字段 | 旧值 | 新值 |
| --- | --- | --- |
| pricing_tiers[Pro].amount | 25.0 | 29.0 |
| pricing_tiers[Pro].key_limits | 3 席位; 50GB 存储 | 5 席位; 100GB 存储 |

**变更解读**：Pro 档涨价 16%，同时席位与存储配额各提升约一倍，属于"提价 + 加量"的同步调整……

## 当前定价快照

| 竞品 | 页面 | 套餐 | 价格 | 周期 | 关键限制 |
| --- | --- | --- | --- | --- | --- |
| acme-cloud | /pricing | Free | 0 USD | monthly | 单用户; 社区支持 |
| acme-cloud | /pricing | Pro | 29 USD | monthly | 5 席位; 100GB 存储 |
| acme-cloud | /pricing | Enterprise | 免费/面议 | custom | SSO; SLA |
```

按量计费页面（如 token 计价）会把多个维度压进同一行，保证「一个模型一行」且仍是数值型（示例数据）：

```
| 竞品 | 套餐 | 价格 |
| --- | --- | --- |
| acme-llm | acme-flash | 输入·缓存未命中 0.34 CNY/1M tokens；输出 1.02 CNY/1M tokens |
```

### HTTP API + 定时调度

```bash
pip install -e ".[server]"

# 一键起 redis + api + worker + beat（日志在 scripts/.logs/）
./scripts/start.sh up
./scripts/start.sh status
./scripts/start.sh down
```

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/runs` | 触发采集。同日同竞品幂等：首次 `202`，重复 `200` 且 `run_id` 不变 |
| GET | `/runs/{run_id}` | 状态（`queued/running/succeeded/partial/failed`）+ 计数指标 |
| GET | `/snapshots` | 快照元数据，支持 `competitor/since/until/limit` |
| GET | `/reports/latest` | 最新 Markdown 报告（`text/markdown`） |
| GET | `/healthz` | 探活（含数据库可用性） |

```bash
curl -X POST localhost:8000/runs -H 'Content-Type: application/json' \
     -d '{"competitor":"deepseek"}'
# {"run_id":"...","status":"queued","idempotent":false}

curl localhost:8000/runs/<run_id>
# {"status":"succeeded","processed":1,"extracted":1,"skipped":0,
#  "metrics":{"skip_rate":0.0,"fallback_rate":0.0,"extraction_error_rate":0.0}, ...}
```

`beat` 默认每天 09:00（`Asia/Shanghai`）触发 `competitor_intel.daily_run`，只创建当日 run 再投递，重复触发不会重复抓取。

> **安全提示**：`api` 默认只绑定 `127.0.0.1`。若改为对外暴露（`0.0.0.0`），**必须**设置 `API_TOKEN`，否则任何人都能触发采集并读取快照与报告。

**状态语义**：`succeeded`（含"全部 skipped"，页面无变化属正常）/ `partial`（有兜底、抽取失败或 robots 拦截）/ `failed`（有目标但一条都没抽到，或空批次）。当日 `failed` 允许重新 POST 重试，`run_id` 复用。

### 飞书推送

配好 webhook 后，每次 run 结束自动推一张卡片（状态色 / 计数 / 三个比率 / 报告路径）：

```bash
FEISHU_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/xxxx
FEISHU_KEYWORD=竞品分析        # webhook 若开了关键词校验，卡片标题会自动带上
```

推送失败只记 warning，**不影响 run 状态与返回值**。

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `LLM_API_KEY` | — | 必填（采集路径），OpenAI 兼容接口的 key |
| `LLM_BASE_URL` | — | 兼容接口的 base URL，如 `https://api.deepseek.com` |
| `LLM_MODEL` | `deepseek-chat` | 模型 ID；带 `/` 则原样作为 litellm provider |
| `DB_PATH` | `data/snapshots.db` | SQLite 路径 |
| `REPORT_DIR` | `data/reports` | 报告输出目录 |
| `API_TOKEN` | 空 | 设置后 API 需带 `X-API-Token` 请求头 |
| `FEISHU_WEBHOOK` | 空 | 留空则不推送 |
| `REDIS_URL` | `redis://127.0.0.1:6379/0` | Celery broker / backend |
| `DAILY_CRON_HOUR` | `9` | beat 每日触发小时 |

## 数据契约

```python
CompetitorSnapshot
├── competitor_name / page_url / snapshot_date / page_type
├── pricing_tiers: PricingTier[]
│   ├── name / amount / currency / billing_period / key_limits / pricing_model
│   └── components: PriceComponent[]        # 按量计费的多维价格（订阅制留空）
│       └── unit / amount / currency / condition
├── feature_items: FeatureItem[]
│   └── name / description / category
└── target_user_note
```

两个刻意的取舍：

- `amount` 用 **float** 换可比较性，且用 `StrictFloat` 拒绝 `"99"` 这类字符串，防止 LLM 输出被静默强转；
- "免费 / 面议" 显式 `null`，**不臆造 0**。

## 项目结构

```
competitor-intel/
├── config/targets.yaml          # 站点配置
├── src/competitor_intel/
│   ├── models.py                # Pydantic 契约（最先写、反复打磨）
│   ├── fingerprint.py           # 容器文本归一化 + sha256
│   ├── collector.py             # Crawl4AI 主路径 + Playwright 兜底
│   ├── store.py                 # SQLite 读写 / review_queue / runs
│   ├── diff.py                  # 字段级对比（纯函数）
│   ├── analyst.py               # 基于 diff 的变更解读
│   ├── reporter.py              # Markdown 报告
│   ├── graph.py                 # LangGraph 五节点编排
│   ├── service.py               # 幂等执行 + 三态判定 + 指标
│   ├── api.py / tasks.py        # FastAPI / Celery
│   ├── notifier.py              # 飞书 webhook
│   └── cli.py                   # 命令入口
├── scripts/start.sh             # 一键启停本地四进程
├── tests/                       # 单测 + 集成测试（假 LLM / mock 页面）
└── docker-compose.yml           # redis / api / worker / beat
```

## 测试

```bash
pytest              # 单测 + 集成测试（不触网、不花 token）
CI_SMOKE=1 pytest   # 额外跑真实站点冒烟（会消耗 token）
```

## 合规

- 仅采集**公开可访问**页面，不触碰登录态与付费墙
- 尊重 `robots.txt`，禁止则跳过并记录 `blocked`
- 默认请求间隔 ≥ 2s、单目标超时 30s
- 请自行确认目标站点的服务条款；本项目不提供绕过反爬的能力

## 已知限制

- 页面改版导致 `main_selector` 失效时，指纹可能取错容器（会告警并降级整页指纹）
- Crawl4AI 的 L1 缓存无 TTL，因此默认 `cache_mode: bypass`，增量只由业务指纹负责
- macOS 本地跑 Celery worker 需 `--pool=solo`（fork 子进程里启动 Playwright 易崩）

## Roadmap

- [ ] 邮箱推送（SMTP，报告作为附件）
- [ ] HTML 报告 / 快照可视化
- [ ] 多品类配置化、Firecrawl change-tracking 作为增量备选

## 文档

- [MVP.md](./MVP.md)：范围、验收标准与里程碑
- [LICENSE](./LICENSE)：Apache-2.0
