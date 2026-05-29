# Resources

在此目录放置以下文件：

- `icon.ico` - 应用图标（256x256，Windows ICO 格式）
- `tray-icon.ico` - 系统托盘图标（32x32，Windows ICO 格式）

## 图标制作

可以使用以下工具生成 ICO 文件：
- https://convertico.com/ - 在线 PNG 转 ICO
- https://icoconvert.com/ - 在线图标转换

准备一个 1024x1024 的 PNG 图标，转换为 ICO 格式即可。

## 临时方案

如果暂时没有图标，electron-builder 会使用默认图标。
托盘图标不存在时会显示空白图标（不影响功能）。
