import '@testing-library/jest-dom/vitest'

import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'

// **自动清理不能省。** `@testing-library/react` 只在**全局** `afterEach`
// 存在时才自己注册清理，而这里的 vitest 配置没开 `globals`
// （每个测试文件都显式从 `vitest` 引入）。
//
// 不注册的表现很绕：同一份 DOM 在用例之间累积，于是第二个渲染组件的用例
// 报 `Found multiple elements with the text: ...`——看起来像组件渲染了两次，
// 而组件毫无问题。第一条用例还照样是绿的，因为那时只挂了一个。
afterEach(cleanup)
