<template>
  <div class="config-page">
    <el-card shadow="hover">
      <template #header><span>系统配置</span></template>
      <el-tabs v-model="activeTab">
        <!-- 系统设置 -->
        <el-tab-pane label="系统设置" name="system">
          <el-form label-width="120px" style="max-width:600px">
            <el-form-item label="系统名称"><el-input v-model="config.system.name" /></el-form-item>
            <el-form-item label="版本"><el-input value="v1.0.0" disabled /></el-form-item>
            <el-form-item label="Web端口"><el-input-number v-model="config.system.port" :min="1" :max="65535" /></el-form-item>
            <el-form-item label="Web地址"><el-input v-model="config.system.host" /></el-form-item>
            <el-form-item label="调试模式"><el-switch v-model="config.system.debug" /></el-form-item>
            <el-form-item><el-button type="primary" @click="saveConfig('system')">保存</el-button></el-form-item>
          </el-form>
        </el-tab-pane>

        <!-- 采集设置 -->
        <el-tab-pane label="采集设置" name="collection">
          <el-form label-width="120px" style="max-width:600px">
            <el-form-item label="默认采集间隔(秒)"><el-input-number v-model="config.collection.interval" :min="1" /></el-form-item>
            <el-form-item label="连接超时(秒)"><el-input-number v-model="config.collection.timeout" :min="1" /></el-form-item>
            <el-form-item label="重试次数"><el-input-number v-model="config.collection.retries" :min="0" /></el-form-item>
            <el-form-item label="重试间隔(秒)"><el-input-number v-model="config.collection.retry_interval" :min="1" /></el-form-item>
            <el-form-item><el-button type="primary" @click="saveConfig('collection')">保存</el-button></el-form-item>
          </el-form>
        </el-tab-pane>

        <!-- 数据库设置 -->
        <el-tab-pane label="数据库设置" name="database">
          <el-form label-width="140px" style="max-width:600px">
            <el-form-item label="原始数据保留(天)"><el-input-number v-model="config.database.retention_days" :min="1" /></el-form-item>
            <el-form-item label="数据压缩"><el-switch v-model="config.database.compression" /></el-form-item>
            <el-form-item label="压缩间隔(小时)"><el-input-number v-model="config.database.compression_interval" :min="1" /></el-form-item>
            <el-form-item><el-button type="primary" @click="saveConfig('database')">保存</el-button></el-form-item>
          </el-form>
        </el-tab-pane>

        <!-- 报警规则 -->
        <el-tab-pane label="报警规则" name="alarms">
          <el-button type="primary" size="small" class="mb-16" @click="showRuleDialog">添加规则</el-button>
          <el-table :data="alarmRules" stripe size="small">
            <el-table-column prop="id" label="ID" width="100" />
            <el-table-column prop="name" label="名称" />
            <el-table-column prop="device_id" label="设备" width="140" />
            <el-table-column prop="condition" label="条件" width="80" />
            <el-table-column prop="threshold" label="阈值" width="80" />
            <el-table-column prop="level" label="等级" width="80">
              <template #default="{ row }">
                <el-tag :type="row.level === 'critical' ? 'danger' : 'warning'" size="small">{{ row.level }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="enabled" label="启用" width="60">
              <template #default="{ row }"><el-switch v-model="row.enabled" size="small" /></template>
            </el-table-column>
            <el-table-column label="操作" width="120">
              <template #default="{ row }">
                <el-button type="primary" link size="small" @click="editRule(row)">编辑</el-button>
                <el-popconfirm title="确定删除？" @confirm="deleteRule(row.id)">
                  <template #reference><el-button type="danger" link size="small">删除</el-button></template>
                </el-popconfirm>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>

        <!-- 能源费率 -->
        <el-tab-pane label="能源费率" name="energy">
          <el-form label-width="140px" style="max-width:600px">
            <el-form-item label="峰时电价(元/kWh)"><el-input-number v-model="config.energy.peak_price" :min="0" :step="0.01" /></el-form-item>
            <el-form-item label="平时电价(元/kWh)"><el-input-number v-model="config.energy.flat_price" :min="0" :step="0.01" /></el-form-item>
            <el-form-item label="谷时电价(元/kWh)"><el-input-number v-model="config.energy.valley_price" :min="0" :step="0.01" /></el-form-item>
            <el-form-item label="碳排放因子"><el-input-number v-model="config.energy.carbon_factor" :min="0" :step="0.01" /></el-form-item>
            <el-form-item><el-button type="primary" @click="saveConfig('energy')">保存</el-button></el-form-item>
          </el-form>
          <el-divider />
          <h4>费率预览</h4>
          <el-descriptions :column="3" border size="small">
            <el-descriptions-item label="峰时">{{ config.energy.peak_price }} 元/kWh</el-descriptions-item>
            <el-descriptions-item label="平时">{{ config.energy.flat_price }} 元/kWh</el-descriptions-item>
            <el-descriptions-item label="谷时">{{ config.energy.valley_price }} 元/kWh</el-descriptions-item>
          </el-descriptions>
        </el-tab-pane>

        <!-- 系统状态 -->
        <el-tab-pane label="系统状态" name="status">
          <el-descriptions :column="2" border>
            <el-descriptions-item label="运行模式">
              <el-tag :type="systemStatus.simulation_mode ? 'warning' : 'success'">{{ systemStatus.simulation_mode ? '模拟模式' : '实时模式' }}</el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="设备总数">{{ systemStatus.devices_total || 0 }}</el-descriptions-item>
            <el-descriptions-item label="在线设备">{{ systemStatus.devices_connected || 0 }}</el-descriptions-item>
            <el-descriptions-item label="活跃报警">{{ systemStatus.alarms_active || 0 }}</el-descriptions-item>
            <el-descriptions-item label="数据采集器">
              <el-tag :type="systemStatus.data_collector_running ? 'success' : 'danger'">{{ systemStatus.data_collector_running ? '运行中' : '已停止' }}</el-tag>
            </el-descriptions-item>
          </el-descriptions>
          <el-divider />
          <h4>数据库信息</h4>
          <el-table :data="dbTables" stripe size="small">
            <el-table-column prop="name" label="表名" />
            <el-table-column prop="rows" label="记录数" width="120" />
            <el-table-column prop="size" label="大小" width="120" />
          </el-table>
        </el-tab-pane>
      </el-tabs>
    </el-card>

    <!-- 报警规则弹窗 -->
    <el-dialog v-model="ruleDialogVisible" :title="isEditRule ? '编辑规则' : '添加规则'" width="500px">
      <el-form :model="ruleForm" label-width="80px">
        <el-form-item label="名称"><el-input v-model="ruleForm.name" /></el-form-item>
        <el-form-item label="设备">
          <el-select v-model="ruleForm.device_id" style="width:100%">
            <el-option v-for="d in devices" :key="d.device_id" :label="d.device_name" :value="d.device_id" />
          </el-select>
        </el-form-item>
        <el-form-item label="条件">
          <el-select v-model="ruleForm.condition" style="width:100%">
            <el-option label="大于 (>)" value=">" />
            <el-option label="小于 (<)" value="<" />
            <el-option label="大于等于 (>=)" value=">=" />
            <el-option label="小于等于 (<=)" value="<=" />
            <el-option label="等于 (=)" value="=" />
          </el-select>
        </el-form-item>
        <el-form-item label="阈值"><el-input-number v-model="ruleForm.threshold" style="width:100%" /></el-form-item>
        <el-form-item label="等级">
          <el-select v-model="ruleForm.level" style="width:100%">
            <el-option label="严重" value="critical" />
            <el-option label="警告" value="warning" />
            <el-option label="信息" value="info" />
          </el-select>
        </el-form-item>
        <el-form-item label="启用"><el-switch v-model="ruleForm.enabled" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="ruleDialogVisible = false">取消</el-button>
        <el-button type="primary" @click="saveRule">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, reactive, computed } from 'vue'
import { ElMessage } from 'element-plus'
import { systemApi, devicesApi, type Device } from '@/api'
import api from '@/api/request'

const activeTab = ref('system')
const devices = ref<Device[]>([])
const alarmRules = ref<any[]>([])
const systemStatus = ref<any>({})
const dbInfo = ref<any>(null)
const ruleDialogVisible = ref(false)
const isEditRule = ref(false)

const config = reactive({
  system: { name: 'SmartSCADA', port: 5000, host: '127.0.0.1', debug: false },
  collection: { interval: 5, timeout: 10, retries: 3, retry_interval: 5 },
  database: { retention_days: 30, compression: true, compression_interval: 24 },
  energy: { peak_price: 1.2, flat_price: 0.8, valley_price: 0.4, carbon_factor: 0.5 },
})

const ruleForm = reactive({ id: '', name: '', device_id: '', register_name: '', condition: '>', threshold: 0, level: 'warning', enabled: true })

const dbTables = computed(() => {
  if (!dbInfo.value?.tables) return []
  return Object.entries(dbInfo.value.tables).map(([name, info]: any) => ({ name, rows: info.rows, size: info.size }))
})

onMounted(async () => {
  try { const data = await devicesApi.getAll(); devices.value = data.devices || [] } catch { /* ignore */ }
  loadSystemStatus()
  loadAlarmRules()
})

async function loadSystemStatus() {
  try {
    const [s, d] = await Promise.all([systemApi.getStatus(), systemApi.getDatabase()])
    systemStatus.value = s
    dbInfo.value = d
  } catch { /* ignore */ }
}

async function loadAlarmRules() {
  try { const data = await api.get('/alarm-rules') as any; alarmRules.value = data.rules || [] } catch { /* ignore */ }
}

async function saveConfig(section: string) {
  try {
    await api.put('/config', { section, data: (config as any)[section] })
    ElMessage.success('配置已保存')
  } catch { /* ignore */ }
}

function showRuleDialog() {
  isEditRule.value = false
  Object.assign(ruleForm, { id: '', name: '', device_id: '', register_name: '', condition: '>', threshold: 0, level: 'warning', enabled: true })
  ruleDialogVisible.value = true
}

function editRule(rule: any) {
  isEditRule.value = true
  Object.assign(ruleForm, rule)
  ruleDialogVisible.value = true
}

async function saveRule() {
  try {
    if (isEditRule.value) {
      await api.put(`/alarm-rules/${ruleForm.id}`, ruleForm)
    } else {
      await api.post('/alarm-rules', ruleForm)
    }
    ElMessage.success('规则已保存')
    ruleDialogVisible.value = false
    loadAlarmRules()
  } catch { /* ignore */ }
}

async function deleteRule(id: string) {
  try { await api.delete(`/alarm-rules/${id}`); ElMessage.success('规则已删除'); loadAlarmRules() } catch { /* ignore */ }
}
</script>

<style scoped>
.mb-16 { margin-bottom: 16px; }
</style>
