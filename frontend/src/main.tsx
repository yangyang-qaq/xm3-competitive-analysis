import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import App from './App.tsx'
import './index.css'

const container = document.getElementById('root')
if (!container) {
  throw new Error('找不到挂载点 #root——index.html 被改坏了？')
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
