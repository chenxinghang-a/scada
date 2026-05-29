import api from './request'

export interface Device {
  device_id: string
  device_name: string
  protocol: string
  host?: string
  port?: number
  slave_id?: number
  enabled: boolean
  connected?: boolean
  registers?: Register[]
}

export interface Register {
  name: string
  address: number
  data_type: string
  scale: number
  unit: string
  description: string
  rw: string
}

export const devicesApi = {
  getAll() {
    return api.get('/devices') as Promise<{ devices: Device[] }>
  },

  getById(id: string) {
    return api.get(`/devices/${id}`) as Promise<{ device: Device }>
  },

  create(device: Partial<Device>) {
    return api.post('/devices', device)
  },

  update(id: string, device: Partial<Device>) {
    return api.put(`/devices/${id}`, device)
  },

  delete(id: string) {
    return api.delete(`/devices/${id}`)
  },

  test(id: string) {
    return api.post(`/devices/${id}/test`) as Promise<{ success: boolean; message: string }>
  },

  getProtocols() {
    return api.get('/devices/protocols') as Promise<{ protocols: string[] }>
  },

  getTemplates() {
    return api.get('/devices/templates') as Promise<{ templates: Record<string, any> }>
  },

  injectFault(id: string, fault: any) {
    return api.post(`/devices/${id}/inject-fault`, fault)
  },

  forceState(id: string, state: any) {
    return api.post(`/devices/${id}/force-state`, state)
  },
}
