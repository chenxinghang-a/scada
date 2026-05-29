<template>
  <div class="users-page">
    <el-card shadow="hover">
      <template #header>
        <div class="card-header">
          <span>用户管理</span>
          <el-button type="primary" size="small" @click="showAddDialog">添加用户</el-button>
        </div>
      </template>

      <el-table :data="users" stripe v-loading="loading">
        <el-table-column prop="username" label="用户名" width="150" />
        <el-table-column prop="display_name" label="显示名" />
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
            <el-popconfirm title="确定删除此用户？" @confirm="deleteUser(row.username)">
              <template #reference>
                <el-button type="danger" link size="small" :disabled="row.username === 'admin'">删除</el-button>
              </template>
            </el-popconfirm>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog v-model="dialogVisible" :title="isEdit ? '编辑用户' : '添加用户'" width="400px">
      <el-form :model="form" label-width="80px">
        <el-form-item label="用户名">
          <el-input v-model="form.username" :disabled="isEdit" />
        </el-form-item>
        <el-form-item v-if="!isEdit" label="密码">
          <el-input v-model="form.password" type="password" show-password />
        </el-form-item>
        <el-form-item label="显示名">
          <el-input v-model="form.display_name" />
        </el-form-item>
        <el-form-item label="角色">
          <el-select v-model="form.role">
            <el-option label="管理员" value="admin" />
            <el-option label="工程师" value="engineer" />
            <el-option label="观察者" value="viewer" />
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
const loading = ref(false)
const dialogVisible = ref(false)
const isEdit = ref(false)
const form = reactive({ username: '', password: '', display_name: '', role: 'viewer' })

onMounted(() => { refreshUsers() })

async function refreshUsers() {
  loading.value = true
  try {
    const data = await authApi.getUsers()
    users.value = data.users || []
  } catch { /* ignore */ }
  finally { loading.value = false }
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
  try {
    await api.delete(`/auth/users/${username}`)
    ElMessage.success('用户已删除')
    refreshUsers()
  } catch { /* ignore */ }
}
</script>

<style scoped>
.card-header { display: flex; align-items: center; justify-content: space-between; }
</style>
