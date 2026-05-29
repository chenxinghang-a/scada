import { defineStore } from 'pinia'
import { ref } from 'vue'
import { systemApi, type SystemStatus } from '@/api'

export const useAppStore = defineStore('app', () => {
  const sidebarCollapsed = ref(false)
  const systemStatus = ref<SystemStatus | null>(null)
  const simulationMode = ref(false)
  const activeAlarmCount = ref(0)

  function toggleSidebar() {
    sidebarCollapsed.value = !sidebarCollapsed.value
  }

  async function fetchSystemStatus() {
    try {
      systemStatus.value = await systemApi.getStatus()
      simulationMode.value = systemStatus.value.simulation_mode
      activeAlarmCount.value = systemStatus.value.alarms_active
    } catch {
      // 忽略，可能未登录
    }
  }

  async function fetchSimulationMode() {
    try {
      const data = await systemApi.getSimulationMode()
      simulationMode.value = data.simulation_mode
    } catch {
      // ignore
    }
  }

  return {
    sidebarCollapsed,
    systemStatus,
    simulationMode,
    activeAlarmCount,
    toggleSidebar,
    fetchSystemStatus,
    fetchSimulationMode,
  }
})
