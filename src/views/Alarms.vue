<template>
  <div class="alarms-page">
    <el-row :gutter="16" class="mb-16">
      <el-col :span="6">
        <el-card shadow="hover">
          <div class="stat">
            <div class="stat-label">活跃报警</div>
            <div class="stat-value text-danger">{{ stats.active || 0 }}</div>
          </div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="hover">
          <div class="stat">
            <div class="stat-label">今日总计</div>
            <div class="stat-value">{{ stats.total || 0 }}</div>
          </div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="hover">
          <div class="stat">
            <div class="stat-label">已确认</div>
            <div class="stat-value text-success">{{ stats.acknowledged || 0 }}</div>
          </div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="hover">
          <div class="stat">
            <div class="stat-label">严重报警</div>
            <div class="stat-value text-danger">{{ stats.by_level?.critical || 0 }}</div>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <el-card shadow="hover">
      <template #header>
        <div class="card-header">
          <span>报警记录</span>
          <el-button @click="refreshAlarms"><el-icon><Refresh /></el-icon> 刷新</el-button>
        </div>
      </template>

      <el-table :data="alarms" stripe v-loading="loading">
        <el-table-column prop="timestamp" label="时间" width="180" />
        <el-table-column prop="device_id" label="设备" width="150" />
        <el-table-column prop="alarm_level" label="等级" width="100">
          <template #default="{ row }">
            <el-tag :type="row.alarm_level === 'critical' ? 'danger' : row.alarm_level === 'warning' ? 'warning' : 'info'" size="small">
              {{ row.alarm_level }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="alarm_message" label="报警信息" />
        <el-table-column prop="actual_value" label="实际值" width="100" />
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="row.acknowledged ? 'success' : 'danger'" size="small">
              {{ row.acknowledged ? '已确认' : '未确认' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="100">
          <template #default="{ row }">
            <el-button v-if="!row.acknowledged" type="primary" link size="small" @click="acknowledge(row.id)">
              确认
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { alarmsApi, type Alarm } from '@/api'

const alarms = ref<Alarm[]>([])
const stats = ref<any>({})
const loading = ref(false)

onMounted(() => { refreshAlarms() })

async function refreshAlarms() {
  loading.value = true
  try {
    const [alarmData, statsData] = await Promise.all([alarmsApi.getAll(), alarmsApi.getStatistics()])
    alarms.value = alarmData.alarms || []
    stats.value = statsData || {}
  } catch { /* ignore */ }
  finally { loading.value = false }
}

async function acknowledge(id: string) {
  try {
    await alarmsApi.acknowledge(id)
    ElMessage.success('报警已确认')
    refreshAlarms()
  } catch { /* ignore */ }
}
</script>

<style scoped>
.mb-16 { margin-bottom: 16px; }
.stat { text-align: center; }
.stat-label { font-size: 14px; color: #909399; margin-bottom: 8px; }
.stat-value { font-size: 28px; font-weight: bold; }
.text-danger { color: #f56c6c; }
.text-success { color: #67c23a; }
.card-header { display: flex; align-items: center; justify-content: space-between; }
</style>
