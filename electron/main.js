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

// 检查端口是否被占用
function checkPort(port) {
  return new Promise((resolve) => {
    const server = net.createServer()
    server.once('error', () => resolve(true))  // 被占用
    server.once('listening', () => {
      server.close()
      resolve(false)  // 空闲
    })
    server.listen(port, '127.0.0.1')
  })
}

// 启动 Python 后端
async function startBackend() {
  const portInUse = await checkPort(BACKEND_PORT)
  if (portInUse) {
    console.log(`端口 ${BACKEND_PORT} 已被占用，跳过后端启动`)
    return
  }

  let backendPath
  if (isDev) {
    // 开发模式：不启动后端，需要手动运行 Flask
    console.log('开发模式：请手动启动 Flask 后端 (python run.py)')
    return
  } else {
    // 生产模式：启动打包的后端
    backendPath = path.join(process.resourcesPath, 'scada-backend.exe')
  }

  console.log(`启动后端: ${backendPath}`)
  backendProcess = spawn(backendPath, [], {
    cwd: path.dirname(backendPath),
    stdio: ['pipe', 'pipe', 'pipe'],
    windowsHide: true,
  })

  backendProcess.stdout.on('data', (data) => {
    console.log(`[Backend] ${data.toString().trim()}`)
  })

  backendProcess.stderr.on('data', (data) => {
    console.error(`[Backend] ${data.toString().trim()}`)
  })

  backendProcess.on('error', (err) => {
    console.error('后端启动失败:', err)
  })

  backendProcess.on('exit', (code) => {
    console.log(`后端进程退出，代码: ${code}`)
    backendProcess = null
  })
}

// 等待后端就绪
function waitForBackend(maxWait = 30000) {
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
    icon: path.join(__dirname, '..', 'resources', 'icon.ico'),
    show: false,  // 等待加载完成后再显示
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      nodeIntegration: false,
      contextIsolation: true,
    },
  })

  // 加载页面
  if (isDev) {
    mainWindow.loadURL('http://localhost:5173')
    mainWindow.webContents.openDevTools()
  } else {
    mainWindow.loadFile(path.join(__dirname, '..', 'dist', 'index.html'))
  }

  // 窗口准备好后显示
  mainWindow.once('ready-to-show', () => {
    mainWindow.show()
  })

  // 关闭时最小化到托盘
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
  } catch {
    // 如果图标不存在，使用默认
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
      label: '系统状态',
      enabled: false,
    },
    { type: 'separator' },
    {
      label: '退出',
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

// 应用生命周期
app.whenReady().then(async () => {
  createTray()

  // 启动后端
  await startBackend()

  // 等待后端就绪（非开发模式）
  if (!isDev) {
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
  // Windows 下不退出，保持托盘
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
  // 关闭后端进程
  if (backendProcess) {
    backendProcess.kill()
    backendProcess = null
  }
})

// IPC 通信
ipcMain.handle('get-app-version', () => {
  return app.getVersion()
})

ipcMain.handle('get-backend-status', async () => {
  const portInUse = await checkPort(BACKEND_PORT)
  return { running: portInUse, port: BACKEND_PORT }
})
