<template>
  <div class="config-page">
    <el-card shadow="hover">
      <template #header><span>系统配置</span></template>

      <el-descriptions :column="2" border class="mb-16">
        <el-descriptions-item label="系统版本">v1.0.0</el-descriptions-item>
        <el-descriptions-item label="运行模式">
          <el-tag :type="status?.simulation_mode ? 'warning' : 'success'" size="small">
            {{ status?.simulation_mode ? '模拟模式' : '实时模式' }}
          </el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="设备总数">{{ status?.devices_total || 0 }}</el-descriptions-item>
        <el-descriptions-item label="在线设备">{{ status?.devices_connected || 0 }}</el-descriptions-item>
        <el-descriptions-item label="数据采集器">
          <el-tag :type="status?.data_collector_running ? 'success' : 'danger'" size="small">
            {{ status?.data_collector_running ? '运行中' : '已停止' }}
          </el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="活跃报警">{{ status?.alarms_active || 0 }}</el-descriptions-item>
      </el-descriptions>

      <el-divider />

      <h4>数据库信息</h4>
      <el-table :data="dbTables" stripe>
        <el-table-column prop="name" label="表名" />
        <el-table-column prop="rows" label="记录数" width="120" />
        <el-table-column prop="size" label="大小" width="120" />
      </el-table>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import { systemApi, type SystemStatus, type DatabaseInfo } from '@/api'

const status = ref<SystemStatus | null>(null)
const dbInfo = ref<DatabaseInfo | null>(null)

const dbTables = computed(() => {
  if (!dbInfo.value?.tables) return []
  return Object.entries(dbInfo.value.tables).map(([name, info]) => ({
    name,
    rows: info.rows,
    size: info.size,
  }))
})

onMounted(async () => {
  try {
    const [s, d] = await Promise.all([systemApi.getStatus(), systemApi.getDatabase()])
    status.value = s
    dbInfo.value = d
  } catch { /* ignore */ }
})
</script>

<style scoped>
.mb-16 { margin-bottom: 16px; }
</style>
