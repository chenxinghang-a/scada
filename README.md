# SmartSCADA

工业数据采集与监控系统 - 桌面版

## 技术栈

- **前端**: Vue 3 + TypeScript + Element Plus + ECharts
- **桌面**: Electron
- **后端**: Python Flask (SCADA毕业设计)

## 开发环境搭建

### 前置要求

- Node.js 18+
- Python 3.12+ (用于后端)
- npm 或 yarn

### 安装依赖

```bash
npm install
```

### 启动开发环境

1. 先启动 Flask 后端：
```bash
cd C:\Users\cxx\Desktop\SCADA毕业设计
python run.py
```

2. 启动 Electron + Vue 前端：
```bash
cd C:\Users\cxx\scada-app
npm run electron:dev
```

### 仅前端开发（不启动 Electron）

```bash
npm run dev
```

访问 http://localhost:5173

## 打包发布

### 1. 打包 Python 后端

```bash
cd C:\Users\cxx\Desktop\SCADA毕业设计
pip install pyinstaller
pyinstaller --name scada-backend --onefile --hidden-import=用户层 --hidden-import=用户层.auth run.py
```

将生成的 `dist/scada-backend.exe` 复制到 `scada-app/backend/`

### 2. 打包 Electron 应用

```bash
cd C:\Users\cxx\scada-app
npm run electron:build
```

生成的安装包在 `release/` 目录。

## 项目结构

```
scada-app/
├── electron/          # Electron 主进程
│   ├── main.js        # 主进程入口
│   ├── preload.js     # 预加载脚本
│   ├── updater.js     # 自动更新
│   └── first-run.js   # 首次启动配置
├── src/               # Vue 3 前端
│   ├── api/           # API 请求
│   ├── components/    # 组件
│   ├── router/        # 路由
│   ├── stores/        # 状态管理
│   └── views/         # 页面
├── backend/           # Python 后端 (打包产物)
└── resources/         # 图标等资源
```

## 自动更新

使用 GitHub Releases 作为更新源：

1. 在 GitHub 创建 Release
2. 上传打包产物（exe 安装包 + yml 文件）
3. 应用启动时自动检查更新

## 配置文件

- `package.json` - Electron 配置
- `vite.config.ts` - Vite 构建配置
- `electron-builder.yml` - 打包配置（可选）
