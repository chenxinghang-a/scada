<template>
  <el-container class="layout-container">
    <!-- 侧边栏 -->
    <el-aside :width="appStore.sidebarCollapsed ? '64px' : '220px'" class="layout-aside">
      <div class="logo" :class="{ collapsed: appStore.sidebarCollapsed }">
        <el-icon :size="28"><Monitor /></el-icon>
        <span v-show="!appStore.sidebarCollapsed" class="logo-text">SmartSCADA</span>
      </div>

      <el-menu
        :default-active="currentRoute"
        :collapse="appStore.sidebarCollapsed"
        router
        background-color="#1d1e1f"
        text-color="#bfcbd9"
        active-text-color="#409eff"
        class="sidebar-menu"
      >
        <el-menu-item index="/dashboard">
          <el-icon><Odometer /></el-icon>
          <template #title>仪表盘</template>
        </el-menu-item>
        <el-menu-item index="/devices">
          <el-icon><Monitor /></el-icon>
          <template #title>设备管理</template>
        </el-menu-item>
        <el-menu-item index="/control">
          <el-icon><Switch /></el-icon>
          <template #title>设备控制</template>
        </el-menu-item>
        <el-menu-item index="/history">
          <el-icon><DataLine /></el-icon>
          <template #title>历史数据</template>
        </el-menu-item>
        <el-menu-item index="/alarms">
          <el-icon><Bell /></el-icon>
          <template #title>报警管理</template>
        </el-menu-item>
        <el-menu-item index="/alarm-output">
          <el-icon><Lightning /></el-icon>
          <template #title>报警输出</template>
        </el-menu-item>
        <el-menu-item index="/config">
          <el-icon><Setting /></el-icon>
          <template #title>系统配置</template>
        </el-menu-item>
        <el-menu-item index="/users">
          <el-icon><User /></el-icon>
          <template #title>用户管理</template>
        </el-menu-item>
      </el-menu>
    </el-aside>

    <!-- 主内容区 -->
    <el-container>
      <!-- 顶部栏 -->
      <el-header class="layout-header">
        <div class="header-left">
          <el-icon
            class="collapse-btn"
            :size="20"
            @click="appStore.toggleSidebar"
          >
            <Fold v-if="!appStore.sidebarCollapsed" />
            <Expand v-else />
          </el-icon>

          <!-- 模拟模式标签 -->
          <el-tag v-if="appStore.simulationMode" type="warning" size="small" effect="dark" class="ml-12">
            模拟模式
          </el-tag>
        </div>

        <div class="header-right">
          <!-- 系统状态 -->
          <el-badge :value="appStore.activeAlarmCount" :hidden="appStore.activeAlarmCount === 0" :max="99">
            <el-icon :size="18" class="header-icon"><Bell /></el-icon>
          </el-badge>

          <el-icon :size="18" class="header-icon system-status-dot" :class="{ online: appStore.systemStatus }">
            <CircleCheckFilled />
          </el-icon>

          <!-- 用户菜单 -->
          <el-dropdown trigger="click" @command="handleUserCommand">
            <span class="user-dropdown">
              <el-icon><UserFilled /></el-icon>
              <span>{{ authStore.displayName }}</span>
              <el-icon class="el-icon--right"><ArrowDown /></el-icon>
            </span>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item disabled>
                  <small>{{ authStore.roleName }}</small>
                </el-dropdown-item>
                <el-dropdown-item divided command="users">用户管理</el-dropdown-item>
                <el-dropdown-item command="password">修改密码</el-dropdown-item>
                <el-dropdown-item divided command="logout" class="text-danger">
                  退出登录
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </el-header>

      <!-- 页面内容 -->
      <el-main class="layout-main">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { ElMessageBox, ElMessage } from 'element-plus'
import { useAuthStore } from '@/stores/auth'
import { useAppStore } from '@/stores/app'

const router = useRouter()
const route = useRoute()
const authStore = useAuthStore()
const appStore = useAppStore()

const currentRoute = computed(() => route.path)

let statusTimer: ReturnType<typeof setInterval>

onMounted(async () => {
  // 验证登录状态
  await authStore.verify()
  // 获取系统状态
  appStore.fetchSystemStatus()
  // 定时刷新状态
  statusTimer = setInterval(() => {
    appStore.fetchSystemStatus()
  }, 10000)
})

onUnmounted(() => {
  clearInterval(statusTimer)
})

function handleUserCommand(command: string) {
  switch (command) {
    case 'users':
      router.push('/users')
      break
    case 'password':
      changePassword()
      break
    case 'logout':
      authStore.logout()
      router.push('/login')
      break
  }
}

async function changePassword() {
  try {
    const { value: oldPwd } = await ElMessageBox.prompt('请输入旧密码', '修改密码', {
      inputType: 'password',
      confirmButtonText: '下一步',
      cancelButtonText: '取消',
    })
    if (!oldPwd) return

    const { value: newPwd } = await ElMessageBox.prompt('请输入新密码（至少6位）', '修改密码', {
      inputType: 'password',
      confirmButtonText: '确认修改',
      cancelButtonText: '取消',
      inputValidator: (val) => (val && val.length >= 6) || '密码长度至少6位',
    })
    if (!newPwd) return

    const { authApi } = await import('@/api')
    const data = await authApi.changePassword(oldPwd, newPwd)
    if (data.success) {
      ElMessage.success('密码修改成功，请重新登录')
      authStore.logout()
      router.push('/login')
    } else {
      ElMessage.error(data.message || '修改失败')
    }
  } catch {
    // 用户取消
  }
}
</script>

<style scoped>
.layout-container {
  height: 100vh;
}

.layout-aside {
  background-color: #1d1e1f;
  transition: width 0.3s;
  overflow: hidden;
}

.logo {
  height: 60px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  color: #409eff;
  font-size: 20px;
  font-weight: bold;
  border-bottom: 1px solid #2d2d2d;
  white-space: nowrap;
}

.logo.collapsed .logo-text {
  display: none;
}

.sidebar-menu {
  border-right: none;
  height: calc(100vh - 60px);
  overflow-y: auto;
}

.layout-header {
  background: #fff;
  border-bottom: 1px solid #e6e6e6;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 20px;
  height: 60px;
}

.header-left {
  display: flex;
  align-items: center;
  gap: 12px;
}

.header-right {
  display: flex;
  align-items: center;
  gap: 18px;
}

.collapse-btn {
  cursor: pointer;
  color: #666;
}
.collapse-btn:hover {
  color: #409eff;
}

.header-icon {
  cursor: pointer;
  color: #666;
}
.header-icon:hover {
  color: #409eff;
}

.system-status-dot {
  color: #67c23a;
}

.user-dropdown {
  display: flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
  color: #333;
}

.layout-main {
  background: #f5f7fa;
  padding: 20px;
  overflow-y: auto;
}

.ml-12 {
  margin-left: 12px;
}

.text-danger {
  color: #f56c6c;
}
</style>
