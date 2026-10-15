# Tidepool 接入与控制候选

2026-10-02。现有设备的独立接收链保留；DotVoice 通过新增本机接口读取状态、切换输入来源、控制屏幕。没有启动第二个 UDP、串口或 OpenLess 音频转发进程。

## 当前证据与边界

已只读核实：安装接收器为 `%LOCALAPPDATA%\Tidepool\receiver\tidepool_receiver.py`，版本 `4.0.5`，UDP 端口 `17876`。原有 Python 运行器与实际解释器进程同时出现是父子启动关系，只有一个进程拥有该 UDP 端口。守护入口是同目录 `run.ps1`。原有自启动项为 `Tidepool Receiver (user logon)`。

接收器存活和端口监听不等于设备在线。旧日志不能证明本轮连接状态，因此旧版在界面中显示“需要更新并启用控制 API”；不会假装其已有远程控制能力。设备电量目前没有上报到电脑，保持未知。

本次修改了最新 `Tidepool OS/v4.0.5/source/pc/tidepool_receiver.py`，新增同目录 `tidepool_control.py`；原始接收器已备份到 `backups/tidepool-receiver-v405-before-control-20261002.py`，SHA256：

`AE41A536BCFD18574ABFDA49C05C33177CFBA5D669EAE929D3CCC904765D5E25`

**开发与模拟测试没有改动已安装副本、运行进程、自启动任务或设备固件，也没有录音。** 源码候选需要经明确的接收器更新流程部署，不能仅凭 DotVoice 已更新就称现有接收器也已升级。

## 控制 API

新接收器启动时默认创建仅 `127.0.0.1` 的随机 HTTP 端口，不占用现有 UDP 端口。`--no-control-api` 可关闭；`--control-endpoint-file` 可指定发现文件。

发现文件：`%LOCALAPPDATA%\Tidepool\control\endpoint.json`，包含 `schema=1`、`url`、`token`、`pid`。令牌随机生成，Windows 文件先设置为当前用户 SID 的专属 ACL 再写入。退出时仅删除属于本进程令牌的文件。令牌不能出现在日志或 UI 中。

请求头 `Authorization: Bearer <token>`，POST 另需 `Content-Type: application/json`。拒绝带浏览器 Origin 的请求，验证 Host，限制请求体 256 字节；客户端禁用代理与重定向。

- `GET /v1/status`：只返回接收器与设备状态，不含音频或转写内容。
- `POST /v1/control`，正文 `{"action":"pause"}`。仅接受 `pause`、`resume`、`cancel`、`wake`、`sleep`；没有远程开始录音的命令。

状态字段包括：`fresh`、`receiver_running`、`receiver_version`、`receiver_pid`、`enabled`、`recording`、`device_online`、`usb_connected`、`link`、`battery_percent`、`control_supported`、`cancel_pending`、`cancel_error`、`cancel_generation`、`asr`、`message`。

`device_online` 只由接收器实际收到并接受的设备报文更新，超过 10 秒即失效。API 快照超过 2 秒未刷新也不能作为切换依据。`usb_connected` 使用已有串口协议最近活动检测，端口打开本身不足以说明已连接。`battery_percent` 保持 null。

## 线程与生命周期

HTTP 线程只向主循环提交带截止时间的请求。主循环修改接收许可；音频工作线程拥有清队列、停止流和 OpenLess cancel；串口线程拥有屏幕命令写入。

`pause` 同时阻止 USB/UDP 新会话和音频帧；取消在途会话、清空缓存，拒绝恢复旧 session。`resume` 仅在取消完成且无错误时允许新的设备按键录音。`cancel` 取消当前输入，保留启用意图。暂停接收器不等于设备固件休眠，也不表示设备的物理麦克风已断电。

暂停的第一条 HTTP 应答只表明请求已应用到门控。DotVoice 客户端继续等待**同一 cancellation generation** 的音频工作线程完成，才返回控制成功；超时或错误会阻止切换到另一支笔。

使用翻页笔前先暂停 Tidepool 并等取消完成；使用 Tidepool 前先确认 DotVoice 的翻页笔控制器已完全释放音频/后端，然后恢复原接收器。两侧各保留自己的协议和唯一音频路径。

`wake` / `sleep` 仅在 USB 新鲜连接上接受，350 ms 截止时间，执行前再次核对同一串口对象；断开、重连、过期即丢弃。没有使用原 `.cmd` 文件，也没有把命令留到设备重连后补发。应答 `written=true, confirmed=false` 仅证明命令已写入当前串口，固件未提供可关联事务的画面状态 ACK，界面应显示“已发送唤醒/息屏请求”。

## 原始协议依据

- `source/pc/tidepool_receiver.py` 原版 `SerialLink`：USB 独占、DTR/RTS 低电平、队列线程；`AudioWorker`：输出线程；`AudioSession`：USB/UDP 共用会话门。
- `source/main/tp_link.h`：设备的 HELLO/PING/START/STOP 与 16 kHz PCM 帧；没有向电脑上报电量的消息。
- `source/main/tidepool.c`：`wake`、`sleep`、`voice-start/stop`、`link-status`、`status` 的固件处理。此次控制 API 不暴露 `voice-start`。
- `source/main/tp_battery.c` 和 `tp_powerlog.c`：设备本地真实电量与耗电日志存在，但原接收接口没有取得它们的协议。不能从标称容量编造百分比。

## 验证

- `tests/test_tidepool.py`：10 项通过，含临时私有令牌文件、真实本机 HTTP + 模拟状态的 status/pause/resume 闭环；没有物理设备参与。
- `source/tests/test_receiver_control.py`：9 项通过，覆盖鉴权、Origin、动作白名单、状态老化、双传输暂停、取消失败、旧会话拒绝、清音频队列、OpenLess cancel 和串口 TTL/连接代际。
- 接收器 `test_receiver*.py`：41 项通过，其中 1 项环境相关跳过。
- 原 `tools/test_receiver_audio.py`：20 项离线音频、会话与队列回归全部通过。
- 部署后的真实语音、USB/Wi-Fi 切换、唤醒/息屏效果和同 OpenLess 共存仍需实际验收。

## 本机部署记录 · 2026-10-02

用户明确同意“现在更新并重启接收器”后，已执行升级脚本 -Apply。接收器受控重启成功，新接口真实返回 control_supported=true、api_upgrade_required=false。配置、run.ps1 与 VBS 哈希和升级前一致，未改固件。

备份位于当前用户 LocalAppData/Tidepool/backups/dotvoice-control-20261002-193334-e0317dd8。可用 integrations/tidepool/install-control.ps1 -Rollback 指定此目录回退。

接收器进程已运行，当前设备无新鲜连接：device_online=false、usb_connected=false，电量未上报。已验证真实暂停/恢复，不启动录音。首次恢复检查发现“ACK先返回、状态快照后刷新”的短暂竞态，客户端增加状态确认后再复验。物理设备唤醒/息屏、USB/Wi-Fi及语音仍待实机配合。
