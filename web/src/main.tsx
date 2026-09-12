import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import App from './App'
import './index.css'

const container = document.getElementById('root')
if (!container) {
  throw new Error('找不到挂载节点 #root，index.html 可能被改坏了')
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
