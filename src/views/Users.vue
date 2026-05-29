<template>
  <div class="users-page">
    <!-- 统计卡片 -->
    <el-row :gutter="16" class="mb-16">
      <el-col :span="6">
        <el-card shadow="hover"><div class="stat"><div class="stat-label">用户总数</div><div class="stat-value">{{ users.length }}</div></div></el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="hover"><div class="stat"><div class="stat-label">管理员</div><div class="stat-value">{{ users.filter(u => u.role === 'admin').length }}</div></div></el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="hover"><div class="stat"><div class="stat-label">工程师</div><div class="stat-value">{{ users.filter(u => u.role === 'engineer').length }}</div></div></el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="hover"><div class="stat"><div class="stat-label">观察者</div><div class="stat-value">{{ users.filter(u => u.role === 'viewer').length }}</div></div></el-card>
      </el-col>
    </el-row>

    <!-- 用户列表 -->
    <el-card shadow="hover" class="mb-16">
      <template #header>
        <div class="card-header">
          <span>用户管理</span>
          <el-button type="primary" size="small" @click="showAddDialog">添加用户</el-button>
        </div>
      </template>
      <el-table :data="users" stripe v-loading="loading">
        <el-table-column label="用户" width="200">
          <template #default="{ row }">
            <div class="user-cell">
              <el-avatar :size="32" class="user-avatar">{{ (row.display_name || row.username)[0] }}</el-avatar>
              <div>
                <div class="user-name">{{ row.display_name || row.username }}</div>
                <div class="user-id">{{ row.username }}</div>
              </div>
            </div>
          </template>
        </el-table-column>
        <el-table-column prop="role" label="角色" width="120">
          <template #default="{ row }">
            <el-tag :type="row.role === 'admin' ? 'danger' : row.role === 'engineer' ? 'warning' : 'info'" size="small">
              {{ row.role === 'admin' ? '管理员' : row.role === 'engineer' ? '工程师' : '观察者' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="150">
          <template #default="{ row }">
            <el-button type="primary" link size="small" @click="editUser(row)">编辑</el-button>
            <el-popconfirm title="确定删除此用户？" @confirm="deleteUser(row.username)" :disabled="row.username === 'admin'">
              <template #reference>
                <el-button type="danger" link size="small" :disabled="row.username === 'admin'">删除</el-button>
              </template>
            </el-popconfirm>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 操作日志 -->
    <el-card shadow="hover">
      <template #header><span>操作日志</span></template>
      <el-table :data="logs" size="small" max-height="300" v-loading="logsLoading">
        <el-table-column prop="timestamp" label="时间" width="180">
          <template #default="{ row }">{{ new Date(row.timestamp).toLocaleString() }}</template>
        </el-table-column>
        <el-table-column prop="username" label="用户" width="100" />
        <el-table-column prop="action" label="操作" width="120">
          <template #default="{ row }">
            <el-tag :type="row.action === 'login' ? 'success' : row.action === 'login_failed' ? 'danger' : 'info'" size="small">
              {{ actionLabel(row.action) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="detail" label="详情" show-overflow-tooltip />
      </el-table>
    </el-card>

    <!-- 添加/编辑弹窗 -->
    <el-dialog v-model="dialogVisible" :title="isEdit ? '编辑用户' : '添加用户'" width="450px">
      <el-form :model="form" label-width="80px">
        <el-form-item label="用户名"><el-input v-model="form.username" :disabled="isEdit" /></el-form-item>
        <el-form-item v-if="!isEdit" label="密码"><el-input v-model="form.password" type="password" show-password /></el-form-item>
        <el-form-item label="显示名"><el-input v-model="form.display_name" /></el-form-item>
        <el-form-item label="角色">
          <el-select v-model="form.role" style="width:100%">
            <el-option label="管理员" value="admin"><span>管理员</span><small style="color:#909399;margin-left:8px">完全访问权限</small></el-option>
            <el-option label="工程师" value="engineer"><span>工程师</span><small style="color:#909399;margin-left:8px">设备管理+数据导出</small></el-option>
            <el-option label="观察者" value="viewer"><span>观察者</span><small style="color:#909399;margin-left:8px">只读权限</small></el-option>
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" @click="saveUser">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, reactive } from 'vue'
import { ElMessage } from 'element-plus'
import { authApi } from '@/api'
import api from '@/api/request'

interface User { username: string; display_name: string; role: string }

const users = ref<User[]>([])
const logs = ref<any[]>([])
const loading = ref(false)
const logsLoading = ref(false)
const dialogVisible = ref(false)
const isEdit = ref(false)
const form = reactive({ username: '', password: '', display_name: '', role: 'viewer' })

onMounted(() => { refreshUsers(); refreshLogs() })

async function refreshUsers() {
  loading.value = true
  try { const data = await authApi.getUsers(); users.value = data.users || [] } catch { /* ignore */ }
  finally { loading.value = false }
}

async function refreshLogs() {
  logsLoading.value = true
  try { const data = await authApi.getLogs({ per_page: 50 }) as any; logs.value = data.logs || data.items || [] } catch { /* ignore */ }
  finally { logsLoading.value = false }
}

function showAddDialog() {
  isEdit.value = false
  Object.assign(form, { username: '', password: '', display_name: '', role: 'viewer' })
  dialogVisible.value = true
}

function editUser(user: User) {
  isEdit.value = true
  Object.assign(form, { ...user, password: '' })
  dialogVisible.value = true
}

async function saveUser() {
  try {
    if (isEdit.value) {
      await api.put(`/auth/users/${form.username}`, { display_name: form.display_name, role: form.role })
    } else {
      await api.post('/auth/register', form)
    }
    ElMessage.success(isEdit.value ? '用户已更新' : '用户已添加')
    dialogVisible.value = false
    refreshUsers()
  } catch { /* handled */ }
}

async function deleteUser(username: string) {
  try { await api.delete(`/auth/users/${username}`); ElMessage.success('用户已删除'); refreshUsers() } catch { /* ignore */ }
}

function actionLabel(action: string) {
  const map: Record<string, string> = { login: '登录', login_failed: '登录失败', register: '注册', change_password: '改密', update_user: '更新用户', delete_user: '删除用户' }
  return map[action] || action
}
</script>

<style scoped>
.mb-16 { margin-bottom: 16px; }
.stat { text-align: center; }
.stat-label { font-size: 14px; color: #909399; margin-bottom: 8px; }
.stat-value { font-size: 28px; font-weight: bold; }
.card-header { display: flex; align-items: center; justify-content: space-between; }
.user-cell { display: flex; align-items: center; gap: 10px; }
.user-avatar { background: #409eff; color: #fff; }
.user-name { font-weight: 500; }
.user-id { font-size: 12px; color: #909399; }
</style>
