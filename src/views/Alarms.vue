<template>
  <div class="alarms-page">
    <!-- 统计卡片 -->
    <el-row :gutter="16" class="mb-16">
      <el-col :span="6">
        <el-card shadow="hover"><div class="stat"><div class="stat-label">报警总数</div><div class="stat-value">{{ stats.total || 0 }}</div></div></el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="hover"><div class="stat"><div class="stat-label">未确认</div><div class="stat-value text-danger">{{ stats.active || 0 }}</div></div></el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="hover"><div class="stat"><div class="stat-label">严重报警</div><div class="stat-value text-danger">{{ stats.by_level?.critical || 0 }}</div></div></el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="hover"><div class="stat"><div class="stat-label">已确认</div><div class="stat-value text-success">{{ stats.acknowledged || 0 }}</div></div></el-card>
      </el-col>
    </el-row>

    <!-- 筛选 + 列表 -->
    <el-card shadow="hover">
      <template #header>
        <div class="card-header">
          <span>报警记录</span>
          <div>
            <el-select v-model="filter.device_id" placeholder="全部设备" clearable style="width:150px" class="mr-8">
              <el-option v-for="d in devices" :key="d.device_id" :label="d.device_name" :value="d.device_id" />
            </el-select>
            <el-select v-model="filter.alarm_level" placeholder="全部等级" clearable style="width:120px" class="mr-8">
              <el-option label="严重" value="critical" />
              <el-option label="警告" value="warning" />
              <el-option label="信息" value="info" />
            </el-select>
            <el-select v-model="filter.status" placeholder="全部状态" clearable style="width:120px" class="mr-8">
              <el-option label="未确认" value="unacknowledged" />
              <el-option label="已确认" value="acknowledged" />
            </el-select>
            <el-button @click="refreshAlarms"><el-icon><Refresh /></el-icon> 刷新</el-button>
          </div>
        </div>
      </template>

      <el-table :data="alarms" stripe v-loading="loading" max-height="600">
        <el-table-column prop="timestamp" label="时间" width="180">
          <template #default="{ row }">{{ new Date(row.timestamp).toLocaleString() }}</template>
        </el-table-column>
        <el-table-column prop="device_id" label="设备" width="140" show-overflow-tooltip />
        <el-table-column prop="register_name" label="参数" width="120" show-overflow-tooltip />
        <el-table-column prop="alarm_level" label="等级" width="80">
          <template #default="{ row }">
            <el-tag :type="row.alarm_level === 'critical' ? 'danger' : row.alarm_level === 'warning' ? 'warning' : 'info'" size="small" effect="dark">
              {{ row.alarm_level === 'critical' ? '严重' : row.alarm_level === 'warning' ? '警告' : '信息' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="alarm_message" label="报警信息" show-overflow-tooltip />
        <el-table-column prop="threshold" label="阈值" width="80" />
        <el-table-column prop="actual_value" label="实际值" width="80">
          <template #default="{ row }">{{ row.actual_value?.toFixed(2) }}</template>
        </el-table-column>
        <el-table-column label="状态" width="80">
          <template #default="{ row }">
            <el-tag :type="row.acknowledged ? 'success' : 'danger'" size="small">{{ row.acknowledged ? '已确认' : '未确认' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="100" fixed="right">
          <template #default="{ row }">
            <el-button v-if="!row.acknowledged" type="primary" link size="small" @click="acknowledge(row.id)">确认</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, reactive } from 'vue'
import { ElMessage } from 'element-plus'
import { alarmsApi, devicesApi, type Alarm, type Device } from '@/api'

const alarms = ref<Alarm[]>([])
const devices = ref<Device[]>([])
const stats = ref<any>({})
const loading = ref(false)
const filter = reactive({ device_id: '', alarm_level: '', status: '' })

onMounted(() => { refreshAlarms(); loadDevices() })

async function loadDevices() {
  try { const data = await devicesApi.getAll(); devices.value = data.devices || [] } catch { /* ignore */ }
}

async function refreshAlarms() {
  loading.value = true
  try {
    const params: any = { limit: 100 }
    if (filter.device_id) params.device_id = filter.device_id
    if (filter.alarm_level) params.alarm_level = filter.alarm_level
    const [alarmData, statsData] = await Promise.all([alarmsApi.getAll(params), alarmsApi.getStatistics()])
    alarms.value = (alarmData.alarms || []).filter((a: any) => {
      if (filter.status === 'unacknowledged') return !a.acknowledged
      if (filter.status === 'acknowledged') return a.acknowledged
      return true
    })
    stats.value = statsData || {}
  } catch { /* ignore */ }
  finally { loading.value = false }
}

async function acknowledge(id: string) {
  try { await alarmsApi.acknowledge(id); ElMessage.success('报警已确认'); refreshAlarms() } catch { /* ignore */ }
}
</script>

<style scoped>
.mb-16 { margin-bottom: 16px; }
.stat { text-align: center; }
.stat-label { font-size: 14px; color: #909399; margin-bottom: 8px; }
.stat-value { font-size: 28px; font-weight: bold; }
.text-danger { color: #f56c6c; }
.text-success { color: #67c23a; }
.card-header { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px; }
.mr-8 { margin-right: 8px; }
</style>
