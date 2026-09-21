# xm3 · 多 Agent 竞品分析系统

一个证据驱动、全程可观测、带返工闭环的 AI 竞品调研系统。给它一个调研需求，它会自动拆解需求、组建专家团队、联网采集证据、分析研判、质量审计、返工补采，最后产出一份每个论点都能追溯到原始信源的报告。

> **项目状态**：功能完整。十个阶段全部完成——流水线、48 专家三层分工、SSE 工作台、报告页、决策回放、知识图谱、仪表盘、评测闸门、压测与文档都已就位。
> 后端 **943 条 pytest**、前端 **314 条 vitest + 1 条 Playwright**，全绿且**默认零 API 花费**。
> 逐阶段的开发记录见 [开发文档.md](./开发文档.md)，未做完的部分见 [技术栈.md](./技术栈.md) 的「负面清单」。

---

## 它解决什么问题

用 LLM 做竞品调研，最容易出的不是"写不出来"，而是**写得太顺**——模型会流畅地编造市场份额、编造定价、编造用户评价，而你无法分辨哪一句有据可依。

xm3 的核心不是"让模型写报告"，而是给这套流程加上四道约束：

| 铁律 | 含义 | 在系统里怎么落地 |
|---|---|---|
| **无证据不立论** | 每个论点必须挂到具体证据上 | 论点的 `evidence_ids` 会被校验，LLM 编造的引用被丢弃并计数 |
| **交叉验证** | 高置信结论需要 ≥2 个独立域名支撑 | `independentDomains` 决定 `confidence`，单源只能给到中置信 |
| **返工闭环** | 质量不达标就打回重做，不降低标准 | 质检不通过时发出 `REWORK` 信封，回退到采集或分析阶段 |
| **全程可观测** | 每次 LLM 调用的输入输出、耗时、成本、决策依据都可回放 | contextvars 无侵入埋点，按任务记录 trace span |

可量化的是第一、二条的副产品：**幻觉引用率**和**引用忠实度**——直接度量"这份报告有多少是编的"。

---

## 核心设计

### Provider 适配层

LLM 与搜索都藏在归一化接口后面，流水线里不出现任何厂商名：

```python
# 采集阶段只表达意图，不关心它怎么变成 HTTP 请求
search(SearchQuery(text=query, sites=("douyin.com",), freshness="month"))
```

这行调用会变成博查的 `include` + `oneMonth`、Tavily 的 `include_domains` + `days`、还是 Serper 的 `site:` + `tbs`，是适配器的私事。切换 provider 只改 `.env` 两行，代码零改动。

附带的好处：适配层可以包一层**录制/回放**（cassette），于是整条真实流水线能在 CI 里离线跑完，零 API 花费。

详见 [docs/PROVIDERS.md](./docs/PROVIDERS.md)。

### 三层专家结构

48 位虚拟专家分三层：3 位决策层（调研总监 / 首席分析官 / 质检总监）、9 位战略层（战略、定价、用户研究、增长…）、36 位执行层（24 个行业专家 + 12 个职能专家）。每次任务由调度器按调研主题挑选一个小队，而不是 48 人全部上场。

### 质检在写作之前

流水线顺序是 `intake → orchestrator → collect → analyze → audit → rework → write`（`rework` 是可选阶段，质检通过时跳过）。

质检刻意排在撰写之前：返工要发生在最昂贵的步骤（多章节并行撰写）之前，而不是之后。这个顺序由 `core/pipeline/stages.py` 单一维护，前端 DAG 和架构文档都从它派生，并有测试盯着文档与代码一致。

---

## 技术栈

**后端** FastAPI · SQLite（WAL）· httpx · trafilatura · openai SDK（作 OpenAI 兼容协议客户端）
**前端** React 19 · Vite · TypeScript · Tailwind v4 · Zustand · React Router · ECharts · d3
**默认 Provider** DeepSeek（LLM）· 博查 Bocha（搜索）

每个选型的理由与代价记录在 [技术栈.md](./技术栈.md)。

---

## 快速开始

### 环境要求

Python ≥ 3.11、Node ≥ 20。开发与验证都在 Windows（Git Bash 与 PowerShell 各一套脚本）。

### 1. 装依赖

```bash
# 后端
cd backend
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"   # macOS / Linux 换成 .venv/bin/python
cp .env.example .env                                  # 填入 API Key

# 前端
cd ../frontend
npm install
```

### 2. 起服务

```bash
bash scripts/dev.sh          # Git Bash
pwsh scripts/dev.ps1         # PowerShell
```

后端 **8020**、前端 **3500**。前端通过 vite 代理把 `/api` 与 `/health`
转发到后端，所以浏览器侧不需要配置后端地址。

两个脚本都会先探端口：**端口被占时直接失败并告诉你占着的是谁，
而不是悄悄换一个**——否则代理会指向错误的后端而无人察觉。

### 3. 验证

```bash
curl http://127.0.0.1:8020/health
```

应返回当前生效的 provider 与数据库路径（不含任何密钥）。

### 4. 不想配 Key 也能跑

```bash
cd backend
LLM_PROVIDER=mock SEARCH_PROVIDER=mock FETCH_PROVIDER=mock \
  .venv/Scripts/python.exe -m scripts.run_pipeline_cli "对比 Notion 与 Obsidian" --mode quick
```

mock provider 是确定性的、不看 query、零成本零网络，
**整条流水线（含 SSE 事件流）会完整跑一遍**。
所有测试走的也是这条路径——所以"跑得起来"不依赖任何账号。

### 5. 停服务

```bash
bash scripts/stop.sh         # 或 pwsh scripts/stop.ps1
```

日志与 PID 留在 `.dev/`（已 gitignore，**跑完不删**，留给事后看）。

---

## 规模

| | 物理行 | 测试 |
|---|---|---|
| 后端（`app/` + `tests/`） | 约 **33,200** | pytest **943** 条（另有 6 条 `live` 默认不跑） |
| 前端 `src/` | 约 **20,400** | vitest **314** 条 + Playwright **1** 条 |

只放两个量级数。**逐目录的行数、注释行数、文件数只在
[项目总结.md](./项目总结.md) 的「技术统计表」里**——同一个数字写两处就有两个版本，
而散文里的数字没有任何工具会去比对（详见 [问题记录.md](./问题记录.md) 的问题 48）。

报的是**物理行**，不是"代码行"：跨语言没法一致地算——Python 有 docstring、
TS 有块注释，两个不能比的数放在一张表里比，比不报更糟。

---

## 测试

```bash
# 后端
cd backend && .venv/Scripts/python.exe -m pytest

# 前端
cd frontend && npm test
```

**跑测试不花钱**。联网且产生费用的用例标记为 `live`，默认被 `addopts` 排除，需显式 `pytest -m live` 才会执行。

---

## 文档

| 文档 | 内容 |
|---|---|
| [开发文档.md](./开发文档.md) | 分阶段开发记录，含每阶段的数据库/API/前端/验证方法 |
| [技术栈.md](./技术栈.md) | 选型理由、已知代价与负面清单 |
| [问题记录.md](./问题记录.md) | 54 条按「现象→定位→根因→修复→验证」记录的工程问题 |
| [项目总结.md](./项目总结.md) | 技术统计与项目回顾 |
| [面试问答集.md](./面试问答集.md) | 设计决策的问答预演 |
| [docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md) | 架构与流水线，**顺序由测试守护** |
| [docs/PROVIDERS.md](./docs/PROVIDERS.md) | Provider 适配层设计与方言对照表 |
| [docs/AGENTS.md](./docs/AGENTS.md) | 48 专家名册、组队两道关、消息协议、四条铁律 |
| [docs/API.md](./docs/API.md) | 21 条路由的字段、状态码与口径说明 |
| [docs/DATA_MODEL.md](./docs/DATA_MODEL.md) | 9 张表、迁移史、已知缺口 |
| [docs/EVAL.md](./docs/EVAL.md) | 评测体系，以及 mock 闸门**做不到**的事 |
| [修补文档.md](./修补文档.md) | 遗留问题与修复计划 |

文档里的相对链接有一条测试守着
（[`backend/tests/unit/test_docs_links.py`](./backend/tests/unit/test_docs_links.py)）——
因为这份 README 曾经链向五个还不存在的文件，而**没有任何工具发现得了**。
**测试条数**也一样：这份 README、[`项目总结.md`](./项目总结.md)、
[`技术栈.md`](./技术栈.md)、[`面试问答集.md`](./面试问答集.md) 里
每一处写了条数的话都由
[`backend/tests/unit/test_docs_counts.py`](./backend/tests/unit/test_docs_counts.py)
对着 `pytest --collect-only` 断言过——那个数曾经在四份文档之间漂了三轮。

---

## 许可

[MIT](./LICENSE)。本项目为独立实现，仅借鉴同领域项目的架构思路，未复制其代码。
