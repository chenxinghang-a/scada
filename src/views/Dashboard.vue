<template>
  <div class="dashboard">
    <!-- 顶部统计卡片 -->
    <el-row :gutter="16" class="stat-cards">
      <el-col :span="6">
        <el-card shadow="hover" class="stat-card">
          <div class="stat-content">
            <div class="stat-info">
              <div class="stat-label">设备总数</div>
              <div class="stat-value">{{ status?.devices_total || 0 }}</div>
            </div>
            <el-icon :size="40" color="#409eff"><Monitor /></el-icon>
          </div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="hover" class="stat-card">
          <div class="stat-content">
            <div class="stat-info">
              <div class="stat-label">在线设备</div>
              <div class="stat-value text-success">{{ status?.devices_connected || 0 }}</div>
            </div>
            <el-icon :size="40" color="#67c23a"><CircleCheckFilled /></el-icon>
          </div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="hover" class="stat-card">
          <div class="stat-content">
            <div class="stat-info">
              <div class="stat-label">活跃报警</div>
              <div class="stat-value text-danger">{{ status?.alarms_active || 0 }}</div>
            </div>
            <el-icon :size="40" color="#f56c6c"><Bell /></el-icon>
          </div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="hover" class="stat-card">
          <div class="stat-content">
            <div class="stat-info">
              <div class="stat-label">运行模式</div>
              <div class="stat-value">{{ status?.simulation_mode ? '模拟' : '实时' }}</div>
            </div>
            <el-icon :size="40" color="#e6a23c"><Cpu /></el-icon>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 图表区域 -->
    <el-row :gutter="16" class="chart-row">
      <el-col :span="16">
        <el-card shadow="hover">
          <template #header>
            <span>实时数据趋势</span>
          </template>
          <div ref="trendChartRef" class="chart-container"></div>
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card shadow="hover">
          <template #header>
            <span>设备状态分布</span>
          </template>
          <div ref="pieChartRef" class="chart-container"></div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 设备列表 -->
    <el-card shadow="hover" class="mt-16">
      <template #header>
        <div class="card-header">
          <span>设备概览</span>
          <el-button type="primary" size="small" @click="refreshData">
            <el-icon><Refresh /></el-icon> 刷新
          </el-button>
        </div>
      </template>
      <el-table :data="devices" stripe style="width: 100%">
        <el-table-column prop="device_id" label="设备ID" width="180" />
        <el-table-column prop="device_name" label="设备名称" />
        <el-table-column prop="protocol" label="协议" width="100">
          <template #default="{ row }">
            <el-tag size="small">{{ row.protocol }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="row.connected ? 'success' : 'danger'" size="small">
              {{ row.connected ? '在线' : '离线' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="120">
          <template #default="{ row }">
            <el-button type="primary" link size="small" @click="viewDevice(row)">
              详情
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import * as echarts from 'echarts'
import { systemApi, devicesApi, dataApi, type SystemStatus, type Device, type RealtimeData } from '@/api'
import { io } from 'socket.io-client'

const router = useRouter()
const status = ref<SystemStatus | null>(null)
const devices = ref<Device[]>([])
const realtimeData = ref<RealtimeData[]>([])

const trendChartRef = ref<HTMLElement>()
const pieChartRef = ref<HTMLElement>()
let trendChart: echarts.ECharts | null = null
let pieChart: echarts.ECharts | null = null
let socket: ReturnType<typeof io> | null = null

// 存储趋势数据
const trendData: Record<string, { timestamps: string[]; values: number[] }> = {}

onMounted(async () => {
  await refreshData()
  initCharts()
  connectSocket()
})

onUnmounted(() => {
  trendChart?.dispose()
  pieChart?.dispose()
  socket?.disconnect()
})

async function refreshData() {
  try {
    const [statusData, devicesData, realtimeResult] = await Promise.all([
      systemApi.getStatus(),
      devicesApi.getAll(),
      dataApi.getRealtime(),
    ])
    status.value = statusData
    devices.value = devicesData.devices || []
    realtimeData.value = realtimeResult.data || []
    updateCharts()
  } catch {
    // 忽略
  }
}

function initCharts() {
  if (trendChartRef.value) {
    trendChart = echarts.init(trendChartRef.value)
    trendChart.setOption({
      tooltip: { trigger: 'axis' },
      legend: { data: [] },
      grid: { left: '3%', right: '4%', bottom: '3%', containLabel: true },
      xAxis: { type: 'category', boundaryGap: false, data: [] },
      yAxis: { type: 'value' },
      series: [],
    })
  }

  if (pieChartRef.value) {
    pieChart = echarts.init(pieChartRef.value)
    pieChart.setOption({
      tooltip: { trigger: 'item' },
      legend: { orient: 'vertical', left: 'left' },
      series: [{
        name: '设备状态',
        type: 'pie',
        radius: '60%',
        data: [],
        emphasis: { itemStyle: { shadowBlur: 10, shadowOffsetX: 0, shadowColor: 'rgba(0, 0, 0, 0.5)' } },
      }],
    })
  }
}

function updateCharts() {
  // 更新饼图
  if (pieChart) {
    const online = devices.value.filter(d => d.connected).length
    const offline = devices.value.length - online
    pieChart.setOption({
      series: [{
        data: [
          { value: online, name: '在线', itemStyle: { color: '#67c23a' } },
          { value: offline, name: '离线', itemStyle: { color: '#f56c6c' } },
        ],
      }],
    })
  }

  // 更新趋势图（取前5个寄存器的数据）
  if (trendChart && realtimeData.value.length > 0) {
    const grouped: Record<string, RealtimeData[]> = {}
    realtimeData.value.forEach(d => {
      const key = `${d.device_id}/${d.register_name}`
      if (!grouped[key]) grouped[key] = []
      grouped[key].push(d)
    })

    const keys = Object.keys(grouped).slice(0, 5)
    const now = new Date().toLocaleTimeString()

    keys.forEach(key => {
      if (!trendData[key]) {
        trendData[key] = { timestamps: [], values: [] }
      }
      const val = grouped[key][0]?.value || 0
      trendData[key].timestamps.push(now)
      trendData[key].values.push(val)
      // 保留最近20个点
      if (trendData[key].timestamps.length > 20) {
        trendData[key].timestamps.shift()
        trendData[key].values.shift()
      }
    })

    trendChart.setOption({
      legend: { data: keys },
      xAxis: { data: trendData[keys[0]]?.timestamps || [] },
      series: keys.map(key => ({
        name: key,
        type: 'line',
        smooth: true,
        data: trendData[key]?.values || [],
      })),
    })
  }
}

function connectSocket() {
  const socketUrl = import.meta.env.DEV ? window.location.origin : 'http://localhost:5000'
  socket = io(socketUrl, { transports: ['websocket', 'polling'] })

  socket.on('data_update', (data: any) => {
    // 更新实时数据
    if (data.data) {
      const idx = realtimeData.value.findIndex(
        d => d.device_id === data.data.device_id && d.register_name === data.data.register_name
      )
      if (idx >= 0) {
        realtimeData.value[idx] = data.data
      } else {
        realtimeData.value.push(data.data)
      }
    }
  })

  socket.on('alarm', () => {
    // 刷新报警计数
    appStore.fetchSystemStatus()
  })
}

function viewDevice(device: Device) {
  router.push(`/devices?id=${device.device_id}`)
}

// 导入 appStore
import { useAppStore } from '@/stores/app'
const appStore = useAppStore()
</script>

<style scoped>
.stat-cards {
  margin-bottom: 16px;
}

.stat-card .stat-content {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.stat-label {
  font-size: 14px;
  color: #909399;
  margin-bottom: 8px;
}

.stat-value {
  font-size: 28px;
  font-weight: bold;
  color: #303133;
}

.text-success { color: #67c23a; }
.text-danger { color: #f56c6c; }

.chart-row {
  margin-bottom: 16px;
}

.chart-container {
  height: 300px;
}

.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.mt-16 {
  margin-top: 16px;
}
</style>
