<template>
  <div class="alarm-output-page">
    <el-card shadow="hover">
      <template #header><span>报警输出与广播控制</span></template>
      <el-alert type="info" :closable="false" class="mb-16">
        此页面用于控制声光报警器（信号灯塔）和工厂广播系统。
      </el-alert>

      <el-row :gutter="16">
        <el-col :span="12">
          <el-card shadow="never">
            <template #header><span>信号灯塔控制</span></template>
            <el-form label-width="100px">
              <el-form-item label="红灯"><el-switch v-model="lights.red" @change="setLight('red', lights.red)" /></el-form-item>
              <el-form-item label="黄灯"><el-switch v-model="lights.yellow" @change="setLight('yellow', lights.yellow)" /></el-form-item>
              <el-form-item label="绿灯"><el-switch v-model="lights.green" @change="setLight('green', lights.green)" /></el-form-item>
              <el-form-item label="蜂鸣器"><el-switch v-model="lights.buzzer" @change="setLight('buzzer', lights.buzzer)" /></el-form-item>
            </el-form>
          </el-card>
        </el-col>
        <el-col :span="12">
          <el-card shadow="never">
            <template #header><span>广播系统</span></template>
            <el-form label-width="100px">
              <el-form-item label="广播区域">
                <el-select v-model="broadcast.area" style="width: 100%">
                  <el-option label="全部区域" value="all" />
                  <el-option label="车间A" value="车间A" />
                  <el-option label="车间B" value="车间B" />
                  <el-option label="仓库" value="仓库" />
                  <el-option label="办公楼" value="办公楼" />
                </el-select>
              </el-form-item>
              <el-form-item label="广播内容">
                <el-input v-model="broadcast.message" type="textarea" :rows="3" />
              </el-form-item>
              <el-form-item>
                <el-button type="primary" @click="sendBroadcast">发送广播</el-button>
              </el-form-item>
            </el-form>
          </el-card>
        </el-col>
      </el-row>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { reactive } from 'vue'
import { ElMessage } from 'element-plus'
import api from '@/api/request'

const lights = reactive({ red: false, yellow: false, green: false, buzzer: false })
const broadcast = reactive({ area: 'all', message: '' })

async function setLight(color: string, state: boolean) {
  try {
    await api.post('/alarm-output/light', { color, state })
    ElMessage.success(`${color}灯已${state ? '开启' : '关闭'}`)
  } catch { /* ignore */ }
}

async function sendBroadcast() {
  if (!broadcast.message) { ElMessage.warning('请输入广播内容'); return }
  try {
    await api.post('/broadcast/speak', { area: broadcast.area, message: broadcast.message })
    ElMessage.success('广播已发送')
  } catch { /* ignore */ }
}
</script>

<style scoped>
.mb-16 { margin-bottom: 16px; }
</style>
