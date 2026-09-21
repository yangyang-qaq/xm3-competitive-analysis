/**
 * 路由分两组：
 *
 * - **侧边栏组**（`/` `/library` `/knowledge` `/experts` `/experts/:expertId`
 *   `/dashboard`）——日常翻看，共用一个带导航的外壳（`AppShell`）。
 * - **全屏沉浸组**（`/clarify/:taskId` `/workspace/:taskId` `/report/:reportId`
 *   `/trace/:taskId` `/graph/:reportId`）——一次只做一件事，侧边栏在那里只会占地方。
 *   工作台的证据流本来就挤成三栏，报告页与回放页也是，再切掉 240px 是实打实的损失。
 *
 * 分组不是按"页面重不重要"分的，而是按**这一页要待多久**：
 * 翻看型页面之间来回跳，需要导航；跑调研、读一份报告是一次性的连续动作，
 * 中间跳走等于放弃。
 *
 * `/report/:reportId` 曾经**故意不接**（工作台上那个链接指向一个不存在的
 * 路由，是记在 `问题记录.md` 里的欠账）：接一个只能显示半份报告的空壳
 * 比不接更坏，用户会以为报告页就是这样。现在报告页做完了，路由补上，
 * 那条欠账销掉。
 */
import { BrowserRouter, Route, Routes } from 'react-router-dom'

import { AppShell } from './components/layout/AppShell'
import ClarifyPage from './pages/ClarifyPage'
import DashboardPage from './pages/DashboardPage'
import ExpertDetailPage from './pages/ExpertDetailPage'
import ExpertsPage from './pages/ExpertsPage'
import GraphPage from './pages/GraphPage'
import HomePage from './pages/HomePage'
import KnowledgePage from './pages/KnowledgePage'
import LibraryPage from './pages/LibraryPage'
import NotFoundPage from './pages/NotFoundPage'
import ReportPage from './pages/ReportPage'
import TracePage from './pages/TracePage'
import WorkspacePage from './pages/WorkspacePage'

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        {/* 侧边栏组 */}
        <Route element={<AppShell />}>
          <Route path="/" element={<HomePage />} />
          <Route path="/library" element={<LibraryPage />} />
          <Route path="/knowledge" element={<KnowledgePage />} />
          <Route path="/experts" element={<ExpertsPage />} />
          {/* 专家档案挂在**侧边栏组**里，不是沉浸组：它是翻看型页面——
              从名册点进来、看完再点回去，中间还会顺手开几份他参与过的报告。 */}
          <Route path="/experts/:expertId" element={<ExpertDetailPage />} />
          <Route path="/dashboard" element={<DashboardPage />} />
        </Route>

        {/* 全屏沉浸组 */}
        <Route path="/clarify/:taskId" element={<ClarifyPage />} />
        <Route path="/workspace/:taskId" element={<WorkspacePage />} />
        <Route path="/report/:reportId" element={<ReportPage />} />
        {/* 决策回放挂在**任务**下，不是报告下：一条时间线属于一次运行，
            而报告是那次运行的产物。`?report=` 可选，带上它右栏才有证据可对照
            （证据列表在报告正文里，没有任务级的取法）。 */}
        <Route path="/trace/:taskId" element={<TracePage />} />
        {/* 知识图谱挂在**报告**下：图上的每一个数都从报告正文里来
            （品牌名单、计划维度、证据的 matchedDimensions），
            没有报告就没有图，换一份报告就是另一张图。 */}
        <Route path="/graph/:reportId" element={<GraphPage />} />
        <Route path="*" element={<NotFoundPage />} />
      </Routes>
    </BrowserRouter>
  )
}
