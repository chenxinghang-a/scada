/// <reference types="vite/client" />

declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<{}, {}, any>
  export default component
}

interface ElectronAPI {
  getAppVersion: () => Promise<string>
  getBackendStatus: () => Promise<{ running: boolean; port: number }>
  onBackendLog: (callback: (data: string) => void) => void
}

declare global {
  interface Window {
    electronAPI?: ElectronAPI
  }
}
