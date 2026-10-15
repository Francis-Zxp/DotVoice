//! Owns the Python core process: spawn, JSON-lines requests, events, restarts.
//! The core is reached only through its stdio pipes; no port is opened.

use serde_json::{json, Value};
use std::collections::HashMap;
use std::fs::File;
use std::io::{BufRead, BufReader, Write};
use std::os::windows::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tauri::{AppHandle, Emitter};
use tokio::sync::oneshot;

const CREATE_NO_WINDOW: u32 = 0x0800_0000;
const MAX_RESTARTS: usize = 3;

type Reply = oneshot::Sender<Result<Value, String>>;
type EventHook = Box<dyn Fn(&AppHandle, &str, &Value) + Send + Sync>;

/// One line from the core: an event, a reply to a request, or noise.
#[derive(Debug, PartialEq)]
pub enum Line {
    Event(String, Value),
    Reply(u64, Result<Value, String>),
    Ignored,
}

pub fn parse_line(line: &str) -> Line {
    let Ok(message) = serde_json::from_str::<Value>(line) else { return Line::Ignored };
    if let Some(event) = message.get("event").and_then(Value::as_str) {
        return Line::Event(event.to_string(), message.get("data").cloned().unwrap_or(Value::Null));
    }
    match message.get("id").and_then(Value::as_u64) {
        Some(id) => Line::Reply(
            id,
            match message.get("error") {
                Some(error) => Err(error.as_str().unwrap_or("核心返回错误").to_string()),
                None => Ok(message.get("result").cloned().unwrap_or(Value::Null)),
            },
        ),
        None => Line::Ignored,
    }
}

struct Running {
    child: Child,
    stdin: ChildStdin,
}

/// Take what the slot holds only while it is still `pid`'s.
fn take_if_current<T>(slot: &mut Option<T>, pid: u32, id: impl Fn(&T) -> u32) -> Option<T> {
    if slot.as_ref().map(id) == Some(pid) {
        slot.take()
    } else {
        None
    }
}

pub struct CoreClient {
    app: AppHandle,
    smoke: bool,
    running: Mutex<Option<Running>>,
    pending: Mutex<HashMap<u64, Reply>>,
    next_id: AtomicU64,
    pub ready: Mutex<Option<Value>>,
    pub quitting: AtomicBool,
    restarts: Mutex<Vec<Instant>>,
    on_event: EventHook,
}

/// Data folder: installed copies keep user data in %LOCALAPPDATA%\DotVoice,
/// portable copies next to the executable, development builds in the repo.
pub fn data_dir() -> PathBuf {
    if std::env::args().any(|a| a == "--smoke") {
        let root = if cfg!(debug_assertions) { project_root() } else { exe_dir() };
        return root.join("runtime").join("smoke");
    }
    if cfg!(debug_assertions) {
        return project_root();
    }
    let exe_dir = exe_dir();
    if exe_dir.join("installed.json").exists() {
        if let Ok(local) = std::env::var("LOCALAPPDATA") {
            return PathBuf::from(local).join("DotVoice");
        }
    }
    exe_dir
}

fn exe_dir() -> PathBuf {
    std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(Path::to_path_buf))
        .unwrap_or_else(|| PathBuf::from("."))
}

fn project_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("..").join("..")
}

fn core_command(smoke: bool) -> Command {
    let data = data_dir();
    let mut command = if let Ok(custom) = std::env::var("DOTVOICE_CORE") {
        Command::new(custom)
    } else if cfg!(debug_assertions) {
        let root = project_root();
        let mut c = Command::new(root.join(".venv").join("Scripts").join("python.exe"));
        c.arg(root.join("core_main.py"));
        c.current_dir(&root);
        c
    } else {
        let core = exe_dir().join("core");
        let mut c = Command::new(core.join("dotvoice-core.exe"));
        c.current_dir(core);
        c
    };
    command.arg("--data").arg(&data);
    if smoke {
        command.arg("--smoke");
    }
    command
}

impl CoreClient {
    pub fn new(
        app: AppHandle,
        smoke: bool,
        on_event: impl Fn(&AppHandle, &str, &Value) + Send + Sync + 'static,
    ) -> Arc<Self> {
        Arc::new(Self {
            app,
            smoke,
            running: Mutex::new(None),
            pending: Mutex::new(HashMap::new()),
            next_id: AtomicU64::new(1),
            ready: Mutex::new(None),
            quitting: AtomicBool::new(false),
            restarts: Mutex::new(Vec::new()),
            on_event: Box::new(on_event),
        })
    }

    pub fn spawn(self: &Arc<Self>) -> Result<(), String> {
        // One core at a time: the '重新启动核心' button and the auto-restart can meet here.
        let mut running = self.running.lock().unwrap();
        if running.as_mut().is_some_and(|r| matches!(r.child.try_wait(), Ok(None))) {
            return Ok(());
        }
        let mut command = core_command(self.smoke);
        let runtime = data_dir().join("runtime");
        let _ = std::fs::create_dir_all(&runtime);
        let stderr = File::create(runtime.join("core.log"))
            .map(Stdio::from)
            .unwrap_or_else(|_| Stdio::null());
        command
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(stderr)
            .creation_flags(CREATE_NO_WINDOW);
        let mut child = command
            .spawn()
            .map_err(|e| format!("无法启动点声核心：{e}"))?;
        let stdin = child.stdin.take().ok_or("核心输入管道不可用")?;
        let stdout = child.stdout.take().ok_or("核心输出管道不可用")?;
        let pid = child.id();
        *self.ready.lock().unwrap() = None;
        *running = Some(Running { child, stdin });
        drop(running);
        let me = Arc::clone(self);
        std::thread::Builder::new()
            .name("core-reader".into())
            .spawn(move || me.read_loop(stdout, pid))
            .map_err(|e| e.to_string())?;
        Ok(())
    }

    fn read_loop(self: Arc<Self>, stdout: std::process::ChildStdout, pid: u32) {
        let reader = BufReader::new(stdout);
        for line in reader.lines() {
            let Ok(line) = line else { break };
            match parse_line(&line) {
                Line::Event(event, data) => {
                    if event == "ready" {
                        *self.ready.lock().unwrap() = Some(data.clone());
                    }
                    (self.on_event)(&self.app, &event, &data);
                    let _ = self.app.emit("core://event", json!({ "event": event, "data": data }));
                }
                Line::Reply(id, result) => {
                    if let Some(reply) = self.pending.lock().unwrap().remove(&id) {
                        let _ = reply.send(result);
                    }
                }
                Line::Ignored => {}
            }
        }
        self.exited(pid);
    }

    fn exited(self: &Arc<Self>, pid: u32) {
        // Only this reader's own core: one that was replaced (or taken by shutdown) must not
        // take, and wait on, the core now running, nor fail its requests or restart it.
        let mine = take_if_current(&mut *self.running.lock().unwrap(), pid, |r| r.child.id());
        let Some(Running { mut child, stdin }) = mine else { return };
        // Outside the lock, and with its input closed so a core still alive sees EOF and exits.
        drop(stdin);
        let code = child.wait().ok().and_then(|s| s.code());
        for (_, reply) in self.pending.lock().unwrap().drain() {
            let _ = reply.send(Err("点声核心已退出".into()));
        }
        if self.quitting.load(Ordering::SeqCst) {
            return;
        }
        let restart = {
            let mut times = self.restarts.lock().unwrap();
            times.retain(|t| t.elapsed() < Duration::from_secs(60));
            times.push(Instant::now());
            times.len() <= MAX_RESTARTS
        };
        let data = json!({ "code": code, "restarting": restart });
        (self.on_event)(&self.app, "core_exit", &data);
        let _ = self.app.emit("core://event", json!({ "event": "core_exit", "data": data }));
        if restart {
            let me = Arc::clone(self);
            std::thread::spawn(move || {
                std::thread::sleep(Duration::from_secs(1));
                // Not if the '重新启动核心' button already started one meanwhile.
                if !me.quitting.load(Ordering::SeqCst) && !me.is_running() {
                    if let Err(error) = me.spawn() {
                        let _ = me.app.emit("core://event", json!({ "event": "fatal", "data": error }));
                    }
                }
            });
        }
    }

    fn write(&self, message: &Value) -> Result<(), String> {
        let mut line = serde_json::to_vec(message).map_err(|e| e.to_string())?;
        line.push(b'\n');
        let mut running = self.running.lock().unwrap();
        let running = running.as_mut().ok_or("点声核心未运行")?;
        running
            .stdin
            .write_all(&line)
            .and_then(|_| running.stdin.flush())
            .map_err(|_| "无法向点声核心发送指令".to_string())
    }

    pub async fn call(&self, method: &str, params: Value, timeout: Duration) -> Result<Value, String> {
        let id = self.next_id.fetch_add(1, Ordering::SeqCst);
        let (tx, rx) = oneshot::channel();
        self.pending.lock().unwrap().insert(id, tx);
        if let Err(error) = self.write(&json!({ "id": id, "method": method, "params": params })) {
            self.pending.lock().unwrap().remove(&id);
            return Err(error);
        }
        match tokio::time::timeout(timeout, rx).await {
            Ok(Ok(result)) => result,
            Ok(Err(_)) => Err("点声核心已退出".into()),
            Err(_) => {
                self.pending.lock().unwrap().remove(&id);
                Err("点声核心响应超时".into())
            }
        }
    }

    /// Fire-and-forget request (tray, hotkeys); the reply is ignored.
    pub fn notify(&self, method: &str, params: Value) {
        let _ = self.write(&json!({ "id": null, "method": method, "params": params }));
    }

    /// Ask the core to release microphone/HID, then make sure it is gone.
    pub fn shutdown(&self, wait: Duration) {
        self.quitting.store(true, Ordering::SeqCst);
        let _ = self.write(&json!({ "id": 0, "method": "shutdown" }));
        let deadline = Instant::now() + wait;
        loop {
            let done = match self.running.lock().unwrap().as_mut() {
                None => true,
                Some(r) => matches!(r.child.try_wait(), Ok(Some(_))),
            };
            if done || Instant::now() >= deadline {
                break;
            }
            std::thread::sleep(Duration::from_millis(50));
        }
        if let Some(mut running) = self.running.lock().unwrap().take() {
            if !matches!(running.child.try_wait(), Ok(Some(_))) {
                let _ = running.child.kill();
            }
            let _ = running.child.wait();
        }
    }

    pub fn is_running(&self) -> bool {
        self.running
            .lock()
            .unwrap()
            .as_mut()
            .map(|r| matches!(r.child.try_wait(), Ok(None)))
            .unwrap_or(false)
    }

    pub fn restart(self: &Arc<Self>) -> Result<(), String> {
        self.restarts.lock().unwrap().clear();
        if self.is_running() {
            return Ok(());
        }
        self.spawn()
    }
}

#[cfg(test)]
mod tests {
    use super::{parse_line, take_if_current, Line};
    use serde_json::json;

    #[test]
    fn a_replaced_cores_reader_leaves_the_running_core_alone() {
        let id = |pid: &u32| *pid;
        // Core 7 was replaced by core 9 (the restart button and the auto-restart met).
        let mut slot = Some(9u32);
        assert_eq!(take_if_current(&mut slot, 7, id), None);
        assert_eq!(slot, Some(9), "core 9 stays, nobody waits on it");
        assert_eq!(take_if_current(&mut slot, 9, id), Some(9));
        assert_eq!(slot, None);
        // Already taken by shutdown.
        assert_eq!(take_if_current(&mut slot, 9, id), None);
    }

    #[test]
    fn events_replies_errors_and_noise() {
        assert_eq!(
            parse_line(r#"{"event":"state","data":{"phase":"idle"}}"#),
            Line::Event("state".into(), json!({"phase": "idle"}))
        );
        assert_eq!(parse_line(r#"{"id":7,"result":{"ok":true}}"#), Line::Reply(7, Ok(json!({"ok": true}))));
        assert_eq!(parse_line(r#"{"id":8,"error":"失败"}"#), Line::Reply(8, Err("失败".into())));
        // Fire-and-forget replies carry a null id and are dropped.
        assert_eq!(parse_line(r#"{"id":null,"result":1}"#), Line::Ignored);
        assert_eq!(parse_line("not json"), Line::Ignored);
        assert_eq!(parse_line(r#"{"event":"ready"}"#), Line::Event("ready".into(), json!(null)));
    }
}
