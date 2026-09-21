/**
 * 报告页冒烟：**在真浏览器里**把一份真实报告打开一次。
 *
 * 这个文件存在的唯一理由是 jsdom 画不了图。见 `playwright.config.ts` 的文件头：
 * 报告页有四张 ECharts（canvas）+ 一个 d3 图谱，单测里它们全都不存在，
 * 所以"图在浏览器里没画出来"这一类缺陷**结构上抓不到**——而它真的发生过
 * （折线图长期显示"这一版还不认识这种图"）。
 *
 * 断言的重心因此不在"页面能打开"，而在下面三件单测做不到的事：
 *   1. 每张图都真的**画出来了**（canvas 有像素），不是那句"没画出来"；
 *   2. 打开过程中**控制台没有报错**；
 *   3. 页面在最窄的桌面宽度下没有横向溢出（ECharts 的宽度算错时最先出现）。
 */
import { expect, test, type ConsoleMessage } from '@playwright/test'

import { REAL_REPORT_BODY } from '../src/test/fixtures/realReport'

const REPORT_ID = 'RP-e2e-smoke'

/** 页面显示的那句"图没画出来"。文案改了就跟着改——**改的时候要意识到
 * 这条测试会红**，而不是顺手把它删掉。它守着的是这个文件头说的那件事。 */
const CHART_FALLBACK = '这张图没画出来'

test.describe('报告页', () => {
  test('一份真实报告能画出来，且过程中没有控制台报错', async ({ page }) => {
    const errors: string[] = []
    page.on('console', (msg: ConsoleMessage) => {
      if (msg.type() === 'error') errors.push(msg.text())
    })
    // 未捕获的异常不会被 console 事件收到，要单独听——React 渲染期抛的错
    // 大多走这条。少了它，"没有控制台报错"会漏掉最严重的那一类。
    page.on('pageerror', (err: Error) => errors.push(`pageerror: ${err.message}`))

    await mockApi(page)

    await page.goto(`/report/${REPORT_ID}`)

    // ---- 1. 页面真的渲染了 ----
    // 用报告 id 而不是"某个标题"：标题来自 fixture，哪天换了 fixture
    // 这里会跟着失效；而 id 是这条 URL 的输入，它出现就说明
    // "请求发出去了 → 响应被解析了 → 页面拿到了它"。
    await expect(page.getByText(REPORT_ID)).toBeVisible()
    // 按**角色**定位而不是按文本：`执行摘要` 在页面上出现两次
    // （目录里一个按钮、正文里一个 `h2`），`getByText` 会撞上
    // Playwright 的严格模式。这里要的是正文那一节真的渲染了，
    // 所以指定 `heading`——顺带也验证了它确实是个标题元素，
    // 而不是被渲染成了一段普通文字。
    await expect(page.getByRole('heading', { name: '执行摘要' })).toBeVisible()

    // ---- 2. 每张图都画出来了 ----
    //
    // 定位全部**限定在 `#charts` 里面**。整页找 `figure` 是不行的：
    // 页面上的"图集"（`gallery`）也是 `figure`，本 fixture 里就有 2 个，
    // 于是"figure 数 ≥ 4"在图表整块缺失时**照样能过**（2 个图集 + 别的）。
    // 第一版就是这么写的，反证时它红在了数量上而不是"图表没了"上——
    // 红对了，但理由不对，那种红下次会红在别的地方。
    const panel = page.locator('#charts')
    await expect(panel, '图表这一块整个没渲染（#charts 不在页面上）').toBeVisible()

    const expected = REAL_REPORT_BODY.charts ?? []
    expect(expected.length, 'fixture 里没有图，这条测试会退化成空转').toBeGreaterThan(0)

    const charts = panel.locator('figure')
    await expect(charts, '页面上挂出来的图数和报告里的对不上').toHaveCount(expected.length)

    // 逐张查那句失败文案。用 `filter` 而不是 `getByText`：
    // 后者会把**任意**一处出现算进去，包括本该出现的地方。
    await expect(charts.filter({ hasText: CHART_FALLBACK })).toHaveCount(0)

    // canvas 有像素才算画出来了。上面那条只证明"没走失败分支"，
    // 而 `echarts.init` 拿不到尺寸时照样不报错、只是什么都不画——
    // 那会得到一张尺寸为 0 的 canvas，`toBeVisible` 也可能因为
    // 父元素有高度而通过。所以这里直接量像素。
    const canvases = panel.locator('canvas')
    await expect(canvases).toHaveCount(expected.length)
    for (let i = 0; i < expected.length; i += 1) {
      const box = await canvases.nth(i).boundingBox()
      expect(box?.width ?? 0, `第 ${i + 1} 张图（${expected[i]?.title}）的 canvas 宽度是 0`).toBeGreaterThan(50)
      expect(box?.height ?? 0, `第 ${i + 1} 张图（${expected[i]?.title}）的 canvas 高度是 0`).toBeGreaterThan(50)
    }

    // ---- 3. 没有横向溢出 ----
    // ECharts 在容器宽度还没算出来时初始化，就会按 100px 的默认宽度画，
    // 然后撑破布局。这在 jsdom 里同样测不出来（没有布局）。
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    )
    expect(overflow, `页面横向溢出了 ${overflow}px`).toBeLessThanOrEqual(1)

    // ---- 4. 截图存档 ----
    // 这一条是给**人**看的，不是断言。作品集里那张报告页截图就该从这里出，
    // 而不是手工截——手工截图会随着页面改动渐渐变成一张过期的东西。
    //
    // **不加 `fullPage: true`。** 这个页面是 `h-screen` + 中间那栏自己滚，
    // 文档本身没有溢出，所以 `fullPage` 截出来和视口一模一样——
    // 一个看起来在截整页、实际只截了首屏的参数，比不写它更容易误导人。
    // 要更长的图，就照下面那样**指名截哪一块**。
    await page.screenshot({ path: 'e2e/.artifacts/report-top.png' })

    // 图表那一块单独来一张：它是这个文件存在的理由，而它在首屏之下。
    // `scrollIntoViewIfNeeded` 会把它滚进视口，再截元素本身——
    // 这样"四张图都画出来了"这件事有一张能直接看的证据，
    // 而不是只有一个 canvas 计数。
    const chartsPanel = page.locator('#charts')
    await chartsPanel.scrollIntoViewIfNeeded()
    await chartsPanel.screenshot({ path: 'e2e/.artifacts/report-charts.png' })

    expect(errors, `控制台有报错：\n${errors.join('\n')}`).toEqual([])
  })
})

/**
 * 拦掉所有后端请求。
 *
 * **宽匹配 `**\/api/**` 而不是只拦那一个报告接口**：页面上的组件以后
 * 多调一个接口（比如成本面板），只拦一个的话那个请求会打到
 * `vite preview` 上拿到 404，页面显示一句"加载失败"——
 * 而这条测试会以"某个标题不可见"的方式红，指向一个不是原因的地方。
 * 拦全部、默认给空的，新接口的后果就只是"那块空着"，一眼看得出来。
 */
async function mockApi(page: import('@playwright/test').Page): Promise<void> {
  await page.route('**/api/**', async (route) => {
    const url = route.request().url()
    if (/\/api\/reports\/[^/?]+$/.test(url)) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          reportId: REPORT_ID,
          taskId: REAL_REPORT_BODY.taskId,
          data: REAL_REPORT_BODY,
          feedback: [],
          feedbackCount: 0,
        }),
      })
      return
    }
    // 列表类给空数组、详情类给空对象。给 `{}` 而不是 404：
    // 404 会让页面进错误分支，于是这条测试变成在测错误页。
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([]),
    })
  })
  await page.route('**/health', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ status: 'ok', provider: 'e2e' }),
    }),
  )
}
