<template>
  <div class="history-page">
    <el-card shadow="hover">
      <template #header>
        <span>历史数据</span>
      </template>

      <el-form :inline="true" class="filter-form">
        <el-form-item label="设备">
          <el-select v-model="filter.device_id" placeholder="选择设备" @change="loadRegisters">
            <el-option v-for="d in devices" :key="d.device_id" :label="d.device_name" :value="d.device_id" />
          </el-select>
        </el-form-item>
        <el-form-item label="寄存器">
          <el-select v-model="filter.register_name" placeholder="选择寄存器">
            <el-option v-for="r in registers" :key="r.name" :label="r.description || r.name" :value="r.name" />
          </el-select>
        </el-form-item>
        <el-form-item label="时间范围">
          <el-date-picker v-model="filter.timeRange" type="datetimerange" range-separator="至"
            start-placeholder="开始时间" end-placeholder="结束时间" />
        </el-form-item>
        <el-form-item label="聚合">
          <el-select v-model="filter.interval" style="width: 100px">
            <el-option label="原始" value="" />
            <el-option label="1分钟" value="1min" />
            <el-option label="5分钟" value="5min" />
            <el-option label="1小时" value="1hour" />
            <el-option label="1天" value="1day" />
          </el-select>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="queryHistory">查询</el-button>
          <el-button @click="exportData">导出</el-button>
        </el-form-item>
      </el-form>

      <div ref="chartRef" class="chart-container"></div>

      <el-table :data="tableData" stripe class="mt-16" max-height="400">
        <el-table-column prop="timestamp" label="时间" width="180" />
        <el-table-column prop="value" label="数值" />
      </el-table>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, reactive } from 'vue'
import * as echarts from 'echarts'
import { devicesApi, dataApi, type Device, type Register, type HistoryRecord } from '@/api'

const devices = ref<Device[]>([])
const registers = ref<Register[]>([])
const tableData = ref<HistoryRecord[]>([])
const chartRef = ref<HTMLElement>()
let chart: echarts.ECharts | null = null

const filter = reactive({
  device_id: '',
  register_name: '',
  timeRange: null as any,
  interval: '',
})

onMounted(async () => {
  try {
    const data = await devicesApi.getAll()
    devices.value = data.devices || []
  } catch { /* ignore */ }
  if (chartRef.value) chart = echarts.init(chartRef.value)
})

async function loadRegisters(deviceId: string) {
  try {
    const data = await devicesApi.getById(deviceId)
    registers.value = data.device?.registers || []
  } catch { /* ignore */ }
}

async function queryHistory() {
  if (!filter.device_id || !filter.register_name) return
  try {
    const params: any = { interval: filter.interval }
    if (filter.timeRange?.length === 2) {
      params.start = filter.timeRange[0].toISOString()
      params.end = filter.timeRange[1].toISOString()
    }
    const data = await dataApi.getHistory(filter.device_id, filter.register_name, params)
    tableData.value = data.data || []
    updateChart()
  } catch { /* ignore */ }
}

function updateChart() {
  if (!chart) return
  chart.setOption({
    tooltip: { trigger: 'axis' },
    grid: { left: '3%', right: '4%', bottom: '3%', containLabel: true },
    xAxis: { type: 'category', data: tableData.value.map(d => d.timestamp) },
    yAxis: { type: 'value' },
    series: [{ type: 'line', smooth: true, data: tableData.value.map(d => d.value) }],
  })
}

async function exportData() {
  if (!filter.device_id) return
  try {
    const blob = await dataApi.exportDevice(filter.device_id)
    const url = URL.createObjectURL(blob as any)
    const a = document.createElement('a')
    a.href = url
    a.download = `${filter.device_id}_history.csv`
    a.click()
    URL.revokeObjectURL(url)
  } catch { /* ignore */ }
}
</script>

<style scoped>
.filter-form { margin-bottom: 16px; }
.chart-container { height: 300px; }
.mt-16 { margin-top: 16px; }
</style>
