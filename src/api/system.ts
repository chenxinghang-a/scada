import api from './request'

export interface SystemStatus {
  uptime: number
  devices_total: number
  devices_connected: number
  alarms_active: number
  data_collector_running: boolean
  simulation_mode: boolean
}

export interface DatabaseInfo {
  size: number
  tables: Record<string, { rows: number; size: string }>
}

export const systemApi = {
  getStatus() {
    return api.get('/system/status') as Promise<SystemStatus>
  },

  getDatabase() {
    return api.get('/system/database') as Promise<DatabaseInfo>
  },

  getSimulationMode() {
    return api.get('/system/simulation-mode') as Promise<{ simulation_mode: boolean }>
  },

  getHealth() {
    return api.get('/health') as Promise<{ status: string; checks: Record<string, boolean> }>
  },
}
