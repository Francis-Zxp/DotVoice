//! 兼容管理员权限窗口 (Settings → 通用, prefs `general.run_elevated`).
//!
//! Windows drops synthetic input aimed at an elevated window (UIPI): a non-elevated
//! DotVoice cannot click, scroll or type into, say, 红果短剧 inside the elevated Androws.exe.
//! The core notices and says so (bridge/elevation.py); this module lets DotVoice itself run
//! as administrator, only when the user turns the setting on:
//!
//! * On: one UAC prompt runs `DotVoice.exe --elevation-task create <sid>` elevated, which
//!   registers the logon task "DotVoice (管理员)" (RunLevel highest, this user only) that
//!   starts `DotVoice.exe --hidden --elevated-task`. This instance then quits gracefully and
//!   a small relauncher (`--relaunch <pid> task-show`) starts the task once it is gone - a
//!   task the user registered runs elevated without any prompt, now and at every logon.
//! * Off: the task is deleted (through the elevated helper when this instance is not
//!   elevated) and an elevated instance restarts at medium integrity through explorer.exe.
//! * A later start from a shortcut (medium integrity) while the task exists hands over to
//!   the task the same way, so DotVoice never runs half-elevated while the setting is on.
//!   The task's own start (`--elevated-task`) and the relauncher's fallback (`--no-elevate`)
//!   never hand over again: no loops.
//!
//! DotVoice has no other autostart (no Run key, no Startup shortcut; README: 默认不开机自启),
//! so this task is the only one; installer/uninstall.ps1 deletes it.
//!
//! Single instance: an elevated instance would never hear a second, non-elevated start
//! (UIPI drops its WM_COPYDATA), so [`accept_handover`] lets exactly that message through
//! to the single-instance plugin's window. The plugin itself cannot even tell that such a
//! start is the second one (the elevated instance's mutex denies it access), so
//! [`forward_to_running`] sends that message before the plugin runs. schtasks.exe and the
//! helpers run without a console window; no PowerShell is involved.

use serde_json::{json, Value};
use std::ffi::c_void;
use std::fs::OpenOptions;
use std::io::Write;
use std::os::windows::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::ptr::{null, null_mut};
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use windows_sys::Win32::Foundation::{
    CloseHandle, GetLastError, LocalFree, ERROR_CANCELLED, HANDLE, HWND, WAIT_OBJECT_0,
};
use windows_sys::Win32::Globalization::{MultiByteToWideChar, WideCharToMultiByte, CP_OEMCP};
use windows_sys::Win32::Security::Authorization::ConvertSidToStringSidW;
use windows_sys::Win32::Security::{
    GetTokenInformation, TokenElevation, TokenElevationType, TokenElevationTypeLimited, TokenUser,
    TOKEN_ELEVATION, TOKEN_ELEVATION_TYPE, TOKEN_QUERY, TOKEN_USER,
};
use windows_sys::Win32::System::Com::{CoInitializeEx, CoUninitialize, COINIT_APARTMENTTHREADED, COINIT_DISABLE_OLE1DDE};
use windows_sys::Win32::System::DataExchange::COPYDATASTRUCT;
use windows_sys::Win32::System::SystemInformation::{GetSystemDirectoryW, GetWindowsDirectoryW};
use windows_sys::Win32::System::Threading::{
    GetCurrentProcess, GetCurrentProcessId, GetExitCodeProcess, OpenProcess, OpenProcessToken, QueryFullProcessImageNameW,
    WaitForSingleObject, PROCESS_NAME_WIN32, PROCESS_QUERY_LIMITED_INFORMATION,
    PROCESS_SYNCHRONIZE,
};
use windows_sys::Win32::UI::Shell::{
    ShellExecuteExW, SEE_MASK_FLAG_NO_UI, SEE_MASK_NOASYNC, SEE_MASK_NOCLOSEPROCESS, SHELLEXECUTEINFOW,
};
use windows_sys::Win32::UI::WindowsAndMessaging::{
    AllowSetForegroundWindow, ChangeWindowMessageFilterEx, FindWindowW, GetWindowThreadProcessId, SendMessageTimeoutW,
    MSGFLT_ALLOW, SMTO_ABORTIFHUNG, SW_HIDE, WM_COPYDATA,
};

pub const TASK_NAME: &str = "DotVoice (管理员)";
/// The task's own start: elevated by the task, never handed over again.
pub const FROM_TASK: &str = "--elevated-task";
/// Started by the relauncher's fallback: stay as is, never hand over again.
pub const NO_ELEVATE: &str = "--no-elevate";
pub const HIDDEN: &str = "--hidden";
const HELPER: &str = "--elevation-task";
const RELAUNCH: &str = "--relaunch";
const CREATE_NO_WINDOW: u32 = 0x0800_0000;
/// How long the relauncher waits for the old instance (its core may take 45 s to let go).
const OLD_INSTANCE_WAIT: Duration = Duration::from_secs(90);
/// How long a UAC prompt may stay open.
const UAC_WAIT: Duration = Duration::from_secs(300);
/// "Show the window once": the task always starts --hidden; a restart the user asked for
/// leaves this marker so the elevated instance comes up with its window.
const SHOW_MARKER: &str = "DotVoice-show-on-elevated-start";
const MARKER_FRESH_SECS: u64 = 120;
/// tauri-plugin-single-instance's WM_COPYDATA tag (WMCOPYDATA_SINGLE_INSTANCE_DATA).
const SINGLE_INSTANCE_DATA: usize = 1542;

// Helper exit codes (the elevated helper reports through its exit code only).
const EXIT_OK: i32 = 0;
const EXIT_FAILED: i32 = 1;
const EXIT_NOT_ELEVATED: i32 = 2;
const EXIT_OTHER_ACCOUNT: i32 = 3;
const EXIT_SCHTASKS: i32 = 10; // + schtasks' own exit code

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Token {
    pub elevated: bool,
    /// An administrator account behind UAC (a split token), or already elevated.
    pub can_elevate: bool,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum TaskState {
    Missing,
    /// Registered for this DotVoice.exe.
    Ours,
    /// Registered for another copy (a moved portable folder, a development build).
    Other(String),
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Startup {
    Stay,
    HandOver { show: bool },
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Relaunch {
    /// Start the elevated logon task (show: with its window).
    Task { show: bool },
    /// Start DotVoice again at medium integrity.
    Normal,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TaskOp {
    Create,
    Delete,
}

impl TaskOp {
    fn word(self) -> &'static str {
        match self {
            TaskOp::Create => "create",
            TaskOp::Delete => "delete",
        }
    }
}

// -- pure parts (unit-tested) ---------------------------------------------------------

pub fn xml_escape(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for c in text.chars() {
        match c {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            '\'' => out.push_str("&apos;"),
            _ => out.push(c),
        }
    }
    out
}

fn xml_unescape(text: &str) -> String {
    text.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", "\"").replace("&apos;", "'").replace("&amp;", "&")
}

/// The logon task. Battery and time limits are off (a laptop on battery, a PC up for
/// weeks); priority 4 is a normal process (the scheduler's default 7 is below normal).
pub fn task_xml(exe: &Path, sid: &str) -> String {
    let command = xml_escape(&exe.to_string_lossy());
    let folder = xml_escape(&exe.parent().map(|p| p.to_string_lossy().into_owned()).unwrap_or_default());
    let sid = xml_escape(sid);
    format!(
        r#"<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Author>DotVoice</Author>
    <Description>点声 DotVoice：登录 Windows 时以管理员身份在后台启动，让模拟点击和输入也能用于以管理员权限运行的窗口。在点声 设置 → 通用 → 兼容管理员权限窗口 关闭，或卸载点声，会删除此任务。</Description>
    <URI>\{name}</URI>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{sid}</UserId>
      <Delay>PT5S</Delay>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{sid}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>false</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>4</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{hidden} {from_task}</Arguments>
      <WorkingDirectory>{folder}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"#,
        name = xml_escape(TASK_NAME),
        hidden = HIDDEN,
        from_task = FROM_TASK,
    )
}

/// schtasks /XML wants UTF-16 (little endian, with a byte order mark).
pub fn utf16_file(text: &str) -> Vec<u8> {
    let mut bytes = vec![0xFF, 0xFE];
    for unit in text.encode_utf16() {
        bytes.extend_from_slice(&unit.to_le_bytes());
    }
    bytes
}

fn element(xml: &str, name: &str) -> Option<String> {
    let open = format!("<{name}>");
    let close = format!("</{name}>");
    let start = xml.find(&open)? + open.len();
    let end = start + xml[start..].find(&close)?;
    Some(xml_unescape(xml[start..end].trim()))
}

/// The task's program (`<Command>`), as `schtasks /Query /XML` prints it.
pub fn task_command(xml: &str) -> Option<String> {
    element(xml, "Command").map(|c| c.trim_matches('"').to_string()).filter(|c| !c.is_empty())
}

pub fn same_path(a: &str, b: &Path) -> bool {
    let norm = |s: &str| s.trim().trim_matches('"').replace('/', "\\").trim_end_matches('\\').to_lowercase();
    norm(a) == norm(&b.to_string_lossy())
}

/// What the query printed (None: no such task) says about this DotVoice.exe.
pub fn classify(xml: Option<&str>, exe: &Path) -> TaskState {
    match xml.and_then(task_command) {
        None => TaskState::Missing,
        Some(command) if same_path(&command, exe) => TaskState::Ours,
        Some(command) => TaskState::Other(command),
    }
}

/// As [`classify`], for a query printed through a code page that turns what it cannot show
/// into '?' (an English system, a Chinese user name): `lossy` makes the same trip with this
/// exe's path. '?' is never part of a real path.
pub fn classify_printed(xml: Option<&str>, exe: &Path, lossy: impl Fn(&str) -> String) -> TaskState {
    match classify(xml, exe) {
        TaskState::Other(command) if command.contains('?') && same_path(&command, Path::new(&lossy(&exe.to_string_lossy()))) => {
            TaskState::Ours
        }
        state => state,
    }
}

/// Should a fresh, first instance hand over to the elevated task? `task` (a schtasks
/// query, ~60 ms) runs only for an administrator account that is not elevated yet.
pub fn startup(token: Token, args: &[String], task: impl FnOnce() -> TaskState) -> Startup {
    if token.elevated || !token.can_elevate {
        return Startup::Stay;
    }
    if args.iter().any(|a| a == FROM_TASK || a == NO_ELEVATE || a == "--smoke") {
        return Startup::Stay;
    }
    match task() {
        TaskState::Ours => Startup::HandOver { show: !args.iter().any(|a| a == HIDDEN) },
        _ => Startup::Stay,
    }
}

/// One Windows command-line argument (CommandLineToArgvW rules), for ShellExecute.
pub fn quote_arg(arg: &str) -> String {
    if !arg.is_empty() && !arg.contains([' ', '\t', '"']) {
        return arg.to_string();
    }
    let mut out = String::from("\"");
    let mut slashes = 0;
    for c in arg.chars() {
        match c {
            '\\' => slashes += 1,
            '"' => {
                out.push_str(&"\\".repeat(slashes * 2 + 1));
                out.push('"');
                slashes = 0;
            }
            _ => {
                out.push_str(&"\\".repeat(slashes));
                out.push(c);
                slashes = 0;
            }
        }
    }
    out.push_str(&"\\".repeat(slashes * 2));
    out.push('"');
    out
}

/// The elevated helper's parameters (ShellExecute "runas").
pub fn helper_params(op: TaskOp, sid: &str) -> String {
    [HELPER, op.word(), sid].iter().map(|a| quote_arg(a)).collect::<Vec<_>>().join(" ")
}

pub fn relaunch_args(pid: u32, how: Relaunch) -> Vec<String> {
    let word = match how {
        Relaunch::Task { show: true } => "task-show",
        Relaunch::Task { show: false } => "task",
        Relaunch::Normal => "normal",
    };
    vec![RELAUNCH.into(), pid.to_string(), word.into()]
}

/// The single-instance plugin's hand-over text: "cwd|arg0|arg1…", NUL-terminated.
pub fn handover_payload(cwd: &str, args: &[String]) -> String {
    format!("{cwd}|{}\0", args.join("|"))
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Helper {
    Task(TaskOp, String),
    Relaunch(u32, Relaunch),
}

/// `DotVoice.exe --elevation-task create <sid>` / `--relaunch <pid> <how>`: a helper run
/// that never becomes an app instance. Anything else (or malformed) is a normal start.
pub fn parse_helper(args: &[String]) -> Option<Helper> {
    match args.get(1).map(String::as_str)? {
        HELPER => {
            let op = match args.get(2).map(String::as_str)? {
                "create" => TaskOp::Create,
                "delete" => TaskOp::Delete,
                _ => return None,
            };
            let sid = args.get(3)?;
            sid.starts_with("S-1-").then(|| Helper::Task(op, sid.clone()))
        }
        RELAUNCH => {
            let pid = args.get(2)?.parse::<u32>().ok().filter(|p| *p != 0)?;
            let how = match args.get(3).map(String::as_str)? {
                "task" => Relaunch::Task { show: false },
                "task-show" => Relaunch::Task { show: true },
                "normal" => Relaunch::Normal,
                _ => return None,
            };
            Some(Helper::Relaunch(pid, how))
        }
        _ => None,
    }
}

pub fn marker_fresh(written: u64, now: u64) -> bool {
    written <= now + 5 && now - written.min(now) <= MARKER_FRESH_SECS
}

/// The user-facing reason an elevated helper run failed.
pub fn helper_error(op: TaskOp, code: u32) -> String {
    match code as i32 {
        EXIT_OTHER_ACCOUNT => "管理员确认用的是另一个 Windows 账户，设置未更改；请用当前账户确认（当前账户需要是管理员）".into(),
        EXIT_NOT_ELEVATED => "没有获得管理员权限，设置未更改".into(),
        c if c >= EXIT_SCHTASKS => format!(
            "无法{}计划任务「{TASK_NAME}」（schtasks 返回 {}）",
            if op == TaskOp::Create { "登记" } else { "删除" },
            c - EXIT_SCHTASKS
        ),
        c => format!("管理员操作没有完成（代码 {c}），设置未更改"),
    }
}

// -- Windows ----------------------------------------------------------------------------

fn wide(text: &str) -> Vec<u16> {
    text.encode_utf16().chain(std::iter::once(0)).collect()
}

fn wide_os(path: &Path) -> Vec<u16> {
    use std::os::windows::ffi::OsStrExt;
    path.as_os_str().encode_wide().chain(std::iter::once(0)).collect()
}

fn now_secs() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
}

pub fn token() -> Token {
    unsafe {
        let mut handle: HANDLE = null_mut();
        if OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut handle) == 0 {
            return Token { elevated: false, can_elevate: false };
        }
        let mut size = 0u32;
        let mut elevation = TOKEN_ELEVATION { TokenIsElevated: 0 };
        let ok = GetTokenInformation(
            handle,
            TokenElevation,
            &mut elevation as *mut _ as *mut c_void,
            std::mem::size_of::<TOKEN_ELEVATION>() as u32,
            &mut size,
        );
        let mut kind: TOKEN_ELEVATION_TYPE = 0;
        let typed = GetTokenInformation(
            handle,
            TokenElevationType,
            &mut kind as *mut _ as *mut c_void,
            std::mem::size_of::<TOKEN_ELEVATION_TYPE>() as u32,
            &mut size,
        );
        CloseHandle(handle);
        let elevated = ok != 0 && elevation.TokenIsElevated != 0;
        Token { elevated, can_elevate: elevated || (typed != 0 && kind == TokenElevationTypeLimited) }
    }
}

/// This process's user SID ("S-1-5-21-…").
pub fn current_user_sid() -> Option<String> {
    unsafe {
        let mut handle: HANDLE = null_mut();
        if OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut handle) == 0 {
            return None;
        }
        let mut size = 0u32;
        GetTokenInformation(handle, TokenUser, null_mut(), 0, &mut size);
        let mut buffer = vec![0u64; (size as usize).div_ceil(8).max(1)];
        let ok = GetTokenInformation(handle, TokenUser, buffer.as_mut_ptr() as *mut c_void, size, &mut size);
        CloseHandle(handle);
        if ok == 0 {
            return None;
        }
        let user = &*(buffer.as_ptr() as *const TOKEN_USER);
        let mut text = null_mut::<u16>();
        if ConvertSidToStringSidW(user.User.Sid, &mut text) == 0 || text.is_null() {
            return None;
        }
        let len = (0..).take_while(|&i| *text.add(i) != 0).count();
        let sid = String::from_utf16_lossy(std::slice::from_raw_parts(text, len));
        LocalFree(text as *mut c_void);
        Some(sid)
    }
}

fn windows_folder(system: bool) -> PathBuf {
    let mut buffer = [0u16; 512];
    let len = unsafe {
        if system {
            GetSystemDirectoryW(buffer.as_mut_ptr(), buffer.len() as u32)
        } else {
            GetWindowsDirectoryW(buffer.as_mut_ptr(), buffer.len() as u32)
        }
    } as usize;
    if len == 0 || len >= buffer.len() {
        let root = std::env::var("SystemRoot").unwrap_or_else(|_| r"C:\Windows".into());
        return if system { PathBuf::from(root).join("System32") } else { PathBuf::from(root) };
    }
    PathBuf::from(String::from_utf16_lossy(&buffer[..len]))
}

/// schtasks.exe by its full path (never whatever PATH finds), without a console window.
fn schtasks() -> Command {
    let mut command = Command::new(windows_folder(true).join("schtasks.exe"));
    command.creation_flags(CREATE_NO_WINDOW).stdin(Stdio::null()).stderr(Stdio::null());
    command
}

/// schtasks prints in the console (OEM) code page: GBK on a Chinese system.
fn decode_oem(bytes: &[u8]) -> String {
    if bytes.starts_with(&[0xFF, 0xFE]) {
        let units: Vec<u16> = bytes[2..].chunks_exact(2).map(|p| u16::from_le_bytes([p[0], p[1]])).collect();
        return String::from_utf16_lossy(&units);
    }
    if bytes.is_empty() || bytes.len() > i32::MAX as usize {
        return String::new();
    }
    unsafe {
        let len = MultiByteToWideChar(CP_OEMCP, 0, bytes.as_ptr(), bytes.len() as i32, null_mut(), 0);
        if len <= 0 {
            return String::from_utf8_lossy(bytes).into_owned();
        }
        let mut units = vec![0u16; len as usize];
        MultiByteToWideChar(CP_OEMCP, 0, bytes.as_ptr(), bytes.len() as i32, units.as_mut_ptr(), len);
        String::from_utf16_lossy(&units)
    }
}

/// `text` after a trip through the OEM code page, as schtasks prints it.
fn oem_lossy(text: &str) -> String {
    let units: Vec<u16> = text.encode_utf16().collect();
    if units.is_empty() || units.len() > i32::MAX as usize {
        return text.to_string();
    }
    unsafe {
        let len = WideCharToMultiByte(CP_OEMCP, 0, units.as_ptr(), units.len() as i32, null_mut(), 0, null(), null_mut());
        if len <= 0 {
            return text.to_string();
        }
        let mut bytes = vec![0u8; len as usize];
        WideCharToMultiByte(CP_OEMCP, 0, units.as_ptr(), units.len() as i32, bytes.as_mut_ptr(), len, null(), null_mut());
        decode_oem(&bytes)
    }
}

pub fn exe() -> Option<PathBuf> {
    std::env::current_exe().ok()
}

/// Does the task exist, and for this DotVoice.exe? (Read-only; ~60 ms.)
pub fn query_task() -> TaskState {
    let Some(exe) = exe() else { return TaskState::Missing };
    let output = schtasks().args(["/Query", "/TN", TASK_NAME, "/XML"]).stdout(Stdio::piped()).output();
    match output {
        Ok(out) if out.status.success() => classify_printed(Some(&decode_oem(&out.stdout)), &exe, oem_lossy),
        _ => TaskState::Missing,
    }
}

fn run_schtasks(args: &[&str]) -> Result<(), i32> {
    match schtasks().args(args).stdout(Stdio::null()).status() {
        Ok(status) if status.success() => Ok(()),
        Ok(status) => Err(status.code().unwrap_or(1).clamp(1, 200)),
        Err(_) => Err(1),
    }
}

/// Register the task (elevated only). The XML goes to %SystemRoot%\Temp under a fresh
/// name: a file an elevated process creates there belongs to the Administrators group,
/// so a non-elevated process cannot swap in its own command before schtasks reads it.
/// (The user's %TEMP% is only the fallback.) The command is always this DotVoice.exe.
fn create_task(sid: &str) -> Result<(), i32> {
    let exe = exe().ok_or(EXIT_FAILED)?;
    let bytes = utf16_file(&task_xml(&exe, sid));
    let nanos = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.subsec_nanos()).unwrap_or(0);
    let name = format!("dotvoice-task-{}-{nanos}.xml", std::process::id());
    for folder in [windows_folder(false).join("Temp"), std::env::temp_dir()] {
        let path = folder.join(&name);
        // create_new: never write into (or later delete) a file someone else put there.
        let Ok(mut file) = OpenOptions::new().write(true).create_new(true).open(&path) else { continue };
        let written = file.write_all(&bytes).and_then(|_| file.sync_all());
        drop(file);
        if written.is_err() {
            let _ = std::fs::remove_file(&path);
            continue;
        }
        let text = path.to_string_lossy().into_owned();
        let result = run_schtasks(&["/Create", "/TN", TASK_NAME, "/XML", text.as_str(), "/F"]).map_err(|c| EXIT_SCHTASKS + c);
        let _ = std::fs::remove_file(&path);
        return result;
    }
    Err(EXIT_FAILED)
}

fn delete_task() -> Result<(), i32> {
    run_schtasks(&["/Delete", "/TN", TASK_NAME, "/F"]).map_err(|c| EXIT_SCHTASKS + c)
}

fn run_task() -> Result<(), i32> {
    run_schtasks(&["/Run", "/TN", TASK_NAME]).map_err(|c| EXIT_SCHTASKS + c)
}

fn marker_path() -> PathBuf {
    std::env::temp_dir().join(SHOW_MARKER)
}

fn write_show_marker() {
    let _ = std::fs::write(marker_path(), now_secs().to_string());
}

/// Once, at startup of a `--hidden` instance: did a restart the user asked for leave the
/// "show the window" marker? (It is removed either way.)
pub fn take_show_marker() -> bool {
    let path = marker_path();
    let Ok(text) = std::fs::read_to_string(&path) else { return false };
    let _ = std::fs::remove_file(&path);
    text.trim().parse::<u64>().map(|written| marker_fresh(written, now_secs())).unwrap_or(false)
}

/// Wait for the old instance to be gone (its single-instance lock with it). A pid that
/// meanwhile belongs to another program means it is gone already.
fn wait_for_exit(pid: u32, wait: Duration) -> bool {
    unsafe {
        let handle = OpenProcess(PROCESS_SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
        if handle.is_null() {
            return true; // already gone
        }
        let mut buffer = [0u16; 1024];
        let mut len = buffer.len() as u32;
        let named = QueryFullProcessImageNameW(handle, PROCESS_NAME_WIN32, buffer.as_mut_ptr(), &mut len) != 0;
        let ours = !named
            || exe().map_or(true, |exe| same_path(&String::from_utf16_lossy(&buffer[..len as usize]), &exe));
        let done = !ours
            || WaitForSingleObject(handle, wait.as_millis().min(u32::MAX as u128 - 1) as u32) == WAIT_OBJECT_0;
        CloseHandle(handle);
        done
    }
}

fn start_plain(args: &[&str]) -> bool {
    let Some(exe) = exe() else { return false };
    Command::new(exe).args(args).creation_flags(CREATE_NO_WINDOW).spawn().is_ok()
}

/// A medium-integrity start from an elevated process: explorer.exe hands the launch to
/// the running (non-elevated) shell. It shows the main window (no arguments get through).
fn start_unelevated() -> bool {
    let Some(exe) = exe() else { return false };
    if !token().elevated {
        return start_plain(&[]);
    }
    Command::new(windows_folder(false).join("explorer.exe")).arg(&exe).spawn().is_ok()
}

fn run_relaunch(pid: u32, how: Relaunch) -> i32 {
    if !wait_for_exit(pid, OLD_INSTANCE_WAIT) {
        return EXIT_FAILED; // a hung instance: never start a second one next to it
    }
    match how {
        Relaunch::Task { show } => {
            if show {
                write_show_marker();
            }
            if run_task().is_ok() {
                return EXIT_OK;
            }
            // The task would not start: never leave the user without DotVoice.
            let _ = std::fs::remove_file(marker_path());
            if start_plain(&[NO_ELEVATE]) { EXIT_OK } else { EXIT_FAILED }
        }
        Relaunch::Normal => {
            if start_unelevated() { EXIT_OK } else { EXIT_FAILED }
        }
    }
}

fn run_task_helper(op: TaskOp, sid: &str) -> i32 {
    if !token().elevated {
        return EXIT_NOT_ELEVATED;
    }
    match current_user_sid() {
        Some(own) if own.eq_ignore_ascii_case(sid) => {}
        Some(_) => return EXIT_OTHER_ACCOUNT, // UAC used another administrator's credentials
        None => return EXIT_FAILED,
    }
    let result = match op {
        TaskOp::Create => create_task(sid),
        TaskOp::Delete => delete_task(),
    };
    result.err().unwrap_or(EXIT_OK)
}

/// main(): run a helper (and exit with its code) instead of the app, if asked to.
pub fn run_helper() -> Option<i32> {
    let args: Vec<String> = std::env::args().collect();
    Some(match parse_helper(&args)? {
        Helper::Task(op, sid) => run_task_helper(op, &sid),
        Helper::Relaunch(pid, how) => run_relaunch(pid, how),
    })
}

/// Start the relauncher for this process (it waits until we are gone).
pub fn spawn_relauncher(how: Relaunch) -> Result<(), String> {
    let exe = exe().ok_or("找不到点声程序路径")?;
    Command::new(exe)
        .args(relaunch_args(unsafe { GetCurrentProcessId() }, how))
        .creation_flags(CREATE_NO_WINDOW)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map(|_| ())
        .map_err(|e| format!("无法安排点声重启：{e}"))
}

#[derive(Debug, PartialEq, Eq)]
pub enum AdminError {
    Cancelled,
    Failed(String),
}

/// Run this DotVoice.exe elevated with `params` (one UAC prompt, owned by `owner` so it
/// comes to the front) and wait for its exit code. Blocking: call off the main thread.
fn run_as_admin(params: &str, owner: isize) -> Result<u32, AdminError> {
    let exe = exe().ok_or_else(|| AdminError::Failed("找不到点声程序路径".into()))?;
    let file = wide_os(&exe);
    let params = wide(params);
    let verb = wide("runas");
    unsafe {
        let com = CoInitializeEx(null(), (COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE) as u32);
        let mut info: SHELLEXECUTEINFOW = std::mem::zeroed();
        info.cbSize = std::mem::size_of::<SHELLEXECUTEINFOW>() as u32;
        info.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_FLAG_NO_UI | SEE_MASK_NOASYNC;
        info.hwnd = owner as HWND;
        info.lpVerb = verb.as_ptr();
        info.lpFile = file.as_ptr();
        info.lpParameters = params.as_ptr();
        info.nShow = SW_HIDE;
        let launched = ShellExecuteExW(&mut info);
        let error = GetLastError();
        if com >= 0 {
            CoUninitialize();
        }
        if launched == 0 {
            return Err(if error == ERROR_CANCELLED {
                AdminError::Cancelled
            } else {
                AdminError::Failed(format!("无法请求管理员权限（Windows 错误 {error}）"))
            });
        }
        if info.hProcess.is_null() {
            return Err(AdminError::Failed("管理员操作没有启动".into()));
        }
        let waited = WaitForSingleObject(info.hProcess, UAC_WAIT.as_millis() as u32);
        let mut code = 1u32;
        let got = GetExitCodeProcess(info.hProcess, &mut code);
        CloseHandle(info.hProcess);
        if waited != WAIT_OBJECT_0 || got == 0 {
            return Err(AdminError::Failed("管理员操作没有在规定时间内完成，设置未更改".into()));
        }
        Ok(code)
    }
}

/// Create or delete the task, elevating through UAC when this instance is not elevated.
/// Blocking (a UAC prompt may stay open): call off the main thread.
pub fn change_task(op: TaskOp, owner: isize) -> Result<(), String> {
    let token = token();
    let sid = current_user_sid().ok_or("无法读取当前 Windows 账户")?;
    if token.elevated {
        let result = match op {
            TaskOp::Create => create_task(&sid),
            TaskOp::Delete => delete_task(),
        };
        return result.map_err(|code| helper_error(op, code as u32));
    }
    if op == TaskOp::Delete {
        // A task this user registered may be deletable without a prompt; try that first.
        if delete_task().is_ok() || query_task() == TaskState::Missing {
            return Ok(());
        }
    }
    if !token.can_elevate {
        return Err("当前 Windows 账户不是管理员，无法以管理员身份运行点声".into());
    }
    match run_as_admin(&helper_params(op, &sid), owner) {
        Ok(0) => Ok(()),
        Ok(code) => Err(helper_error(op, code)),
        Err(AdminError::Cancelled) => Err("已取消：没有获得管理员确认，设置未更改".into()),
        Err(AdminError::Failed(message)) => Err(message),
    }
}

/// For the settings page: whether DotVoice runs elevated, could, and what the task says.
pub fn status(smoke: bool) -> Value {
    let token = token();
    let task = if smoke { TaskState::Missing } else { query_task() };
    let (task_word, task_path) = match &task {
        TaskState::Missing => ("missing", Value::Null),
        TaskState::Ours => ("ours", Value::Null),
        TaskState::Other(path) => ("other", Value::String(path.clone())),
    };
    json!({
        "elevated": token.elevated,
        "available": token.can_elevate && !smoke,
        "task": task_word,
        "task_path": task_path,
        "smoke": smoke,
    })
}

/// An elevated instance: let a second, non-elevated start's hand-over message (the
/// single-instance plugin's WM_COPYDATA, window "{identifier}-siw") through UIPI, so a
/// click on the desktop shortcut still brings this window up. Nothing else is allowed in.
pub fn accept_handover(identifier: &str) {
    if !token().elevated {
        return;
    }
    let class = wide(&format!("{identifier}-sic"));
    let title = wide(&format!("{identifier}-siw"));
    unsafe {
        let hwnd = FindWindowW(class.as_ptr(), title.as_ptr());
        if hwnd.is_null() {
            return;
        }
        let mut pid = 0u32;
        GetWindowThreadProcessId(hwnd, &mut pid);
        if pid == GetCurrentProcessId() {
            ChangeWindowMessageFilterEx(hwnd, WM_COPYDATA, MSGFLT_ALLOW, null_mut());
        }
    }
}

/// A non-elevated start while an elevated DotVoice runs: the plugin's CreateMutexW on the
/// elevated instance's "{identifier}-sim" fails with access denied instead of "already
/// exists", so it would take this start for the first one (a dead shortcut click, or a
/// second shell). Send the plugin's own hand-over (what [`accept_handover`] lets through)
/// and report whether another instance's window was there. Call before the plugin creates
/// this process's window; true means: exit now.
pub fn forward_to_running(identifier: &str) -> bool {
    let class = wide(&format!("{identifier}-sic"));
    let title = wide(&format!("{identifier}-siw"));
    unsafe {
        let hwnd = FindWindowW(class.as_ptr(), title.as_ptr());
        if hwnd.is_null() {
            return false;
        }
        let mut pid = 0u32;
        GetWindowThreadProcessId(hwnd, &mut pid);
        if pid == 0 || pid == GetCurrentProcessId() {
            return false;
        }
        // Lets the running instance bring its window to the front.
        AllowSetForegroundWindow(pid);
        let cwd = std::env::current_dir().unwrap_or_default();
        let args: Vec<String> = std::env::args().collect();
        let data = handover_payload(cwd.to_str().unwrap_or_default(), &args);
        let cds = COPYDATASTRUCT {
            dwData: SINGLE_INSTANCE_DATA,
            cbData: data.len() as u32,
            lpData: data.as_ptr() as *mut c_void,
        };
        // Even unanswered (hung, or the message refused): never start a second one next to it.
        let mut answer = 0usize;
        SendMessageTimeoutW(hwnd, WM_COPYDATA, 0, &cds as *const _ as isize, SMTO_ABORTIFHUNG, 3000, &mut answer);
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const SID: &str = "S-1-5-21-2817847997-3205930499-1521930722-1001";

    fn args(list: &[&str]) -> Vec<String> {
        list.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn the_task_runs_this_exe_hidden_as_administrator_at_this_users_logon() {
        let exe = Path::new(r"C:\Users\张三 & Co\AppData\Local\Programs\DotVoice\DotVoice.exe");
        let xml = task_xml(exe, SID);
        assert!(xml.starts_with(r#"<?xml version="1.0" encoding="UTF-16"?>"#));
        assert!(xml.contains("<RunLevel>HighestAvailable</RunLevel>"));
        assert!(xml.contains("<LogonType>InteractiveToken</LogonType>"));
        assert_eq!(xml.matches(&format!("<UserId>{SID}</UserId>")).count(), 2, "trigger and principal: this user only");
        assert!(xml.contains(r"<Command>C:\Users\张三 &amp; Co\AppData\Local\Programs\DotVoice\DotVoice.exe</Command>"));
        assert!(xml.contains("<Arguments>--hidden --elevated-task</Arguments>"));
        assert!(xml.contains(r"<WorkingDirectory>C:\Users\张三 &amp; Co\AppData\Local\Programs\DotVoice</WorkingDirectory>"));
        assert!(xml.contains(r"<URI>\DotVoice (管理员)</URI>"));
        for setting in [
            "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>",
            "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>",
            "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>",
            "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>",
            "<Priority>4</Priority>",
            "<StopOnIdleEnd>false</StopOnIdleEnd>",
        ] {
            assert!(xml.contains(setting), "missing {setting}");
        }
        assert!(!xml.contains(" & "), "every ampersand is escaped");
        // What schtasks /Query /XML prints back is recognised as ours.
        assert_eq!(task_command(&xml).as_deref(), Some(r"C:\Users\张三 & Co\AppData\Local\Programs\DotVoice\DotVoice.exe"));
        assert_eq!(classify(Some(&xml), exe), TaskState::Ours);
    }

    #[test]
    fn the_task_file_is_utf16_with_a_byte_order_mark() {
        let bytes = utf16_file("<a>点声</a>");
        assert_eq!(&bytes[..2], &[0xFF, 0xFE]);
        let units: Vec<u16> = bytes[2..].chunks_exact(2).map(|p| u16::from_le_bytes([p[0], p[1]])).collect();
        assert_eq!(String::from_utf16(&units).unwrap(), "<a>点声</a>");
        assert_eq!(decode_oem(&bytes), "<a>点声</a>");
    }

    #[test]
    fn a_queried_task_is_ours_only_for_this_exe() {
        let exe = Path::new(r"C:\Users\A\AppData\Local\Programs\DotVoice\DotVoice.exe");
        let printed = "<?xml version=\"1.0\" encoding=\"UTF-16\"?>\r\r\n<Task>\r\r\n  <Actions Context=\"Author\">\r\r\n    <Exec>\r\r\n      <Command>\"c:/users/a/appdata/local/programs/dotvoice/DOTVOICE.EXE\"</Command>\r\r\n      <Arguments>--hidden --elevated-task</Arguments>\r\r\n    </Exec>\r\r\n  </Actions>\r\r\n</Task>";
        assert_eq!(classify(Some(printed), exe), TaskState::Ours, "case, quotes and slashes do not matter");
        let moved = printed.replace("c:/users/a/appdata/local/programs/dotvoice", r"D:\Portable\DotVoice");
        assert_eq!(classify(Some(&moved), exe), TaskState::Other(r"D:\Portable\DotVoice/DOTVOICE.EXE".into()));
        assert_eq!(classify(None, exe), TaskState::Missing);
        assert_eq!(classify(Some("<Task><Command></Command></Task>"), exe), TaskState::Missing);
    }

    #[test]
    fn a_task_printed_through_a_lossy_code_page_is_still_ours() {
        // An English system (OEM 437) prints a Chinese user name as '?'.
        let lossy = |s: &str| s.chars().map(|c| if c.is_ascii() { c } else { '?' }).collect::<String>();
        let exe = Path::new(r"C:\Users\张三\AppData\Local\Programs\DotVoice\DotVoice.exe");
        let printed = lossy(&task_xml(exe, SID));
        let seen = r"C:\Users\??\AppData\Local\Programs\DotVoice\DotVoice.exe";
        assert_eq!(classify(Some(&printed), exe), TaskState::Other(seen.into()));
        assert_eq!(classify_printed(Some(&printed), exe, lossy), TaskState::Ours);
        // Another copy stays another copy, '?' or not.
        assert_eq!(classify_printed(Some(&printed), Path::new(r"D:\点声\DotVoice.exe"), lossy), TaskState::Other(seen.into()));
        let moved = lossy(&task_xml(Path::new(r"D:\Portable\DotVoice.exe"), SID));
        assert_eq!(classify_printed(Some(&moved), exe, lossy), TaskState::Other(r"D:\Portable\DotVoice.exe".into()));
        assert_eq!(classify_printed(None, exe, lossy), TaskState::Missing);
        // The real trip leaves an ASCII path as it is, in any OEM code page.
        let plain = r"C:\Program Files\DotVoice\DotVoice.exe";
        assert_eq!(oem_lossy(plain), plain);
    }

    #[test]
    fn only_a_non_elevated_administrator_start_with_our_task_hands_over() {
        let admin = Token { elevated: false, can_elevate: true };
        let ours = || TaskState::Ours;
        assert_eq!(startup(admin, &args(&["DotVoice.exe"]), ours), Startup::HandOver { show: true });
        assert_eq!(startup(admin, &args(&["DotVoice.exe", "--hidden"]), ours), Startup::HandOver { show: false });
        // The task's own start and the relauncher's fallback never hand over again (no loop).
        assert_eq!(startup(admin, &args(&["DotVoice.exe", "--hidden", FROM_TASK]), ours), Startup::Stay);
        assert_eq!(startup(admin, &args(&["DotVoice.exe", NO_ELEVATE]), ours), Startup::Stay);
        assert_eq!(startup(admin, &args(&["DotVoice.exe", "--smoke"]), ours), Startup::Stay);
        assert_eq!(startup(admin, &args(&["DotVoice.exe"]), || TaskState::Missing), Startup::Stay);
        assert_eq!(startup(admin, &args(&["DotVoice.exe"]), || TaskState::Other("D:\\x.exe".into())), Startup::Stay);
        // Already elevated, or a standard account: the task is never even queried.
        let never = || -> TaskState { panic!("queried") };
        assert_eq!(startup(Token { elevated: true, can_elevate: true }, &args(&["DotVoice.exe"]), never), Startup::Stay);
        assert_eq!(startup(Token { elevated: false, can_elevate: false }, &args(&["DotVoice.exe"]), never), Startup::Stay);
    }

    #[test]
    fn command_lines_round_trip() {
        assert_eq!(quote_arg("create"), "create");
        assert_eq!(quote_arg(""), "\"\"");
        assert_eq!(quote_arg(r"C:\Program Files\DotVoice"), r#""C:\Program Files\DotVoice""#);
        assert_eq!(quote_arg(r"C:\a b\"), r#""C:\a b\\""#, "a trailing backslash must not escape the quote");
        assert_eq!(quote_arg(r#"say "hi""#), r#""say \"hi\"""#);
        assert_eq!(helper_params(TaskOp::Create, SID), format!("--elevation-task create {SID}"));
        assert_eq!(helper_params(TaskOp::Delete, SID), format!("--elevation-task delete {SID}"));
        assert_eq!(relaunch_args(42, Relaunch::Task { show: true }), args(&["--relaunch", "42", "task-show"]));
        assert_eq!(relaunch_args(42, Relaunch::Normal), args(&["--relaunch", "42", "normal"]));
    }

    #[test]
    fn the_hand_over_matches_what_the_single_instance_plugin_parses() {
        // The plugin reads a C string, splits on '|': the first part is the cwd, the rest args.
        let payload = handover_payload(r"C:\Users\张三", &args(&[r"C:\Program Files\DotVoice\DotVoice.exe", "--hidden"]));
        assert_eq!(payload, "C:\\Users\\张三|C:\\Program Files\\DotVoice\\DotVoice.exe|--hidden\0");
        assert_eq!(payload.matches('\0').count(), 1);
        // No running instance (no plugin window under that name): start normally.
        assert!(!forward_to_running("app.dotvoice.test-no-such-instance"));
    }

    #[test]
    fn helper_runs_are_recognised_and_anything_else_is_a_normal_start() {
        let exe = "DotVoice.exe";
        assert_eq!(parse_helper(&args(&[exe, "--elevation-task", "create", SID])), Some(Helper::Task(TaskOp::Create, SID.into())));
        assert_eq!(parse_helper(&args(&[exe, "--elevation-task", "delete", SID])), Some(Helper::Task(TaskOp::Delete, SID.into())));
        assert_eq!(parse_helper(&args(&[exe, "--relaunch", "1234", "task"])), Some(Helper::Relaunch(1234, Relaunch::Task { show: false })));
        assert_eq!(parse_helper(&args(&[exe, "--relaunch", "1234", "task-show"])), Some(Helper::Relaunch(1234, Relaunch::Task { show: true })));
        assert_eq!(parse_helper(&args(&[exe, "--relaunch", "1234", "normal"])), Some(Helper::Relaunch(1234, Relaunch::Normal)));
        for bad in [
            vec![exe],
            vec![exe, "--hidden"],
            vec![exe, "--elevation-task", "create"],
            vec![exe, "--elevation-task", "format", SID],
            vec![exe, "--elevation-task", "create", "Administrator"],
            vec![exe, "--relaunch", "0", "task"],
            vec![exe, "--relaunch", "abc", "task"],
            vec![exe, "--relaunch", "12", "now"],
        ] {
            assert_eq!(parse_helper(&args(&bad)), None, "{bad:?}");
        }
    }

    #[test]
    fn the_show_marker_is_only_fresh_briefly() {
        assert!(marker_fresh(1000, 1000));
        assert!(marker_fresh(1000, 1000 + MARKER_FRESH_SECS));
        assert!(!marker_fresh(1000, 1001 + MARKER_FRESH_SECS));
        assert!(!marker_fresh(5000, 1000), "a marker from the future (clock change) is not trusted");
    }

    #[test]
    fn helper_failures_read_as_sentences() {
        assert!(helper_error(TaskOp::Create, EXIT_OTHER_ACCOUNT as u32).contains("另一个 Windows 账户"));
        assert!(helper_error(TaskOp::Create, (EXIT_SCHTASKS + 1) as u32).contains("无法登记计划任务「DotVoice (管理员)」（schtasks 返回 1）"));
        assert!(helper_error(TaskOp::Delete, (EXIT_SCHTASKS + 1) as u32).contains("无法删除"));
        assert!(helper_error(TaskOp::Create, 7).contains("代码 7"));
    }
}
