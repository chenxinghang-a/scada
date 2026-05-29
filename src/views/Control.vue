<template>
  <div class="control-page">
    <!-- 急停横幅 -->
    <el-alert v-if="eStop.active" type="error" :closable="false" class="mb-16">
      <template #title>
        <div class="estop-banner">
          <span>⚠️ 紧急停止已激活 — {{ eStop.reason }} ({{ eStop.time }})</span>
          <el-button type="warning" size="small" @click="resetEStop">重置急停</el-button>
        </div>
      </template>
    </el-alert>

    <el-row :gutter="16" class="mb-16">
      <!-- 急停按钮 -->
      <el-col :span="4">
        <el-card shadow="hover" class="estop-card">
          <div class="estop-wrapper">
            <button class="estop-btn" :class="{ active: eStop.active }" @click="triggerEStop">
              <span>急停</span>
            </button>
            <div class="estop-label">紧急停止</div>
          </div>
        </el-card>
      </el-col>

      <!-- 安全联锁 -->
      <el-col :span="10">
        <el-card shadow="hover">
          <template #header><span>安全联锁</span></template>
          <div class="interlock-list">
            <div v-for="il in interlocks" :key="il.id" class="interlock-item" :class="`il-${il.status}`">
              <div class="il-info">
                <div class="il-name">{{ il.name }}</div>
                <div class="il-desc">{{ il.description }}</div>
              </div>
              <div class="il-actions">
                <el-tag :type="il.status === 'triggered' ? 'danger' : il.status === 'bypassed' ? 'warning' : 'success'" size="small">
                  {{ il.status === 'triggered' ? '已触发' : il.status === 'bypassed' ? '已旁路' : '正常' }}
                </el-tag>
                <el-button v-if="il.status !== 'bypassed'" type="warning" link size="small" @click="bypassInterlock(il.id)">旁路</el-button>
                <el-button v-else type="success" link size="small" @click="restoreInterlock(il.id)">恢复</el-button>
              </div>
            </div>
            <el-empty v-if="interlocks.length === 0" description="暂无联锁规则" :image-size="40" />
          </div>
        </el-card>
      </el-col>

      <!-- 设备健康 -->
      <el-col :span="10">
        <el-card shadow="hover">
          <template #header><span>设备健康</span></template>
          <div class="health-list">
            <div v-for="h in deviceHealth" :key="h.device_id" class="health-item">
              <div class="h-info">
                <span class="h-name">{{ h.device_name || h.device_id }}</span>
                <span class="h-time">{{ h.response_time }}ms</span>
              </div>
              <el-tag :type="h.connected ? 'success' : 'danger'" size="small">{{ h.connected ? '在线' : '离线' }}</el-tag>
            </div>
            <el-empty v-if="deviceHealth.length === 0" description="暂无设备" :image-size="40" />
          </div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 控制面板 -->
    <el-row :gutter="16" class="mb-16">
      <el-col :span="12">
        <el-card shadow="hover">
          <template #header><span>寄存器写入</span></template>
          <el-form :model="regForm" label-width="80px">
            <el-form-item label="设备">
              <el-select v-model="regForm.device_id" style="width:100%" @change="onDeviceChange">
                <el-option v-for="d in devices" :key="d.device_id" :label="`${d.device_name} (${d.device_id})`" :value="d.device_id" />
              </el-select>
            </el-form-item>
            <el-form-item label="寄存器">
              <el-select v-model="regForm.register_name" style="width:100%">
                <el-option v-for="r in currentRegisters" :key="r.name" :label="`${r.description || r.name} (${r.name})`" :value="r.name" />
              </el-select>
            </el-form-item>
            <el-form-item label="写入值">
              <el-input-number v-model="regForm.value" style="width:100%" />
            </el-form-item>
            <el-form-item label="预设值">
              <el-select v-model="regForm.value" style="width:100%">
                <el-option label="温度设定 75°C" :value="75" />
                <el-option label="压力设定 1.0 MPa" :value="100" />
                <el-option label="启动设备" :value="1" />
                <el-option label="停止设备" :value="0" />
              </el-select>
            </el-form-item>
            <el-form-item>
              <el-button type="primary" @click="writeRegister" :disabled="!canControl">写入寄存器</el-button>
            </el-form-item>
          </el-form>
        </el-card>
      </el-col>
      <el-col :span="12">
        <el-card shadow="hover">
          <template #header><span>线圈控制</span></template>
          <el-form :model="coilForm" label-width="80px">
            <el-form-item label="设备">
              <el-select v-model="coilForm.device_id" style="width:100%">
                <el-option v-for="d in devices" :key="d.device_id" :label="`${d.device_name} (${d.device_id})`" :value="d.device_id" />
              </el-select>
            </el-form-item>
            <el-form-item label="线圈">
              <el-select v-model="coilForm.register_name" style="width:100%">
                <el-option v-for="r in currentRegisters" :key="r.name" :label="`${r.description || r.name} (${r.name})`" :value="r.name" />
              </el-select>
            </el-form-item>
            <el-form-item label="状态">
              <el-radio-group v-model="coilForm.value">
                <el-radio :value="true">ON</el-radio>
                <el-radio :value="false">OFF</el-radio>
              </el-radio-group>
            </el-form-item>
            <el-form-item label="预设">
              <el-select v-model="coilForm.value" style="width:100%">
                <el-option label="启动设备" :value="true" />
                <el-option label="停止设备" :value="false" />
                <el-option label="打开阀门" :value="true" />
                <el-option label="关闭阀门" :value="false" />
              </el-select>
            </el-form-item>
            <el-form-item>
              <el-button type="primary" @click="writeCoil" :disabled="!canControl">写入线圈</el-button>
            </el-form-item>
          </el-form>
        </el-card>
      </el-col>
    </el-row>

    <!-- 批量控制 + 操作日志 -->
    <el-row :gutter="16">
      <el-col :span="8">
        <el-card shadow="hover">
          <template #header><span>批量控制</span></template>
          <div class="batch-btns">
            <el-button type="success" size="large" @click="batchControl('start')" :disabled="!canControl">
              <el-icon><CaretRight /></el-icon> 启动全部
            </el-button>
            <el-button type="danger" size="large" @click="batchControl('stop')" :disabled="!canControl">
              <el-icon><SwitchButton /></el-icon> 停止全部
            </el-button>
            <el-button type="warning" size="large" @click="batchControl('reset')" :disabled="!canControl">
              <el-icon><RefreshRight /></el-icon> 重置全部
            </el-button>
          </div>
        </el-card>
      </el-col>
      <el-col :span="16">
        <el-card shadow="hover">
          <template #header><span>操作日志</span></template>
          <el-table :data="controlLogs" size="small" max-height="250">
            <el-table-column prop="timestamp" label="时间" width="180">
              <template #default="{ row }">{{ new Date(row.timestamp).toLocaleString() }}</template>
            </el-table-column>
            <el-table-column prop="operator" label="操作员" width="100" />
            <el-table-column prop="action_type" label="操作类型" width="120" />
            <el-table-column prop="details" label="详情" show-overflow-tooltip />
          </el-table>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, reactive, computed } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { devicesApi, type Device, type Register } from '@/api'
import api from '@/api/request'

const devices = ref<Device[]>([])
const currentRegisters = ref<Register[]>([])
const interlocks = ref<any[]>([])
const deviceHealth = ref<any[]>([])
const controlLogs = ref<any[]>([])

const eStop = reactive({ active: false, reason: '', time: '' })

const regForm = reactive({ device_id: '', register_name: '', value: 0 })
const coilForm = reactive({ device_id: '', register_name: '', value: true })

const canControl = computed(() => {
  const user = JSON.parse(localStorage.getItem('scada_user') || '{}')
  return ['admin', 'engineer'].includes(user.role)
})

let statusTimer: ReturnType<typeof setInterval>

onMounted(async () => {
  await loadDevices()
  loadSafetyStatus()
  loadLogs()
  statusTimer = setInterval(loadSafetyStatus, 10000)
})

async function loadDevices() {
  try {
    const data = await devicesApi.getAll()
    devices.value = data.devices || []
  } catch { /* ignore */ }
}

async function onDeviceChange(deviceId: string) {
  try {
    const data = await devicesApi.getById(deviceId)
    currentRegisters.value = data.device?.registers || []
  } catch { /* ignore */ }
}

async function loadSafetyStatus() {
  try {
    const data = await api.get('/control/status') as any
    if (data.estop) {
      eStop.active = data.estop.active
      eStop.reason = data.estop.reason || ''
      eStop.time = data.estop.time || ''
    }
    interlocks.value = data.interlocks || []
    deviceHealth.value = data.health || []
  } catch { /* ignore */ }
}

async function loadLogs() {
  try {
    const data = await api.get('/control/logs?limit=20') as any
    controlLogs.value = data.logs || []
  } catch { /* ignore */ }
}

async function writeRegister() {
  if (!regForm.device_id || !regForm.register_name) { ElMessage.warning('请选择设备和寄存器'); return }
  try {
    await ElMessageBox.confirm(`确认写入 ${regForm.register_name} = ${regForm.value}？`, '确认操作', { type: 'warning' })
    await api.post(`/devices/${regForm.device_id}/write-register`, regForm)
    ElMessage.success('写入成功')
    loadLogs()
  } catch { /* handled */ }
}

async function writeCoil() {
  if (!coilForm.device_id || !coilForm.register_name) { ElMessage.warning('请选择设备和线圈'); return }
  try {
    await ElMessageBox.confirm(`确认写入 ${coilForm.register_name} = ${coilForm.value ? 'ON' : 'OFF'}？`, '确认操作', { type: 'warning' })
    await api.post(`/devices/${coilForm.device_id}/write-coil`, coilForm)
    ElMessage.success('写入成功')
    loadLogs()
  } catch { /* handled */ }
}

async function triggerEStop() {
  try {
    await ElMessageBox.confirm('确定执行紧急停止？此操作将停止所有设备！', '紧急停止', { type: 'error', confirmButtonText: '执行急停' })
    await api.post('/control/estop')
    ElMessage.success('急停已执行')
    loadSafetyStatus()
  } catch { /* cancelled */ }
}

async function resetEStop() {
  try {
    await api.post('/control/estop/reset')
    ElMessage.success('急停已重置')
    loadSafetyStatus()
  } catch { /* ignore */ }
}

async function bypassInterlock(id: string) {
  try {
    await api.post(`/control/interlocks/${id}/bypass`)
    ElMessage.success('联锁已旁路')
    loadSafetyStatus()
  } catch { /* ignore */ }
}

async function restoreInterlock(id: string) {
  try {
    await api.post(`/control/interlocks/${id}/restore`)
    ElMessage.success('联锁已恢复')
    loadSafetyStatus()
  } catch { /* ignore */ }
}

async function batchControl(action: string) {
  try {
    await ElMessageBox.confirm(`确定执行「${action === 'start' ? '启动全部' : action === 'stop' ? '停止全部' : '重置全部'}」？`, '批量控制', { type: 'warning' })
    await api.post('/control/batch', { action })
    ElMessage.success('指令已发送')
    loadLogs()
  } catch { /* cancelled */ }
}
</script>

<style scoped>
.mb-16 { margin-bottom: 16px; }
.estop-banner { display: flex; align-items: center; justify-content: space-between; }
.estop-card { display: flex; align-items: center; justify-content: center; min-height: 280px; }
.estop-wrapper { text-align: center; }
.estop-btn { width: 120px; height: 120px; border-radius: 50%; background: #f56c6c; border: 4px solid #c45656; color: #fff; font-size: 20px; font-weight: bold; cursor: pointer; transition: all 0.3s; box-shadow: 0 4px 12px rgba(245,108,108,0.4); }
.estop-btn:hover { background: #e64c4c; transform: scale(1.05); }
.estop-btn.active { animation: pulse 1.5s infinite; background: #c45656; }
@keyframes pulse { 0%,100% { box-shadow: 0 0 0 0 rgba(245,108,108,0.7); } 50% { box-shadow: 0 0 0 15px rgba(245,108,108,0); } }
.estop-label { margin-top: 8px; font-size: 14px; color: #909399; }

.interlock-list, .health-list { max-height: 220px; overflow-y: auto; }
.interlock-item { display: flex; align-items: center; justify-content: space-between; padding: 8px; border-bottom: 1px solid #f0f0f0; }
.il-name { font-weight: 500; font-size: 13px; }
.il-desc { font-size: 12px; color: #909399; }
.il-actions { display: flex; align-items: center; gap: 8px; }
.health-item { display: flex; align-items: center; justify-content: space-between; padding: 6px 8px; border-bottom: 1px solid #f0f0f0; }
.h-info { display: flex; align-items: center; gap: 8px; }
.h-name { font-size: 13px; }
.h-time { font-size: 12px; color: #909399; }

.batch-btns { display: flex; flex-direction: column; gap: 12px; }
.batch-btns .el-button { width: 100%; }
</style>
