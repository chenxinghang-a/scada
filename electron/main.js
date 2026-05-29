const { app, BrowserWindow, Tray, Menu, nativeImage, ipcMain, dialog } = require('electron')
const path = require('path')
const { spawn } = require('child_process')
const net = require('net')

// 单实例锁
const gotTheLock = app.requestSingleInstanceLock()
if (!gotTheLock) {
  app.quit()
}

let mainWindow = null
let tray = null
let backendProcess = null
const BACKEND_PORT = 5000
const isDev = !app.isPackaged

// 获取后端 exe 路径
function getBackendPath() {
  if (isDev) {
    // 开发模式：从项目根目录的 backend 文件夹
    return path.join(__dirname, '..', 'backend', 'scada-backend.exe')
  }
  // 生产模式：从 resources 目录
  return path.join(process.resourcesPath, 'backend', 'scada-backend.exe')
}

// 检查端口是否被占用
function checkPort(port) {
  return new Promise((resolve) => {
    const server = net.createServer()
    server.once('error', () => resolve(true))
    server.once('listening', () => {
      server.close()
      resolve(false)
    })
    server.listen(port, '127.0.0.1')
  })
}

// 启动 Python 后端
async function startBackend() {
  const portInUse = await checkPort(BACKEND_PORT)
  if (portInUse) {
    console.log(`端口 ${BACKEND_PORT} 已被占用，跳过后端启动`)
    return true
  }

  const backendPath = getBackendPath()
  const backendDir = path.dirname(backendPath)

  console.log(`启动后端: ${backendPath}`)
  console.log(`工作目录: ${backendDir}`)

  try {
    backendProcess = spawn(backendPath, [], {
      cwd: backendDir,
      stdio: ['pipe', 'pipe', 'pipe'],
      windowsHide: true,
    })

    backendProcess.stdout.on('data', (data) => {
      const msg = data.toString().trim()
      if (msg) console.log(`[Backend] ${msg}`)
    })

    backendProcess.stderr.on('data', (data) => {
      const msg = data.toString().trim()
      if (msg) console.error(`[Backend] ${msg}`)
    })

    backendProcess.on('error', (err) => {
      console.error('后端启动失败:', err)
    })

    backendProcess.on('exit', (code) => {
      console.log(`后端进程退出，代码: ${code}`)
      backendProcess = null
    })

    return true
  } catch (err) {
    console.error('启动后端异常:', err)
    return false
  }
}

// 等待后端就绪
function waitForBackend(maxWait = 60000) {
  return new Promise((resolve, reject) => {
    const startTime = Date.now()
    const check = () => {
      if (Date.now() - startTime > maxWait) {
        reject(new Error('后端启动超时'))
        return
      }
      const req = net.createConnection(BACKEND_PORT, '127.0.0.1')
      req.on('connect', () => {
        req.destroy()
        resolve()
      })
      req.on('error', () => {
        setTimeout(check, 500)
      })
    }
    check()
  })
}

// 创建主窗口
function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1400,
    height: 900,
    minWidth: 1024,
    minHeight: 700,
    title: 'SmartSCADA',
    show: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      nodeIntegration: false,
      contextIsolation: true,
    },
  })

  if (isDev) {
    // 开发模式：加载 Vite dev server
    mainWindow.loadURL('http://localhost:5173')
    mainWindow.webContents.openDevTools()
  } else {
    // 生产模式：加载打包后的前端
    mainWindow.loadFile(path.join(__dirname, '..', 'dist', 'index.html'))
  }

  mainWindow.once('ready-to-show', () => {
    mainWindow.show()
  })

  mainWindow.on('close', (e) => {
    if (!app.isQuitting) {
      e.preventDefault()
      mainWindow.hide()
    }
  })

  mainWindow.on('closed', () => {
    mainWindow = null
  })
}

// 创建系统托盘
function createTray() {
  const iconPath = path.join(__dirname, '..', 'resources', 'tray-icon.ico')
  let icon
  try {
    icon = nativeImage.createFromPath(iconPath)
    if (icon.isEmpty()) throw new Error('empty')
  } catch {
    // 没有图标文件就用空图标
    icon = nativeImage.createEmpty()
  }

  tray = new Tray(icon)
  tray.setToolTip('SmartSCADA')

  const contextMenu = Menu.buildFromTemplate([
    {
      label: '显示主窗口',
      click: () => {
        if (mainWindow) {
          mainWindow.show()
          mainWindow.focus()
        }
      },
    },
    { type: 'separator' },
    {
      label: '退出 SmartSCADA',
      click: () => {
        app.isQuitting = true
        app.quit()
      },
    },
  ])

  tray.setContextMenu(contextMenu)
  tray.on('double-click', () => {
    if (mainWindow) {
      mainWindow.show()
      mainWindow.focus()
    }
  })
}

// 启动
app.whenReady().then(async () => {
  createTray()

  // 启动后端
  const started = await startBackend()

  if (started) {
    // 等待后端端口就绪
    try {
      await waitForBackend()
      console.log('后端已就绪')
    } catch (err) {
      console.error(err.message)
      dialog.showErrorBox('启动失败', '后端服务启动超时，请检查配置后重试。')
      app.quit()
      return
    }
  }

  createWindow()
})

app.on('window-all-closed', () => {
  // Windows 下保持托盘运行
})

app.on('activate', () => {
  if (mainWindow === null) {
    createWindow()
  } else {
    mainWindow.show()
  }
})

app.on('before-quit', () => {
  app.isQuitting = true
  if (backendProcess) {
    backendProcess.kill()
    backendProcess = null
  }
})

// IPC
ipcMain.handle('get-app-version', () => app.getVersion())
ipcMain.handle('get-backend-status', async () => {
  const running = await checkPort(BACKEND_PORT)
  return { running, port: BACKEND_PORT }
})
