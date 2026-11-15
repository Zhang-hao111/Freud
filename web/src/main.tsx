import './dsh/design-platform.css'
import { createRoot } from 'react-dom/client'
import { App } from './app/App.js'

const root = document.getElementById('root')
if (!root) throw new Error('找不到网页挂载节点')
createRoot(root).render(<App />)
