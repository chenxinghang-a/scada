const { app } = require('electron')
const path = require('path')
const fs = require('fs')

const CONFIG_FILE = 'first-run-complete.json'

function getConfigPath() {
  return path.join(app.getPath('userData'), CONFIG_FILE)
}

function isFirstRun() {
  return !fs.existsSync(getConfigPath())
}

function markComplete() {
  const configPath = getConfigPath()
  fs.writeFileSync(configPath, JSON.stringify({
    completedAt: new Date().toISOString(),
    version: app.getVersion(),
  }, null, 2))
}

function getFirstRunConfig() {
  const configPath = getConfigPath()
  if (fs.existsSync(configPath)) {
    return JSON.parse(fs.readFileSync(configPath, 'utf-8'))
  }
  return null
}

module.exports = {
  isFirstRun,
  markComplete,
  getFirstRunConfig,
}
