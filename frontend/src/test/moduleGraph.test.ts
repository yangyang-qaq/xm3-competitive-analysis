/**
 * 模块图的两条边：**进去的**和**出去的**。
 *
 * - **进去的**：`src/` 里的文件，有没有人引用它（第 1 条 `it`）；
 * - **出去的**：`src/` 里 import 的包，`package.json` 里声明了没有（第 2、3 条 `it`）。
 *
 * 它们是同一个缺陷的两面——**图里有一条边，而该知道它的地方不知道**。
 * 上一次是"文件在、没人引"（问题 41），这一次是"包在用、没声明"（问题 46）。
 *
 * ---
 *
 * 第一半：没有"写好了却没人引用"的模块。
 *
 * 这条测试守的是 `问题记录.md` 41 那个缺陷：
 *
 *     `ReportChart.tsx` 写得相当完整（按需引入 ECharts、ResizeObserver、
 *     画不出来时给一句说明，连它修过的 bug 都记在文件头），
 *     而**没有任何地方 import 它**。于是报告页上四张图一张都不显示。
 *
 * 而它当时没被任何测试发现，因为 `ReportPage.test.tsx` 里有一句
 * `vi.mock('../components/report/ReportChart', ...)`——mock 的是一个
 * 页面根本没引用的模块，也没有断言查那个假组件的产出。
 *
 * 为什么这类缺陷要靠一条**独立于被测代码**的检查
 * --------------------------------------------
 * 上面两种"看不见"有一个共同结构：**问那个东西本身，它说自己很好**。
 * 组件有注释、有测试提到、看起来像做完了。要发现它，只能换个问题问：
 * "谁 import 它？"——那是一个遍历模块图能回答的问题，与被检查的代码
 * 写得好不好完全无关。
 *
 * 为什么用 `import.meta.glob` 而不是 `node:fs`
 * ------------------------------------------
 * 读文件本来要 `node:fs` / `node:path`，但 `tsconfig.app.json` 的
 * `types` 只开了 `["vite/client"]`——**那是刻意的**，它让"在浏览器代码里
 * 用了 `process`"变成一个编译错误。为了这一条测试去开 `node` 类型，
 * 等于给全部业务代码放行 Node 全局，代价比收益大。
 *
 * `import.meta.glob` 是 Vite 自己的功能，构建时就把源码文本收进来了，
 * 类型也在 `vite/client` 里，一个 Node API 都不用。
 *
 * 为什么只在前端立这条
 * ------------------
 * 后端也扫过一遍（83 个文件，0 个孤儿），但后端有注册表/装饰器这类
 * "约定式引用"，写进去会有误报——而**会误报的闸门很快就会被绕过**。
 * 前端的引用全是引号里的字面量路径（含 `import('./X')` 这种懒加载），
 * 判断是确定的。
 *
 * **已经反证过**：把报告页里 `ReportCharts` 的 import 摘掉，这条立刻红，
 * 并指名 `components/report/ReportCharts.tsx`。
 *
 * 同一次反证还确认了一件想确认的事：报告页里有**两处注释**提到了
 * `ReportCharts`，它们**没有被算成引用**。这是刻意的——判据是
 * "引号里含斜杠的字面量路径"。要是注释能算引用，那么写句注释
 * 就能让这道闸门变绿，它就废了。
 */
import { describe, expect, it } from 'vitest'

/** 所有非测试源码的文本。键形如 `/src/pages/ReportPage.tsx`。 */
const SOURCES = import.meta.glob('/src/**/*.{ts,tsx}', {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>

/**
 * 允许"没人 import"的文件，**按相对 `src/` 的路径写**。
 *
 * **每一条都要写清为什么**，因为这个名单每长一条，这条测试就弱一分。
 * 一个只写豁免不写理由的名单，最后会变成"以前红过的东西都往里塞"。
 *
 * 按路径而不是按文件名：文件名会撞（多个 `index.ts`），
 * 而且路径能一眼看出豁免的是哪一份。
 */
const ALLOWED: Record<string, string> = {
  'main.tsx': '入口，由 index.html 引用，不进模块图',
}

/**
 * 整目录豁免。**只给"测试夹具"这一类**，理由是真的：
 * 判断"谁引用它"时只看非测试文件，所以一个只被 `.test.tsx` 引用的
 * 夹具必然被判成孤儿——这不是它有问题，是这条检查的视野外。
 *
 * 没有为某几个夹具单个开豁免：夹具会增删，逐个列必然过期，
 * 而过期的名单会让人以为"新加的夹具也检查过了"。
 */
const ALLOWED_PREFIXES: Array<[string, string]> = [
  ['test/', '测试夹具，只被 .test 文件引用，而 .test 不在引用方集合里'],
]

/** `/src/components/report/ReportChart.tsx` → `components/report/ReportChart.tsx` */
function relativeToSrc(path: string): string {
  return path.replace(/^\/src\//, '')
}

/** `/src/components/report/ReportChart.tsx` → `ReportChart` */
function nameOf(path: string): string {
  return (path.split('/').pop() ?? '').replace(/\.tsx?$/, '')
}

/**
 * `package.json` 的原文。
 *
 * 同样走 `import.meta.glob` 而不是 `import pkg from '../../package.json'`：
 * 后者要开 `resolveJsonModule`，而 JSON 模块一旦能直接 import，
 * 别处也可能开始 import 配置——那是个比这条测试大得多的口子。
 * 这里只要一份**文本**，`?raw` 正好。
 */
const PACKAGE_FILES = import.meta.glob('/package.json', {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>

/**
 * 取出代码里**裸**的模块说明符（包名），相对路径一律丢掉。
 *
 * `import.meta.glob('/src/**')` 是对这条的一个真实考验：它同时含
 * `import`、含引号参数、而且后面还跟着一个对象字面量。
 * 四条正则各自靠什么避开它：
 *   - `(?!\.meta)`：`import.meta` 直接不匹配；
 *   - `[^;'"`]*?`：不允许 import 与 `from` 之间出现引号，
 *     于是跨不过 `glob('...')` 的那个引号，也就找不到 `from`；
 *   - 动态 import 那条要求 `import` 紧跟 `(`，而这个写法中间隔着 `.meta.glob`。
 *
 * 注释里的 `import 'x'` 必须先剥掉：不剥的话，写一句注释就能让
 * 未声明的包"消失"，这条测试就废了（与上面那条同一条原则）。
 */
function collectSpecifiers(sources: Record<string, string>): string[] {
  const found: string[] = []
  for (const text of Object.values(sources)) {
    const code = text
      .replace(/\/\*[\s\S]*?\*\//g, ' ')
      .replace(/^\s*\/\/.*$/gm, ' ')
    for (const re of [IMPORT_FROM, IMPORT_SIDE_EFFECT, EXPORT_FROM, DYNAMIC_IMPORT]) {
      for (const match of code.matchAll(re)) {
        const target = match[1] ?? ''
        // 相对路径、绝对路径、Vite 的虚拟模块都不是"包"。
        if (!target || target.startsWith('.') || target.startsWith('/')) continue
        found.push(packageNameOf(target))
      }
    }
  }
  return found
}

const IMPORT_FROM = /(?:^|[\s;{}])import(?!\.meta)[^;'"`]*?from\s*['"]([^'"`]+)['"]/g
const IMPORT_SIDE_EFFECT = /(?:^|[\s;{}])import(?!\.meta)\s*['"]([^'"`]+)['"]/g
const EXPORT_FROM = /(?:^|[\s;{}])export[^;'"`]*?from\s*['"]([^'"`]+)['"]/g
const DYNAMIC_IMPORT = /\bimport\s*\(\s*['"]([^'"`]+)['"]/g

/**
 * `@testing-library/jest-dom/vitest` → `@testing-library/jest-dom`
 * `react/jsx-runtime` → `react`
 * `zustand` → `zustand`
 */
function packageNameOf(specifier: string): string {
  const parts = specifier.split('/')
  return specifier.startsWith('@') ? parts.slice(0, 2).join('/') : (parts[0] ?? '')
}

function exemptReason(relative: string): string | undefined {
  if (relative in ALLOWED) return ALLOWED[relative]
  return ALLOWED_PREFIXES.find(([prefix]) => relative.startsWith(prefix))?.[1]
}

describe('模块图', () => {
  it('src/ 下没有写好了却没人引用的模块', () => {
    const files = Object.keys(SOURCES).filter((path) => !/\.test\.tsx?$/.test(path))
    // 这个下限是给这条测试自己用的：哪天 glob 写错、目录挪了，
    // `files` 变空会让下面的断言**空转通过**——一条什么都不检查的
    // 测试比没有测试更容易骗人。
    expect(files.length, '没扫到任何源文件，这条测试失去意义').toBeGreaterThan(30)

    const orphans: string[] = []
    for (const file of files) {
      const relative = relativeToSrc(file)
      if (exemptReason(relative) !== undefined) continue

      const name = nameOf(file)
      // 引用形式：`'./X'`、`'../a/X'`、`import('./X')` —— 都是引号里的字面量路径。
      // 不用 AST：这里的规律足够简单，为一条测试引入 TS 解析器不值当
      // （依赖表要保持最小）。
      const pattern = new RegExp(`['"\`][^'"\`]*[/]${name}(\\.[jt]sx?)?['"\`]`)
      const users = files.filter(
        (other) => other !== file && pattern.test(SOURCES[other] ?? ''),
      )
      if (users.length === 0) orphans.push(relative)
    }

    expect(
      orphans,
      `下面这些文件没有任何地方引用。要么接上，要么加进 ALLOWED 并写清理由：\n  ${orphans.join('\n  ')}`,
    ).toEqual([])
  })

  /**
   * 上面那条问的是"src/ 内部的边"，这条问的是"通向 src/ 外面的边"。
   *
   * 它们是同一个缺陷的两面：**模块图里存在一条边，而声明它的地方不知道**。
   * 上一次是"文件在、没人引"，这一次是"包在用、没声明"。
   *
   * 为什么必须是一条测试
   * -------------------
   * `import { forceSimulation } from 'd3-force'` 而 `package.json` 里
   * **没有 `d3-force`** 时，下面五件事**全部正常**：
   *
   *   tsc 过（`node_modules/d3-force` 确实在）、oxlint 过、vitest 过、
   *   `vite build` 过、CI 全绿。
   *
   * 它之所以还在，纯粹因为 `d3@7` 恰好也依赖 `d3-force`，
   * 于是 npm 把它提升到了 `node_modules` 顶层。
   * **能用，是因为另一个包的依赖树碰巧把它带进来了。**
   *
   * 三种情况会让它消失，且都不报"缺依赖"这种错：
   *   1. 换 pnpm / `npm ci --install-strategy=nested`（不提升）；
   *   2. `d3@8` 不再依赖 `d3-force`；
   *   3. 有人为了瘦身把 `d3` 删掉——**这一条就是本次真实发生的事**，
   *      删掉元包的瞬间，那个已经在用的包失去了唯一的来源。
   *
   * 判据
   * ----
   * 扫 `src/` 全部源码，取出**裸**说明符（不以 `.` 或 `/` 开头），
   * 取包名（`@scope/name` 取前两段，其余取第一段），
   * 断言它在 `dependencies` / `devDependencies` / `peerDependencies` /
   * `optionalDependencies` 里出现过。
   *
   * 刻意不做的事：**不做版本检查**。那需要一套 semver 实现，
   * 而这条要防的是"这个包压根没被声明"，不是"声明的范围松了"。
   */
  it('src/ 里 import 的每一个包都在 package.json 里声明了', () => {
    const raw = Object.values(PACKAGE_FILES)[0]
    expect(raw, '没读到 package.json，这条测试失去意义').toBeTypeOf('string')
    const pkg = JSON.parse(raw as string) as Record<string, Record<string, string>>
    const declared = new Set(
      ['dependencies', 'devDependencies', 'peerDependencies', 'optionalDependencies'].flatMap(
        (field) => Object.keys(pkg[field] ?? {}),
      ),
    )

    const specifiers = collectSpecifiers(SOURCES)
    // 防空转：glob 写错或目录挪了都会让下面这个循环变成空转。
    expect(
      specifiers.length,
      '一个裸说明符都没扫到，像是解析器坏了而不是代码里没有 import',
    ).toBeGreaterThan(5)

    const undeclared = [...new Set(specifiers)].filter((name) => !declared.has(name))
    expect(
      undeclared.sort(),
      '下面这些包被 import 了，但 package.json 里没有声明它们。\n' +
        '现在能跑，只可能是因为别的包的依赖树把它们带进来了——\n' +
        '换一次安装方式或上游改一次依赖就会断，而且是"找不到模块"这种\n' +
        '看起来像手滑的错。直接装上（`npm i <包名>`）或改成从已声明的包引入：\n  ' +
        undeclared.join('\n  '),
    ).toEqual([])
  })

  /**
   * 给上面那条做反证：**证明它真的会红**。
   *
   * 喂一段文本，里面一条已声明、一条未声明、一条相对路径、
   * 一条 `import.meta.glob`（它的参数里也有引号）、一条注释里的假 import。
   * 解析器必须只把前两条里的**裸**说明符交出来。
   */
  it('扫描器认得出未声明的包', () => {
    const text = `
import { create } from 'zustand'
import { forceSimulation } from 'd3-force-不存在'
import { a } from './local'
import type { B } from '../types/b'
// import { c } from '这条在注释里，不算'
const mods = import.meta.glob('/src/**/*.ts', { query: '?raw' })
const lazy = await import('echarts')
export { d } from 'react'
import 'side-effect-pkg'
`
    expect(collectSpecifiers({ '/x.ts': text }).sort()).toEqual([
      'd3-force-不存在',
      'echarts',
      'react',
      'side-effect-pkg',
      'zustand',
    ])
  })
})
