# CLAUDE.md

给 Claude Code 的项目上下文。开始工作前读这一份。

## 项目概述

**xm3** 是一个多 Agent 竞品分析系统：输入一个调研需求，自动完成需求拆解 → 专家组队 → 联网采集 → 分析研判 → 质量审计 →（返工）→ 报告撰写。

- 项目根：`D:\Dekcop\xiangmu\xm3`
- 定位：求职作品。因此**可解释性优先于功能数量**，每个技术决策都要能在面试里讲清理由与代价。
- 后端 8020 / 前端 3500。刻意避开参考项目 Verda 占用的 8010 / 3400，两套可以同时跑。

### 四条铁律（贯穿全部设计，改动时不要破坏）

1. **无证据不立论** —— 论点必须挂 `evidence_ids`
2. **交叉验证** —— 高置信需 ≥2 个独立域名
3. **返工闭环** —— 质量不达标打回重做，不降低标准
4. **全程可观测** —— 每次 LLM 调用可回放（prompt / 输出 / token / 成本 / 决策）

### 两条硬约束（违反会毁掉这个项目，不是风格问题）

1. **绝不复制参考项目 Verda 的代码。** 两个理由：Verda 是 **AGPL-3.0**，
   抄了 xm3 就得跟着 AGPL（本项目是 MIT）；而且这是求职作品，
   作者必须能解释每一行。**只借鉴架构思路，实现全部重写。**
2. **数据源必须留在适配层后面。** 流水线里不允许出现任何厂商名
   （`bocha` / `deepseek` / `zhipu`）、厂商参数名（`include` / `oneMonth` / `days`）、
   或厂商模型名。出现了就说明方言漏上来了，要推到 `providers/` 里去。

## 目录结构

```
xm3/
├─ backend/
│  ├─ app/
│  │  ├─ main.py              FastAPI 入口（单后端，不做多副本镜像）
│  │  ├─ api/                 路由层
│  │  ├─ core/
│  │  │  ├─ config.py         配置；provider 凭据按命名约定动态查
│  │  │  ├─ modes.py          三档调研模式参数
│  │  │  ├─ models.py         领域模型
│  │  │  ├─ schemas/          结构化知识 Schema 与容错校验
│  │  │  ├─ pipeline/         ★ 流水线：stages / runner / orchestrator / 各阶段
│  │  │  ├─ evidence/         可信度、相关性、正文质量、抓取
│  │  │  ├─ observability/    trace 埋点与事件定义
│  │  │  └─ analysis/         舆情、图表、指标
│  │  ├─ providers/           ★ 适配层：llm/ search/ registry / base / errors / retry / cassette
│  │  ├─ db/                  连接、迁移、各表 repo
│  │  └─ data/                专家名册 seed 与生成产物
│  ├─ scripts/                生成专家、CLI 跑流水线、录制 cassette、文档一致性检查
│  └─ tests/                  unit / integration / e2e / fixtures
├─ frontend/src/
│  ├─ types/                  ★ 契约层（先写类型，后写实现）
│  ├─ lib/                    api 客户端、SSE 封装、格式化
│  ├─ store/                  Zustand
│  ├─ hooks/  components/  pages/
├─ eval/                      黄金集与 LLM-as-judge 评测
├─ loadtest/                  Locust 压测
└─ docs/                      架构、Provider、API、数据模型、评测、决策记录
```

## 技术栈

| 层 | 选型 |
|---|---|
| 后端 | FastAPI · SQLite(WAL) · httpx · trafilatura · pydantic-settings |
| 前端 | React 19 · Vite · TypeScript(strict) · Tailwind v4 · Zustand · React Router v7 · ECharts · d3 |
| 默认 Provider | DeepSeek（LLM）· 博查 Bocha（搜索） |

选型理由与**已知代价**见 [技术栈.md](./技术栈.md)。

## 关键文件

| 文件 | 为什么重要 |
|---|---|
| `backend/app/core/config.py` | provider 凭据按 `<PROVIDER>_API_KEY` 约定动态查——接入新 provider 不用改这个文件 |
| `backend/app/providers/base.py` | 归一化接口契约。流水线不出现厂商名，全靠它 |
| `backend/app/core/pipeline/stages.py` | 流水线顺序的**唯一真相源**，前端 DAG 与架构文档都由它派生 |
| `backend/app/core/pipeline/runner.py` | 每个任务只跑一次的守卫 + 事件日志（断线续传的基础） |
| `backend/app/core/evidence/credibility.py` | 可解释可信度评分，返回分项明细而非裸分数 |
| `frontend/src/types/domain.ts` | 领域契约。后端 pydantic 用 camelCase 别名与之对齐 |
| `frontend/src/types/events.ts` | SSE 事件判别联合。不要退化成 `unknown` + `as` |
| `frontend/src/store/taskStore.ts` | SSE 摄入：seq 去重 + rAF 合帧 + 环形上限 |

## 开发与运行

```bash
# 后端（Windows）
cd backend && .venv/Scripts/python.exe -m uvicorn app.main:app --port 8020

# 前端
cd frontend && npm run dev        # :3500，代理 /api 与 /health 到 8020

# 测试（默认不含联网用例，零成本）
cd backend && .venv/Scripts/python.exe -m pytest
cd frontend && npm test

# 需要联网的用例（会产生费用，主动才行）
.venv/Scripts/python.exe -m pytest -m live
```

## 易踩坑

- **不要用 `uvicorn --reload`**：SQLite 文件在被监视的目录内，每次写库都会触发重载，表现为服务反复重启、请求卡死。要热重载就限定 `--reload-dir app`，或者干脆不用。
- **本机 `python3` 是坏的**（指向一个失效路径），只能用 `python`。
- **测试默认不联网**。若测试挂在"连接失败"，先确认是不是该用 cassette 回放的场景被写成了 live。
- **`.env` 不入库**，`.env.example` 入库。新增配置项时先改 `.env.example`，否则别人拉下来跑不起来。
- **前端端口被占会直接失败**（`strictPort: true`）。这是刻意的：静默换端口会让代理指向错误的后端而无人察觉。

## 文档索引

[开发文档.md](./开发文档.md) · [技术栈.md](./技术栈.md) · [问题记录.md](./问题记录.md) · [项目总结.md](./项目总结.md) · [面试问答集.md](./面试问答集.md) · [docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md) · [docs/PROVIDERS.md](./docs/PROVIDERS.md)
