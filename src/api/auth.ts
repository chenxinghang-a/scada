import api from './request'

export interface LoginParams {
  username: string
  password: string
}

export interface UserInfo {
  username: string
  display_name: string
  role: string
  role_name: string
}

export const authApi = {
  login(params: LoginParams) {
    return api.post('/auth/login', params) as Promise<{
      success: boolean
      token: string
      refresh_token: string
      user: UserInfo
    }>
  },

  verify() {
    return api.get('/auth/verify') as Promise<{ valid: boolean; user: UserInfo }>
  },

  refreshToken(refreshToken: string) {
    return api.post('/auth/refresh', { refresh_token: refreshToken }) as Promise<{
      success: boolean
      token: string
    }>
  },

  changePassword(oldPassword: string, newPassword: string) {
    return api.post('/auth/change-password', {
      old_password: oldPassword,
      new_password: newPassword,
    }) as Promise<{ success: boolean; message: string }>
  },

  getUsers() {
    return api.get('/auth/users') as Promise<{ users: UserInfo[] }>
  },

  getLogs(params?: { page?: number; per_page?: number }) {
    return api.get('/auth/logs', { params })
  },
}
