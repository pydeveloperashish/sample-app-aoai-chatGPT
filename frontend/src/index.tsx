import React from 'react'
import ReactDOM from 'react-dom/client'
import { HashRouter, Route, Routes } from 'react-router-dom'
import { initializeIcons } from '@fluentui/react'

import Chat from './pages/chat/Chat'
import Layout from './pages/layout/Layout'
import NoPage from './pages/NoPage'
import { AppStateProvider } from './state/AppProvider'

import './index.css'

initializeIcons("https://res.cdn.office.net/files/fabric-cdn-prod_20241209.001/assets/icons/")

// Check user's dark mode preference
const prefersDarkMode = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
const savedTheme = localStorage.getItem('app-theme');
const isDarkMode = savedTheme ? savedTheme === 'dark' : prefersDarkMode;

// Set dark mode class on root element
if (isDarkMode) {
  document.documentElement.classList.add('dark-mode');
} else {
  document.documentElement.classList.remove('dark-mode');
}

// Add theme toggle function to window for global access
(window as any).toggleTheme = () => {
  const isDark = document.documentElement.classList.contains('dark-mode');
  if (isDark) {
    document.documentElement.classList.remove('dark-mode');
    localStorage.setItem('app-theme', 'light');
  } else {
    document.documentElement.classList.add('dark-mode');
    localStorage.setItem('app-theme', 'dark');
  }
};

export default function App() {
  return (
    <AppStateProvider>
      <HashRouter>
        <Routes>
          <Route path="/" element={<Layout />}>
            <Route index element={<Chat />} />
            <Route path="*" element={<NoPage />} />
          </Route>
        </Routes>
      </HashRouter>
    </AppStateProvider>
  )
}

ReactDOM.createRoot(document.getElementById('root') as HTMLElement).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
)
