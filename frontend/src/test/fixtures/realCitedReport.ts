/**
 * 第二份真实夹具：**带引用索引的那一份**。
 *
 * 来源：`GET /api/reports/RP-a9466ff0aa56`（2026-09-19 取），
 * 裁掉了这一页不渲染、且本用例不需要的 21 个键
 * （`matrix` / `quality` / `metrics` / `team` / `charts` ……），
 * 只留正文两节、`citations`、以及那 4 条被引用的证据。
 *
 * **为什么还要第二份**
 * ----------------
 * `realReport.ts` 那份来得更早，docstring 里自己写着
 * "`citations` 整个键不存在"——它覆盖的是**旧报告**那条路径。
 * 而 `build_citations` 落地之后，任何新生成的报告都有 `citations`，
 * 所以"正文角标读存下来那份编号"才是**常走的那条路**，
 * 它此前只在 `lib/reportCitation.test.ts` 的单元层被验过，
 * 没有在渲染层验过"编号真的落到 DOM 上"。
 *
 * 这一份的裁剪规则里有一条与那份不同：**它来自一次 mock 运行**
 * （`subject` 是"示例调研对象"，证据的 `provider` 全是 `mock`）。
 * 这不影响它要验的东西——引用编号走的是同一条装配代码，
 * 与 provider 无关——但**写文档时不要拿它当真实品牌数据引用**。
 *
 * 类型标成 `ReportBody`，**没有 `as` 断言**：见问题 31.6。
 */
import type { ReportBody } from '../../types/report'

export const CITED_REPORT_BODY: ReportBody = {
  "version": "1.0",
  "taskId": "TK-d66fcd906382",
  "query": "对比 Notion 与 Obsidian",
  "subject": "示例调研对象",
  "domain": "效率工具",
  "category": "知识管理",
  "brands": [
    "示例调研对象",
    "示例品牌",
    "对照品牌"
  ],
  "mode": {
    "key": "quick",
    "label": "快速",
    "description": "十几分钟出结论，适合先摸个底"
  },
  "dimensions": [
    "功能覆盖",
    "定价策略",
    "用户口碑",
    "生态集成"
  ],
  "generatedAt": "2026-09-18T23:19:39+00:00",
  "durationMs": 233,
  "sections": [
    {
      "key": "executive_summary",
      "title": "执行摘要",
      "content": "本节基于已采集的证据梳理相关结论。\n\n第一，功能覆盖面上，官方页面与第三方评测给出了较为一致的描述，核心能力已经完备，差异主要体现在高级档位[证据: EV-7ff3df64c008]。\n\n第二，定价策略上，免费额度足以完成试用，但生产环境使用需要付费档位，这在用户社区讨论中被反复提及[证据: EV-bda50d06a718]。\n\n第三，从用户反馈看，正面评价集中在易用性，负面评价集中在导出与协作能力，这一分歧在不同平台上表现一致[证据: EV-d28541ffca9d][证据: EV-911f34b05c0d]。",
      "claimIds": [
        "CL-03ce5fa8be",
        "CL-21e4dd35c2",
        "CL-0dd0ff3de4"
      ],
      "evidenceIds": [
        "EV-7ff3df64c008",
        "EV-bda50d06a718",
        "EV-d28541ffca9d",
        "EV-911f34b05c0d"
      ],
      "degraded": false,
      "reworked": false
    },
    {
      "key": "feature_comparison",
      "title": "功能对比",
      "content": "本节基于已采集的证据梳理相关结论。\n\n第一，功能覆盖面上，官方页面与第三方评测给出了较为一致的描述，核心能力已经完备，差异主要体现在高级档位[证据: EV-7ff3df64c008]。\n\n第二，定价策略上，免费额度足以完成试用，但生产环境使用需要付费档位，这在用户社区讨论中被反复提及[证据: EV-bda50d06a718]。\n\n第三，从用户反馈看，正面评价集中在易用性，负面评价集中在导出与协作能力，这一分歧在不同平台上表现一致[证据: EV-d28541ffca9d][证据: EV-911f34b05c0d]。",
      "claimIds": [
        "CL-03ce5fa8be",
        "CL-21e4dd35c2",
        "CL-0dd0ff3de4"
      ],
      "evidenceIds": [
        "EV-7ff3df64c008",
        "EV-bda50d06a718",
        "EV-d28541ffca9d",
        "EV-911f34b05c0d"
      ],
      "degraded": false,
      "reworked": false
    }
  ],
  "citations": [
    {
      "number": 1,
      "evidenceId": "EV-7ff3df64c008"
    },
    {
      "number": 2,
      "evidenceId": "EV-bda50d06a718"
    },
    {
      "number": 3,
      "evidenceId": "EV-d28541ffca9d"
    },
    {
      "number": 4,
      "evidenceId": "EV-911f34b05c0d"
    }
  ],
  "evidences": [
    {
      "evidenceId": "EV-7ff3df64c008",
      "url": "https://www.bilibili.com/video/1741a0ff0d87",
      "title": "对照品牌 用户社区讨论 · B站评测视频",
      "snippet": "关于「对照品牌 用户社区讨论」的B站评测视频内容。该来源类型为 bilibili，用于验证可信度评分的分档是否生效。",
      "fullText": "",
      "brand": "对照品牌",
      "sourceType": "bilibili",
      "siteName": "B站评测视频",
      "publishedAt": "2024-06-15T00:00:00+08:00",
      "capturedAt": "2026-09-18T23:19:39+00:00",
      "matchedDimensions": [],
      "query": "对照品牌 用户社区讨论",
      "provider": "mock",
      "rank": 4,
      "credibility": 47.0,
      "credibilityBreakdown": {
        "sourceTypeScore": 32.0,
        "freshnessScore": 2.0,
        "contentScore": 3.0,
        "crossRefScore": 10.0,
        "penalties": 0.0,
        "notes": [
          "发布于 826 天前，已明显过时",
          "有效正文约 0 字",
          "同一议题有 11 个独立域名覆盖"
        ],
        "total": 47.0
      },
      "degraded": false,
      "images": []
    },
    {
      "evidenceId": "EV-bda50d06a718",
      "url": "https://www.xiaohongshu.com/explore/1deffd638139",
      "title": "对照品牌 用户社区讨论 · 小红书笔记",
      "snippet": "关于「对照品牌 用户社区讨论」的小红书笔记内容。该来源类型为 xiaohongshu，用于验证可信度评分的分档是否生效。",
      "fullText": "",
      "brand": "对照品牌",
      "sourceType": "xiaohongshu",
      "siteName": "小红书笔记",
      "publishedAt": "2026-08-14T00:00:00+08:00",
      "capturedAt": "2026-09-18T23:19:39+00:00",
      "matchedDimensions": [],
      "query": "对照品牌 用户社区讨论",
      "provider": "mock",
      "rank": 5,
      "credibility": 52.0,
      "credibilityBreakdown": {
        "sourceTypeScore": 26.0,
        "freshnessScore": 13.0,
        "contentScore": 3.0,
        "crossRefScore": 10.0,
        "penalties": 0.0,
        "notes": [
          "发布于 36 天前",
          "有效正文约 0 字",
          "同一议题有 11 个独立域名覆盖"
        ],
        "total": 52.0
      },
      "degraded": false,
      "images": []
    },
    {
      "evidenceId": "EV-d28541ffca9d",
      "url": "https://www.douyin.com/video/89702057abd3",
      "title": "对照品牌 用户社区讨论 · 抖音短视频",
      "snippet": "关于「对照品牌 用户社区讨论」的抖音短视频内容。该来源类型为 douyin，用于验证可信度评分的分档是否生效。",
      "fullText": "",
      "brand": "对照品牌",
      "sourceType": "douyin",
      "siteName": "抖音短视频",
      "publishedAt": "2026-05-02T00:00:00+08:00",
      "capturedAt": "2026-09-18T23:19:39+00:00",
      "matchedDimensions": [],
      "query": "对照品牌 用户社区讨论",
      "provider": "mock",
      "rank": 6,
      "credibility": 48.0,
      "credibilityBreakdown": {
        "sourceTypeScore": 24.0,
        "freshnessScore": 11.0,
        "contentScore": 3.0,
        "crossRefScore": 10.0,
        "penalties": 0.0,
        "notes": [
          "发布于 140 天前",
          "有效正文约 0 字",
          "同一议题有 11 个独立域名覆盖"
        ],
        "total": 48.0
      },
      "degraded": false,
      "images": []
    },
    {
      "evidenceId": "EV-911f34b05c0d",
      "url": "https://sspai.com/post/7554680a96e7",
      "title": "对照品牌 用户社区讨论 · 少数派体验报告",
      "snippet": "关于「对照品牌 用户社区讨论」的少数派体验报告内容。该来源类型为 review，用于验证可信度评分的分档是否生效。",
      "fullText": "",
      "brand": "对照品牌",
      "sourceType": "review",
      "siteName": "少数派体验报告",
      "publishedAt": "2025-11-20T00:00:00+08:00",
      "capturedAt": "2026-09-18T23:19:39+00:00",
      "matchedDimensions": [],
      "query": "对照品牌 用户社区讨论",
      "provider": "mock",
      "rank": 7,
      "credibility": 67.0,
      "credibilityBreakdown": {
        "sourceTypeScore": 46.0,
        "freshnessScore": 8.0,
        "contentScore": 3.0,
        "crossRefScore": 10.0,
        "penalties": 0.0,
        "notes": [
          "发布于 303 天前",
          "有效正文约 0 字",
          "同一议题有 11 个独立域名覆盖"
        ],
        "total": 67.0
      },
      "degraded": false,
      "images": []
    }
  ],
  "degraded": []
}
