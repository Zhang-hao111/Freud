import { useSyncExternalStore } from 'react'

type Theme = 'light' | 'dark'
const key = 'freud:theme'
const systemTheme = window.matchMedia('(prefers-color-scheme: dark)')
let preference: Theme | undefined
const listeners = new Set<() => void>()

try {
  const stored = localStorage.getItem(key)
  if (stored === 'light' || stored === 'dark') preference = stored
} catch {
  // 浏览器可能禁用本地存储。
}

function getTheme(): Theme {
  return preference ?? (systemTheme.matches ? 'dark' : 'light')
}

function synchronize() {
  const theme = getTheme()
  document.documentElement.style.colorScheme = theme
  document.body.toggleAttribute('data-ds-dark-theme', theme === 'dark')
  document.body.classList.toggle('dark', theme === 'dark')
  listeners.forEach(listener => listener())
}

function subscribe(listener: () => void) {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}

export function useTheme() {
  return useSyncExternalStore(subscribe, getTheme)
}

export function setThemePreference(theme: Theme) {
  preference = theme
  try {
    localStorage.setItem(key, theme)
  } catch {
    // 当前页面仍可切换主题。
  }
  synchronize()
}

synchronize()
systemTheme.addEventListener('change', synchronize)
