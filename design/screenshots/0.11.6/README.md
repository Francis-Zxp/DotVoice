# 点声 0.11.6 界面截图

2026-10-16，Windows 原生 Tauri/WebView2 程序截图。主窗口为正常、未最大化的 1200×800 逻辑尺寸；PNG 保留原生捕获的像素，没有重绘界面。

- `home-dark.png` / `home-light.png`：深色与浅色首页。
- `settings-feedback.png`：声音与胶囊设置全节。
- `settings-capsule.png`：显示范围、屏幕选择与九宫格。
- `capsule-note.png`：固定的示例便签胶囊。

截图使用独立的自检数据目录，保留实际暂停和未连接状态。便签内容是示例，没有使用个人笔记、录音或真实语音服务。透明区域后方使用受控背景。

生成入口为 `DotVoice.exe --smoke`，设置 `DOTVOICE_SMOKE_KEEP=1` 可保留原生 BMP；确认图像内容后无损转换为 PNG。截图用于说明界面，语音和硬件验收见项目根目录 `VALIDATION.md`。
