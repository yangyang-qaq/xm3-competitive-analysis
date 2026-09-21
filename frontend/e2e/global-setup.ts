/**
 * 跑 e2e 之前**先构建**。
 *
 * 为什么这件事非得在代码里做，而不是靠"记得先 build"
 * --------------------------------------------------
 * `playwright.config.ts` 起的 `vite preview` 服务的是 `dist/`，
 * 也就是**上一次构建的产物**。而 `npx playwright test` 不会自己构建。
 * 于是：
 *
 *     改了 src/ → 直接 npx playwright test → 全绿
 *
 * 这个绿是**旧代码**的绿。它不是"测试没覆盖到"，是"测试根本没在看新代码"，
 * 而输出和真绿一模一样。
 *
 * 这不是假想的，本文件就是被这件事逼出来的。反证的时候：把报告页里的
 * `<ReportCharts>` 摘掉、直接跑 `npx playwright test`——
 * 那条"四张图必须都画出来"的测试**照样通过**。原因是 `dist/` 里还是
 * 更早那次 `npm run test:e2e` 构建出来的产物（图表还在的那一版），
 * 于是这次跑的是旧代码，而它当然是绿的。
 *
 * 反证的意义就在这里：**反证失败了，而失败的样子看起来像成功**。
 *
 * 所以构建放在 `globalSetup` 而不是 `webServer.command`：
 * `webServer` 那句带着 `reuseExistingServer`（本地已经在跑 preview 时直接复用），
 * 一旦复用就**整条命令都不执行**，构建也跟着没了。
 * `globalSetup` 每次都跑，不存在"这次跳过"的分支。
 *
 * 代价是每次跑 e2e 多约 4 秒。换掉的是一整类"绿得没有意义"的结果。
 */
import { execSync } from 'node:child_process'

export default function globalSetup(): void {
  // 用 `execSync` 走一条命令字符串，而不是 `execFileSync('npm', [...])`。
  //
  // 后者在 Windows 上跑不了：`npm` 是 `npm.cmd`，而 Node 从 20 起
  // 不允许不带 shell 地 spawn 一个 `.cmd`，实测报 `spawnSync npm.cmd EINVAL`。
  // 加上 `shell: true` 能跑，但会换来一条 DEP0190（args 拼接不转义），
  // 每次跑测试都出现——那种警告最后会变成没人再看的背景噪音。
  // 命令字符串这条路两个问题都没有。
  execSync('npm run build', { stdio: 'inherit' })
}
