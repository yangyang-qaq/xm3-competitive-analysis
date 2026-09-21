/**
 * Playwright 只做一件事：**在真浏览器里把报告页打开一次**。
 *
 * 为什么这件事非真浏览器不可
 * --------------------------
 * `vitest` 跑在 jsdom 里，而 jsdom **没有 canvas 和布局**。
 * 报告页上有四张 ECharts 图和一个 d3 知识图谱，它们在这套单测里
 * 全都不存在——`ReportChart` 的 `useEffect` 里 `echarts.init(host)`
 * 在 jsdom 下拿到的是一个没有 2d 上下文的元素。
 *
 * 所以有一条缺陷是单测**结构上抓不到**的：图在真实浏览器里没画出来。
 * 而这条缺陷真的发生过——`ReportChart` 的文件头记着：折线图从第一天起
 * 就发 `kind: "line"`，而按需引入的清单里没有它，于是趋势图在页面上
 * 一直是那句"这一版还不认识这种图（line）"。**没有报错、没有白屏。**
 * 它是靠人读代码发现的，不是靠测试。
 *
 * 这个文件补的就是那个缺口：把一份真实报告喂进去，断言**没有一张图
 * 落在"没画出来"那条分支上**。
 *
 * 怎么拿到数据：拦 `/api/**` 返回静态 fixture
 * ------------------------------------------
 * 不启后端。`ReportDetail` 那份 fixture 是从一次真实请求的响应里裁下来的
 * （见 `src/test/fixtures/realReport.ts` 的文件头），所以它带着真实报告
 * 才有的那些边角：空数组的 `trends`、没有 `citations` 键、24 张图集。
 * 换成"我以为页面会读的字段"手写一份，这个测试就只会证明它自己是对的。
 *
 * 也**不连真后端**：那样这个测试的成败会取决于本机有没有跑后端、
 * 库里那份报告还在不在——一条"要先把环境搞对"的冒烟测试，
 * 最后没人会跑它。
 */
import { defineConfig, devices } from '@playwright/test'

/** 预览端口。**刻意不用 3500**：dev server 常驻在 3500（本机一直开着），
 * 两者撞端口的话，要么这个测试跑不起来，要么它测的是 dev server 那份代码。 */
const PORT = 4173

export default defineConfig({
  testDir: './e2e',
  // 每次都先构建。**理由见 `e2e/global-setup.ts`**：不加这一句，
  // `npx playwright test` 会拿上一次构建的 `dist/` 跑，改了 src 也照样绿。
  globalSetup: './e2e/global-setup.ts',
  // 冒烟测试就该是短的。一条用例超过 30 秒，说明它已经在测别的东西了。
  timeout: 30_000,
  expect: { timeout: 8_000 },
  fullyParallel: true,
  // CI 上禁止 `test.only`：一条忘了删的 only 会让其余用例静默消失，
  // 而输出仍然是绿的。
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [['github'], ['list']] : [['list']],
  outputDir: 'e2e/.artifacts',

  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    // 失败时留一份可点的证据。成功时不留——省得仓库里堆一堆没人看的 trace。
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
  },

  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],

  // 起的是**构建产物**（`vite preview`），不是 dev server。
  // 差别不只是速度：dev 与 build 走的是两条不同的代码路径
  // （Tailwind 的 JIT、React 的生产/开发模式、代码分割），
  // 而"dev 下好好的、build 出来是白屏"是这套组合里最常见的翻车方式。
  // 测 dev server 等于放过它。
  webServer: {
    command: `npm run preview -- --port ${PORT} --strictPort --host 127.0.0.1`,
    url: `http://127.0.0.1:${PORT}`,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
})
