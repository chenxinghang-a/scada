<template>
  <div class="control-page">
    <el-card shadow="hover">
      <template #header>
        <span>设备控制</span>
      </template>

      <el-alert type="warning" :closable="false" class="mb-16">
        设备控制操作将直接写入设备寄存器，请谨慎操作。紧急情况下请使用急停按钮。
      </el-alert>

      <el-row :gutter="16">
        <el-col :span="8">
          <el-card shadow="never">
            <template #header>选择设备</template>
            <el-select v-model="selectedDevice" placeholder="选择设备" style="width: 100%" @change="loadRegisters">
              <el-option
                v-for="d in devices"
                :key="d.device_id"
                :label="`${d.device_name} (${d.device_id})`"
                :value="d.device_id"
              />
            </el-select>
          </el-card>
        </el-col>

        <el-col :span="16">
          <el-card shadow="never">
            <template #header>
              <div class="card-header">
                <span>寄存器写入</span>
                <el-button type="danger" size="small" @click="emergencyStop" :disabled="!selectedDevice">
                  <el-icon><WarningFilled /></el-icon> 急停
                </el-button>
              </div>
            </template>

            <el-form :model="writeForm" label-width="100px">
              <el-form-item label="寄存器名">
                <el-select v-model="writeForm.register_name" style="width: 100%">
                  <el-option
                    v-for="r in registers"
                    :key="r.name"
                    :label="`${r.description || r.name} (${r.name})`"
                    :value="r.name"
                  />
                </el-select>
              </el-form-item>
              <el-form-item label="写入值">
                <el-input-number v-model="writeForm.value" style="width: 100%" />
              </el-form-item>
              <el-form-item>
                <el-button type="primary" @click="writeRegister" :disabled="!selectedDevice">
                  写入寄存器
                </el-button>
                <el-button @click="writeCoil(true)" :disabled="!selectedDevice">
                  线圈 ON
                </el-button>
                <el-button @click="writeCoil(false)" :disabled="!selectedDevice">
                  线圈 OFF
                </el-button>
              </el-form-item>
            </el-form>
          </el-card>
        </el-col>
      </el-row>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, reactive } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { devicesApi, type Device, type Register } from '@/api'
import api from '@/api/request'

const devices = ref<Device[]>([])
const selectedDevice = ref('')
const registers = ref<Register[]>([])

const writeForm = reactive({
  register_name: '',
  value: 0,
})

onMounted(async () => {
  try {
    const data = await devicesApi.getAll()
    devices.value = data.devices || []
  } catch { /* ignore */ }
})

async function loadRegisters(deviceId: string) {
  try {
    const data = await devicesApi.getById(deviceId)
    registers.value = data.device?.registers || []
  } catch { /* ignore */ }
}

async function writeRegister() {
  if (!selectedDevice.value || !writeForm.register_name) {
    ElMessage.warning('请选择设备和寄存器')
    return
  }
  try {
    await api.post(`/devices/${selectedDevice.value}/write-register`, writeForm)
    ElMessage.success('写入成功')
  } catch { /* handled by interceptor */ }
}

async function writeCoil(value: boolean) {
  if (!selectedDevice.value || !writeForm.register_name) {
    ElMessage.warning('请选择设备和寄存器')
    return
  }
  try {
    await api.post(`/devices/${selectedDevice.value}/write-coil`, {
      register_name: writeForm.register_name,
      value,
    })
    ElMessage.success(`线圈已${value ? '置ON' : '置OFF'}`)
  } catch { /* handled */ }
}

async function emergencyStop() {
  try {
    await ElMessageBox.confirm('确定执行紧急停止？', '紧急停止', {
      confirmButtonText: '确定',
      cancelButtonText: '取消',
      type: 'error',
    })
    await api.post(`/devices/${selectedDevice.value}/emergency-stop`)
    ElMessage.success('急停指令已发送')
  } catch { /* cancelled */ }
}
</script>

<style scoped>
.mb-16 { margin-bottom: 16px; }
.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
</style>
