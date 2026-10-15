# 发布点声

客户端从 `https://api.github.com/repos/Francis-Zxp/DotVoice/releases/latest` 读取正式发行版。推送源码本身不会通知用户；发布完成且有对应 Windows 下载包后才会触发更新提醒。

1. 更新应用版本：Python Core、前端 package.json / mock、Tauri Cargo.toml / Cargo.lock 的 dotvoice 项、tauri.conf.json、build.ps1、package_release.py、安装器。不要替换依赖项的版本。添加本版 `RELEASE_<版本>.md` 并更新打包列表。
2. 运行 Python、前端、Rust 和相关界面回归；构建后运行 `DotVoice.exe --smoke`。真实设备验收与模拟测试分开记录。
3. 运行 `build.ps1` 和 `tools/package_release.py`。确认 Windows ZIP 内 `installed-files.json` 的所有 SHA-256，确认没有 runtime、笔记、密钥、调试录音或用户参考图片。
4. 推送代码并等待 GitHub Checks 通过。创建版本标签 `v<版本>`，先创建草稿 Release，上传以下三个完整资产，再取消草稿并设为 Latest：
   - `DotVoice-<版本>-windows-x64.zip`
   - `DotVoice-<版本>-source.zip`
   - `SHA256-<版本>.txt`
5. 匿名下载公开 Windows ZIP，对照本地 SHA-256；运行 `python tools/verify_update_channel.py --installed-version <上一版> --expected-version <本版>`，确认较旧版本能看到更新。

安装前从系统托盘退出点声。升级不会覆盖 `%LOCALAPPDATA%\DotVoice` 内的用户数据；安装前仍应保留可恢复的备份。不要把 GitHub Release 的校验和当作 Windows 发布者签名。

0.11.4 及更早客户端没有此更新机制，必须先手动升级一次。之后启动会检查，并在运行时每 6 小时轮询；网络不通时不保证即时收到提醒。客户端提供下载提示，不会静默安装或中断录音。
