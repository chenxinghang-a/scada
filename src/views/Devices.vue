<template>
  <div class="devices-page">
    <!-- 操作栏 -->
    <el-card shadow="hover" class="mb-16">
      <div class="action-bar">
        <div>
          <el-button type="primary" @click="showAddDialog"><el-icon><Plus /></el-icon> 自定义添加</el-button>
          <el-button @click="showPresets = !showPresets"><el-icon><Grid /></el-icon> 预设设备</el-button>
          <el-button @click="refreshDevices"><el-icon><Refresh /></el-icon> 刷新</el-button>
        </div>
        <div>
          <el-tag type="info">共 {{ devices.length }} 台设备</el-tag>
        </div>
      </div>
    </el-card>

    <!-- 预设设备面板 -->
    <el-collapse-transition>
      <el-card v-show="showPresets" shadow="hover" class="mb-16">
        <template #header>
          <div class="card-header">
            <span>预设设备</span>
            <el-button type="success" size="small" @click="addAllPresets">一键添加全部</el-button>
          </div>
        </template>
        <el-tabs v-model="presetCategory" type="card">
          <el-tab-pane v-for="cat in presetCategories" :key="cat" :label="cat" :name="cat" />
        </el-tabs>
        <el-row :gutter="12">
          <el-col v-for="p in filteredPresets" :key="p.id" :span="6">
            <div class="preset-card" @click="addPreset(p)">
              <div class="preset-name">{{ p.name }}</div>
              <div class="preset-protocol"><el-tag size="small">{{ p.protocol }}</el-tag></div>
              <div class="preset-desc">{{ p.description }}</div>
            </div>
          </el-col>
        </el-row>
      </el-card>
    </el-collapse-transition>

    <!-- 设备列表 -->
    <el-card shadow="hover">
      <el-table :data="devices" stripe v-loading="loading">
        <el-table-column prop="device_id" label="设备ID" width="160" show-overflow-tooltip />
        <el-table-column prop="device_name" label="设备名称" show-overflow-tooltip />
        <el-table-column prop="protocol" label="协议" width="100">
          <template #default="{ row }">
            <el-tag :type="protocolColor(row.protocol)" size="small">{{ row.protocol }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="地址" width="160" show-overflow-tooltip>
          <template #default="{ row }">{{ row.host }}{{ row.port ? ':' + row.port : '' }}</template>
        </el-table-column>
        <el-table-column label="状态" width="80">
          <template #default="{ row }">
            <el-tag :type="row.connected ? 'success' : 'danger'" size="small" effect="dark">
              {{ row.connected ? '在线' : '离线' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="数据点" width="80" align="center">
          <template #default="{ row }">{{ row.registers?.length || 0 }}</template>
        </el-table-column>
        <el-table-column label="操作" width="200" fixed="right">
          <template #default="{ row }">
            <el-button type="primary" link size="small" @click="testDevice(row)">测试</el-button>
            <el-button type="warning" link size="small" @click="editDevice(row)">编辑</el-button>
            <el-button type="info" link size="small" @click="viewData(row)">数据</el-button>
            <el-popconfirm title="确定删除此设备？" @confirm="deleteDevice(row)">
              <template #reference>
                <el-button type="danger" link size="small">删除</el-button>
              </template>
            </el-popconfirm>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 添加/编辑对话框 -->
    <el-dialog v-model="dialogVisible" :title="isEdit ? '编辑设备' : '添加设备'" width="700px" top="5vh">
      <el-form :model="form" label-width="100px">
        <el-row :gutter="16">
          <el-col :span="12">
            <el-form-item label="设备ID">
              <el-input v-model="form.device_id" :disabled="isEdit" placeholder="如 schneider_m340_01" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="设备名称">
              <el-input v-model="form.device_name" placeholder="如 施耐德M340 PLC" />
            </el-form-item>
          </el-col>
        </el-row>
        <el-row :gutter="16">
          <el-col :span="12">
            <el-form-item label="协议">
              <el-select v-model="form.protocol" @change="onProtocolChange" style="width:100%">
                <el-option label="Modbus TCP" value="modbus_tcp" />
                <el-option label="Modbus RTU" value="modbus_rtu" />
                <el-option label="OPC UA" value="opcua" />
                <el-option label="MQTT" value="mqtt" />
                <el-option label="REST HTTP" value="rest" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="采集间隔">
              <el-input-number v-model="form.collection_interval" :min="1" :max="3600" style="width:100%" />
            </el-form-item>
          </el-col>
        </el-row>

        <!-- Modbus TCP/RTU 字段 -->
        <template v-if="form.protocol?.startsWith('modbus')">
          <el-divider content-position="left">Modbus 配置</el-divider>
          <el-row :gutter="16">
            <el-col :span="8">
              <el-form-item :label="form.protocol === 'modbus_rtu' ? '串口' : 'IP地址'">
                <el-input v-model="form.host" :placeholder="form.protocol === 'modbus_rtu' ? 'COM1' : '192.168.1.100'" />
              </el-form-item>
            </el-col>
            <el-col :span="8">
              <el-form-item :label="form.protocol === 'modbus_rtu' ? '波特率' : '端口'">
                <el-input-number v-model="form.port" :min="1" :max="115200" style="width:100%" />
              </el-form-item>
            </el-col>
            <el-col :span="8">
              <el-form-item label="从站ID">
                <el-input-number v-model="form.slave_id" :min="1" :max="247" style="width:100%" />
              </el-form-item>
            </el-col>
          </el-row>
          <el-divider content-position="left">寄存器配置</el-divider>
          <div v-for="(reg, i) in form.registers" :key="i" class="register-row">
            <el-input v-model="reg.name" placeholder="名称" style="width:120px" />
            <el-input v-model="reg.description" placeholder="描述" style="width:150px" />
            <el-input-number v-model="reg.address" placeholder="地址" :min="0" :max="65535" style="width:100px" />
            <el-select v-model="reg.data_type" style="width:100px">
              <el-option label="INT16" value="int16" />
              <el-option label="UINT16" value="uint16" />
              <el-option label="INT32" value="int32" />
              <el-option label="FLOAT32" value="float32" />
            </el-select>
            <el-input v-model="reg.unit" placeholder="单位" style="width:60px" />
            <el-button type="danger" :icon="Delete" circle size="small" @click="form.registers.splice(i, 1)" />
          </div>
          <el-button type="primary" link @click="addRegister"><el-icon><Plus /></el-icon> 添加寄存器</el-button>
        </template>

        <!-- OPC UA 字段 -->
        <template v-if="form.protocol === 'opcua'">
          <el-divider content-position="left">OPC UA 配置</el-divider>
          <el-form-item label="端点URL">
            <el-input v-model="form.host" placeholder="opc.tcp://192.168.1.100:4840" />
          </el-form-item>
        </template>

        <!-- MQTT 字段 -->
        <template v-if="form.protocol === 'mqtt'">
          <el-divider content-position="left">MQTT 配置</el-divider>
          <el-row :gutter="16">
            <el-col :span="12">
              <el-form-item label="Broker地址">
                <el-input v-model="form.host" placeholder="192.168.1.100" />
              </el-form-item>
            </el-col>
            <el-col :span="12">
              <el-form-item label="端口">
                <el-input-number v-model="form.port" :min="1" :max="65535" style="width:100%" />
              </el-form-item>
            </el-col>
          </el-row>
        </template>

        <!-- REST 字段 -->
        <template v-if="form.protocol === 'rest'">
          <el-divider content-position="left">REST 配置</el-divider>
          <el-form-item label="基础URL">
            <el-input v-model="form.host" placeholder="http://192.168.1.100:8080" />
          </el-form-item>
        </template>

        <el-form-item label="启用">
          <el-switch v-model="form.enabled" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" @click="saveDevice">保存</el-button>
      </template>
    </el-dialog>

    <!-- 测试结果弹窗 -->
    <el-dialog v-model="testDialogVisible" title="连接测试" width="400px">
      <el-result :icon="testResult.success ? 'success' : 'error'" :title="testResult.success ? '连接成功' : '连接失败'" :sub-title="testResult.message" />
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, reactive, computed } from 'vue'
import { ElMessage } from 'element-plus'
import { Delete } from '@element-plus/icons-vue'
import { devicesApi, type Device, type Register } from '@/api'
import api from '@/api/request'

const devices = ref<Device[]>([])
const loading = ref(false)
const dialogVisible = ref(false)
const testDialogVisible = ref(false)
const isEdit = ref(false)
const showPresets = ref(false)
const presetCategory = ref('Modbus')

const form = reactive<any>({
  device_id: '', device_name: '', protocol: 'modbus_tcp', host: '127.0.0.1', port: 502, slave_id: 1,
  collection_interval: 5, enabled: true, registers: [],
})

const testResult = reactive({ success: false, message: '' })

// 预设设备
const presetCategories = ['Modbus', 'OPC UA', 'MQTT', 'REST']
const presets = [
  { id: 'schneider_m340_01', name: '施耐德M340 PLC', protocol: 'modbus_tcp', category: 'Modbus', description: '施耐德M340 PLC - 水处理控制' },
  { id: 'abb_m4m_01', name: 'ABB M4M仪表', protocol: 'modbus_tcp', category: 'Modbus', description: 'ABB M4M电力监测仪表' },
  { id: 'turck_iolink_01', name: '图尔克IO-Link', protocol: 'modbus_tcp', category: 'Modbus', description: '图尔克IO-Link站点' },
  { id: 'delta_dvp_01', name: '台达DVP PLC', protocol: 'modbus_tcp', category: 'Modbus', description: '台达DVP PLC - 包装线控制' },
  { id: 'inovance_h5u_01', name: '汇川H5U PLC', protocol: 'modbus_tcp', category: 'Modbus', description: '汇川H5U PLC - 喷涂车间' },
  { id: 'siemens_1500_01', name: '西门子S7-1500', protocol: 'modbus_tcp', category: 'Modbus', description: '西门子S7-1500 PLC - 锅炉控制' },
  { id: 'hollysys_lk_01', name: '和利时LK PLC', protocol: 'modbus_tcp', category: 'Modbus', description: '和利时LK PLC - 化工流程' },
  { id: 'mitsubishi_fx5u_01', name: '三菱FX5U PLC', protocol: 'modbus_tcp', category: 'Modbus', description: '三菱FX5U PLC - 注塑机控制' },
  { id: 'opcua_plc_01', name: 'OPC UA PLC', protocol: 'opcua', category: 'OPC UA', description: 'OPC UA PLC测试设备' },
  { id: 'vibration_sensor_01', name: 'MQTT振动传感器', protocol: 'mqtt', category: 'MQTT', description: 'MQTT振动监测节点' },
  { id: 'water_quality_01', name: 'MQTT水质传感器', protocol: 'mqtt', category: 'MQTT', description: 'MQTT水质监测终端' },
  { id: 'siemens_web_01', name: '西门子Web API', protocol: 'rest', category: 'REST', description: '西门子S7-1500 REST API' },
  { id: 'mes_api_01', name: 'MES系统API', protocol: 'rest', category: 'REST', description: 'MES制造执行系统' },
]

const filteredPresets = computed(() => presets.filter(p => p.category === presetCategory.value))

onMounted(() => { refreshDevices() })

async function refreshDevices() {
  loading.value = true
  try {
    const data = await devicesApi.getAll()
    devices.value = data.devices || []
  } catch { /* ignore */ }
  finally { loading.value = false }
}

function showAddDialog() {
  isEdit.value = false
  Object.assign(form, { device_id: '', device_name: '', protocol: 'modbus_tcp', host: '127.0.0.1', port: 502, slave_id: 1, collection_interval: 5, enabled: true, registers: [] })
  dialogVisible.value = true
}

function editDevice(device: any) {
  isEdit.value = true
  Object.assign(form, { ...device, registers: device.registers ? [...device.registers] : [] })
  dialogVisible.value = true
}

function onProtocolChange(protocol: string) {
  const defaults: Record<string, any> = {
    modbus_tcp: { host: '127.0.0.1', port: 502 },
    modbus_rtu: { host: 'COM1', port: 9600 },
    opcua: { host: 'opc.tcp://localhost:4840', port: 4840 },
    mqtt: { host: 'localhost', port: 1883 },
    rest: { host: 'http://localhost:8080', port: 8080 },
  }
  const d = defaults[protocol]
  if (d) { form.host = d.host; form.port = d.port }
}

function addRegister() {
  form.registers.push({ name: '', description: '', address: 0, data_type: 'int16', scale: 1, unit: '', rw: 'r' })
}

async function saveDevice() {
  try {
    if (isEdit.value) {
      await devicesApi.update(form.device_id, form)
      ElMessage.success('设备更新成功')
    } else {
      await devicesApi.create(form)
      ElMessage.success('设备添加成功')
    }
    dialogVisible.value = false
    refreshDevices()
  } catch { /* handled */ }
}

async function deleteDevice(device: Device) {
  try {
    await devicesApi.delete(device.device_id)
    ElMessage.success('设备已删除')
    refreshDevices()
  } catch { /* ignore */ }
}

async function testDevice(device: Device) {
  try {
    const data = await devicesApi.test(device.device_id)
    testResult.success = data.success
    testResult.message = data.message
    testDialogVisible.value = true
  } catch { /* ignore */ }
}

function viewData(device: Device) {
  window.location.hash = `#/history?device=${device.device_id}`
}

async function addPreset(preset: any) {
  try {
    await api.post('/devices/presets/add', { preset_id: preset.id })
    ElMessage.success(`已添加: ${preset.name}`)
    refreshDevices()
  } catch { /* handled */ }
}

async function addAllPresets() {
  try {
    await api.post('/devices/presets/add-all')
    ElMessage.success('全部预设设备已添加')
    refreshDevices()
  } catch { /* handled */ }
}

function protocolColor(p: string) {
  const map: Record<string, string> = { modbus_tcp: '', modbus_rtu: 'success', opcua: 'warning', mqtt: 'info', rest: 'danger' }
  return map[p] || ''
}
</script>

<style scoped>
.mb-16 { margin-bottom: 16px; }
.action-bar { display: flex; align-items: center; justify-content: space-between; }
.card-header { display: flex; align-items: center; justify-content: space-between; }
.preset-card { border: 1px solid #e4e7ed; border-radius: 6px; padding: 12px; margin-bottom: 12px; cursor: pointer; transition: all 0.2s; }
.preset-card:hover { border-color: #409eff; background: #f0f7ff; }
.preset-name { font-weight: bold; font-size: 14px; margin-bottom: 4px; }
.preset-protocol { margin-bottom: 4px; }
.preset-desc { font-size: 12px; color: #909399; }
.register-row { display: flex; gap: 8px; align-items: center; margin-bottom: 8px; }
</style>
