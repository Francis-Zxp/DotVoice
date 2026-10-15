# Tidepool 接收器控制接口升级

这份脚本给当前用户已经安装的 Tidepool 接收器增加本机控制接口，供 DotVoice 查询状态、暂停、恢复及取消。设备固件不需要重刷。候选接收器会与 `tidepool_control.py` 一起安装，继续使用原有启动器、Python 环境和配置。

2026-10-02，本机在用户明确同意后已完成备份、升级和受控重启，新接口可查询。其他机器仍需按下述流程安装，复制 DotVoice 本身不会自动修改接收器。物理设备语音及屏幕效果仍需另行验收。

## 文件

```text
integrations/tidepool/
  install-control.ps1
  README.md
  receiver/
    tidepool_receiver.py
    tidepool_control.py
```

候选文件必须齐全才能执行 `-Apply`。默认安装位置固定为 `%LOCALAPPDATA%\Tidepool`，当前版本只支持原安装器创建的 `receiver`、`venv` 目录布局；自定义启动方式会被拒绝，不猜测或修改其它安装。

## 检查、升级、回退

在此目录打开 PowerShell，先检查：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install-control.ps1 -Check
```

不带参数也等同于 `-Check`。它只核对安装目录、候选文件、精确的接收器/守护进程及计划任务，不结束进程、不创建备份、不下载文件。输出 `payload_ready: true` 表示两个候选文件齐全，并不代表升级已完成。

准备升级时，先结束正在进行的录音，并暂停 DotVoice 的翻页笔语音输入，再明确运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install-control.ps1 -Apply
```

脚本会先检查候选 Python 源码语法，备份现有接收器、已有控制模块、`run.ps1`、`run_hidden.vbs`、`config.json`、计划任务 XML 及存在的启动快捷方式，再进行一次受控重启。备份路径会打印到终端。原本未运行的接收器会保持停止。配置的备份只用于留档，升级和回退都不会覆盖当前配置。

升级后，通过仅限 `127.0.0.1` 的认证状态接口检查实际接收器 PID 和新鲜状态；令牌不会打印。若检查失败，会明确报告，不能把它当作已验证可用。此时查看 `%LOCALAPPDATA%\Tidepool\logs\rx.log`，或按输出的备份目录回退：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install-control.ps1 -Rollback "C:\Users\你的用户名\AppData\Local\Tidepool\backups\dotvoice-control-时间戳"
```

回退前也会备份当前版本。回退只恢复当时的 `tidepool_receiver.py`、`tidepool_control.py`；如果原本没有控制模块，就移除本次新增的那个文件。既有启动配置未被替换，因此无需重新注册开机启动。回退到旧版后，DotVoice 会再次显示“旧接收器尚未支持控制”。

## 进程与数据边界

- 只接受精确的当前安装路径和命令行参数，核对进程实际可执行文件、创建时间、当前用户 SID；PID 被复用或出现不认识的启动方式时停止操作。
- 原守护每 3 秒重启接收器，因此先临时禁用匹配的开机任务、停止精确守护，再停止精确接收器。结束后恢复任务原有启用状态；不会执行“结束所有 Python”。
- 同时识别虚拟环境启动器和 `pyvenv.cfg` 指向的 Python 子进程，不以进程名或模糊的 `Tidepool` 文本作为结束进程的依据。
- 不修改 `config.json`、OpenLess PIN、识别配置、Wi-Fi、固件或串口暂停文件；不发送设备串口命令。配置文件仅用于读取当前 Python 路径。
- 只替换上述两个 Python 源码文件；复制失败会恢复本次备份。启动或接口检查失败时保留明确错误和可回退路径，不声称成功。
- 不需要管理员安装。若本机策略拒绝当前用户读取进程或管理自己的计划任务，脚本会报错，未核实的进程不会被强行处理。

更新完成后，启动 DotVoice 即可通过新的状态接口控制 Tidepool。实际语音输入仍由原 Tidepool 接收器负责，DotVoice 不再打开第二条 Tidepool 音频接收通路。
