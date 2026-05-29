const { autoUpdater } = require('electron-updater')
const { BrowserWindow, dialog, app } = require('electron')

let updateAvailable = false
let updateDownloaded = false

function setupUpdater(mainWindow) {
  // 配置更新源（GitHub Releases）
  autoUpdater.autoDownload = false
  autoUpdater.autoInstallOnAppQuit = true

  // 检查更新
  autoUpdater.on('checking-for-update', () => {
    console.log('正在检查更新...')
    sendToRenderer(mainWindow, 'update-status', { status: 'checking' })
  })

  // 发现新版本
  autoUpdater.on('update-available', (info) => {
    console.log('发现新版本:', info.version)
    updateAvailable = true
    sendToRenderer(mainWindow, 'update-status', {
      status: 'available',
      version: info.version,
      releaseNotes: info.releaseNotes,
    })

    // 询问用户是否下载
    dialog.showMessageBox(mainWindow, {
      type: 'info',
      title: '发现新版本',
      message: `SmartSCADA ${info.version} 已发布`,
      detail: '是否现在下载更新？',
      buttons: ['下载', '稍后'],
      defaultId: 0,
    }).then(({ response }) => {
      if (response === 0) {
        autoUpdater.downloadUpdate()
        sendToRenderer(mainWindow, 'update-status', { status: 'downloading' })
      }
    })
  })

  // 没有新版本
  autoUpdater.on('update-not-available', () => {
    console.log('当前已是最新版本')
    sendToRenderer(mainWindow, 'update-status', { status: 'up-to-date' })
  })

  // 下载进度
  autoUpdater.on('download-progress', (progress) => {
    sendToRenderer(mainWindow, 'update-progress', {
      percent: progress.percent,
      bytesPerSecond: progress.bytesPerSecond,
    })
  })

  // 下载完成
  autoUpdater.on('update-downloaded', (info) => {
    console.log('更新下载完成')
    updateDownloaded = true
    sendToRenderer(mainWindow, 'update-status', { status: 'downloaded', version: info.version })

    dialog.showMessageBox(mainWindow, {
      type: 'info',
      title: '更新已就绪',
      message: '更新已下载完成，是否立即重启安装？',
      buttons: ['立即重启', '稍后重启'],
      defaultId: 0,
    }).then(({ response }) => {
      if (response === 0) {
        autoUpdater.quitAndInstall()
      }
    })
  })

  // 错误处理
  autoUpdater.on('error', (err) => {
    console.error('更新检查失败:', err.message)
    sendToRenderer(mainWindow, 'update-status', { status: 'error', error: err.message })
  })
}

function checkForUpdates() {
  if (!updateDownloaded) {
    autoUpdater.checkForUpdates().catch(err => {
      console.error('检查更新失败:', err.message)
    })
  }
}

function sendToRenderer(mainWindow, channel, data) {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send(channel, data)
  }
}

module.exports = {
  setupUpdater,
  checkForUpdates,
}
