<template>
  <div class="devices-page">
    <el-card shadow="hover">
      <template #header>
        <div class="card-header">
          <span>设备管理</span>
          <div>
            <el-button type="primary" @click="showAddDialog">
              <el-icon><Plus /></el-icon> 添加设备
            </el-button>
            <el-button @click="refreshDevices">
              <el-icon><Refresh /></el-icon> 刷新
            </el-button>
          </div>
        </div>
      </template>

      <el-table :data="devices" stripe v-loading="loading">
        <el-table-column prop="device_id" label="设备ID" width="180" />
        <el-table-column prop="device_name" label="设备名称" />
        <el-table-column prop="protocol" label="协议" width="100">
          <template #default="{ row }">
            <el-tag size="small">{{ row.protocol }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="host" label="地址" width="150" />
        <el-table-column prop="port" label="端口" width="80" />
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="row.connected ? 'success' : 'danger'" size="small">
              {{ row.connected ? '在线' : '离线' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="200">
          <template #default="{ row }">
            <el-button type="primary" link size="small" @click="testDevice(row)">测试</el-button>
            <el-button type="warning" link size="small" @click="editDevice(row)">编辑</el-button>
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
    <el-dialog
      v-model="dialogVisible"
      :title="isEdit ? '编辑设备' : '添加设备'"
      width="600px"
    >
      <el-form :model="form" label-width="100px">
        <el-form-item label="设备ID">
          <el-input v-model="form.device_id" :disabled="isEdit" />
        </el-form-item>
        <el-form-item label="设备名称">
          <el-input v-model="form.device_name" />
        </el-form-item>
        <el-form-item label="协议">
          <el-select v-model="form.protocol" @change="onProtocolChange">
            <el-option label="Modbus TCP" value="modbus_tcp" />
            <el-option label="Modbus RTU" value="modbus_rtu" />
            <el-option label="OPC UA" value="opcua" />
            <el-option label="MQTT" value="mqtt" />
            <el-option label="REST" value="rest" />
          </el-select>
        </el-form-item>
        <el-form-item label="地址">
          <el-input v-model="form.host" />
        </el-form-item>
        <el-form-item label="端口">
          <el-input-number v-model="form.port" :min="1" :max="65535" />
        </el-form-item>
        <el-form-item v-if="form.protocol?.startsWith('modbus')" label="从站ID">
          <el-input-number v-model="form.slave_id" :min="1" :max="247" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" @click="saveDevice">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, reactive } from 'vue'
import { ElMessage } from 'element-plus'
import { devicesApi, type Device } from '@/api'

const devices = ref<Device[]>([])
const loading = ref(false)
const dialogVisible = ref(false)
const isEdit = ref(false)

const form = reactive<Partial<Device>>({
  device_id: '',
  device_name: '',
  protocol: 'modbus_tcp',
  host: '127.0.0.1',
  port: 502,
  slave_id: 1,
})

onMounted(() => {
  refreshDevices()
})

async function refreshDevices() {
  loading.value = true
  try {
    const data = await devicesApi.getAll()
    devices.value = data.devices || []
  } catch {
    // ignore
  } finally {
    loading.value = false
  }
}

function showAddDialog() {
  isEdit.value = false
  Object.assign(form, { device_id: '', device_name: '', protocol: 'modbus_tcp', host: '127.0.0.1', port: 502, slave_id: 1 })
  dialogVisible.value = true
}

function editDevice(device: Device) {
  isEdit.value = true
  Object.assign(form, device)
  dialogVisible.value = true
}

function onProtocolChange(protocol: string) {
  const defaults: Record<string, { host: string; port: number }> = {
    modbus_tcp: { host: '127.0.0.1', port: 502 },
    modbus_rtu: { host: 'COM1', port: 9600 },
    opcua: { host: 'opc.tcp://localhost:4840', port: 4840 },
    mqtt: { host: 'localhost', port: 1883 },
    rest: { host: 'http://localhost:8080', port: 8080 },
  }
  const d = defaults[protocol]
  if (d) {
    form.host = d.host
    form.port = d.port
  }
}

async function saveDevice() {
  try {
    if (isEdit.value) {
      await devicesApi.update(form.device_id!, form)
      ElMessage.success('设备更新成功')
    } else {
      await devicesApi.create(form)
      ElMessage.success('设备添加成功')
    }
    dialogVisible.value = false
    refreshDevices()
  } catch {
    // error handled by interceptor
  }
}

async function deleteDevice(device: Device) {
  try {
    await devicesApi.delete(device.device_id)
    ElMessage.success('设备已删除')
    refreshDevices()
  } catch {
    // ignore
  }
}

async function testDevice(device: Device) {
  try {
    const data = await devicesApi.test(device.device_id)
    ElMessage[data.success ? 'success' : 'error'](data.message)
  } catch {
    // ignore
  }
}
</script>

<style scoped>
.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
</style>
