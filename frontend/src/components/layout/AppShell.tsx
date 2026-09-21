/**
 * 带侧边栏的外壳。**只包"日常翻看"那五页。**
 *
 * 澄清页、工作台、报告页刻意**不在里面**（见 `App.tsx` 的路由分组）：
 * 那几页是一次只做一件事的场景——跑调研时侧边栏只会占掉
 * 十分之一的宽度，而工作台本来就紧。给它们套上导航是为了让界面
 * 看起来"更完整"，代价是每次都要在更小的区域里读证据流。
 *
 * 布局用 `h-screen + overflow-hidden` 而不是 `min-h-screen`：
 * 侧边栏要**钉住**而不是跟着内容滚——它上面是导航、下面是工作空间统计，
 * 滚到一半看不见导航，用户就得先滚回去才能换页。
 */
import { Outlet } from 'react-router-dom'

import { Sidebar } from './Sidebar'

export function AppShell() {
  return (
    <div className="flex h-screen overflow-hidden bg-canvas">
      <Sidebar />
      {/* `min-w-0` 是必须的：flex 子项默认 `min-width:auto`，
          内容一宽（比如证据表格）就会把侧边栏挤扁，而不是自己出滚动条。 */}
      <main className="min-w-0 flex-1 overflow-y-auto">
        <Outlet />
      </main>
    </div>
  )
}
