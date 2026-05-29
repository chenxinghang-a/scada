import { createRouter, createWebHashHistory } from 'vue-router'
import MainLayout from '@/components/MainLayout.vue'

const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    {
      path: '/login',
      name: 'Login',
      component: () => import('@/views/Login.vue'),
    },
    {
      path: '/',
      component: MainLayout,
      redirect: '/dashboard',
      children: [
        {
          path: 'dashboard',
          name: 'Dashboard',
          component: () => import('@/views/Dashboard.vue'),
          meta: { title: '仪表盘', icon: 'Odometer' },
        },
        {
          path: 'devices',
          name: 'Devices',
          component: () => import('@/views/Devices.vue'),
          meta: { title: '设备管理', icon: 'Monitor' },
        },
        {
          path: 'control',
          name: 'Control',
          component: () => import('@/views/Control.vue'),
          meta: { title: '设备控制', icon: 'Switch' },
        },
        {
          path: 'history',
          name: 'History',
          component: () => import('@/views/History.vue'),
          meta: { title: '历史数据', icon: 'DataLine' },
        },
        {
          path: 'alarms',
          name: 'Alarms',
          component: () => import('@/views/Alarms.vue'),
          meta: { title: '报警管理', icon: 'Bell' },
        },
        {
          path: 'alarm-output',
          name: 'AlarmOutput',
          component: () => import('@/views/AlarmOutput.vue'),
          meta: { title: '报警输出', icon: 'Lightning' },
        },
        {
          path: 'config',
          name: 'Config',
          component: () => import('@/views/Config.vue'),
          meta: { title: '系统配置', icon: 'Setting' },
        },
        {
          path: 'users',
          name: 'Users',
          component: () => import('@/views/Users.vue'),
          meta: { title: '用户管理', icon: 'User' },
        },
      ],
    },
  ],
})

// 路由守卫 - 检查登录状态
router.beforeEach((to, _from, next) => {
  const token = localStorage.getItem('auth_token')
  if (to.path !== '/login' && !token) {
    next('/login')
  } else {
    next()
  }
})

export default router
