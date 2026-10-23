// Release builds use the GUI subsystem: no console window.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod appearance;
mod backdrop;
mod core_client;
mod elevation;
mod magnifier;
mod overlay;
mod shortcut;
mod smoke;
mod stage;
mod tray;
mod winstyle;

use core_client::CoreClient;
use magnifier::Magnifier;
use stage::StageState;
use overlay::{OverlayState, Rect};
use serde_json::{json, Value};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::Duration;
use tauri::{AppHandle, Emitter, Manager, RunEvent, Runtime, WindowEvent};
use tauri_plugin_global_shortcut::{GlobalShortcutExt, ShortcutState};

/// `--hidden` (the logon task) starts in the tray, unless a restart the user asked for
/// (兼容管理员权限窗口) left the marker that says "show the window this once".
static START_HIDDEN: OnceLock<bool> = OnceLock::new();

fn start_hidden() -> bool {
    *START_HIDDEN.get_or_init(|| {
        std::env::args().any(|a| a == elevation::HIDDEN) && !elevation::take_show_marker()
    })
}

/// A normal start: the page load has shown the main window once (after that, a hidden
/// window means the user put it in the tray).
static PAGE_SHOWN: AtomicBool = AtomicBool::new(false);

/// Show the main window on its first finished page load only: Started comes before any CSS
/// or script (an empty frame with just its shadow), and a later load keeps the tray choice.
fn first_page_load(label: &str, finished: bool, hidden: bool, shown: &AtomicBool) -> bool {
    label == "main" && finished && !hidden && !shown.swap(true, Ordering::SeqCst)
}

pub fn show_main<R: Runtime>(app: &AppHandle<R>) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
    }
    // A second start can arrive while setup still builds the windows, before the core is
    // managed (it starts visible anyway).
    if let Some(core) = app.try_state::<Arc<CoreClient>>() {
        core.notify("set_visible", json!({ "visible": true }));
    }
}

/// A relauncher already waits to start DotVoice again once this instance is gone.
static RELAUNCH_QUEUED: AtomicBool = AtomicBool::new(false);

#[derive(Debug, PartialEq)]
enum SecondStart {
    Show,
    Relaunch,
    Ignore,
}

/// A second start (the single-instance hand-over). `quitting` is None until setup has
/// managed the core. While quitting, the start must not be swallowed: once, a relauncher
/// starts DotVoice again after this instance is gone.
fn second_start(quitting: Option<bool>, queued: &AtomicBool) -> SecondStart {
    if quitting != Some(true) {
        return SecondStart::Show;
    }
    if queued.swap(true, Ordering::SeqCst) { SecondStart::Ignore } else { SecondStart::Relaunch }
}

fn on_second_start<R: Runtime>(app: &AppHandle<R>) {
    let quitting = app.try_state::<Arc<CoreClient>>().map(|core| core.quitting.load(Ordering::SeqCst));
    match second_start(quitting, &RELAUNCH_QUEUED) {
        SecondStart::Show => show_main(app),
        SecondStart::Relaunch => {
            if elevation::spawn_relauncher(elevation::Relaunch::Normal).is_err() {
                RELAUNCH_QUEUED.store(false, Ordering::SeqCst);
            }
        }
        SecondStart::Ignore => {}
    }
}

pub fn quit(app: &AppHandle) {
    let core = Arc::clone(&app.state::<Arc<CoreClient>>());
    // Before anything else: a start from now on is relaunched, not shown by this instance.
    core.quitting.store(true, Ordering::SeqCst);
    shortcut::quitting(app);
    let app = app.clone();
    std::thread::spawn(move || {
        // Includes an in-flight device handoff, audio release and Tidepool restore.
        core.shutdown(Duration::from_secs(45));
        app.exit(0);
    });
}

/// The hotkey map last applied, and its (name, accelerator) entries that did not register.
type AppliedHotkeys = Option<(Option<Value>, Vec<(String, String)>)>;

/// 'prefs' arrives on every settings change and zoom step; re-registering an unchanged map
/// would repeat its "cannot register" notice each time.
static LAST_HOTKEYS: Mutex<AppliedHotkeys> = Mutex::new(None);

/// Apply `prefs.hotkeys` through `register(name, accelerator)`, after `clear` when the map
/// changed. An unchanged map only retries its failed entries, silently (the program that
/// held a key may have closed). Returns the new failures as (accelerator, error).
fn sync_hotkeys(
    last: &Mutex<AppliedHotkeys>,
    prefs: &Value,
    clear: impl FnOnce(),
    mut register: impl FnMut(&str, &str) -> Result<(), String>,
) -> Vec<(String, String)> {
    let wanted = prefs.get("hotkeys").cloned();
    let mut last = last.lock().unwrap();
    if let Some((_, failed)) = last.as_mut().filter(|applied| applied.0 == wanted) {
        failed.retain(|(name, accelerator)| register(name.as_str(), accelerator.as_str()).is_err());
        return Vec::new();
    }
    clear();
    let (mut failed, mut errors) = (Vec::new(), Vec::new());
    for (name, accelerator) in wanted.as_ref().and_then(Value::as_object).into_iter().flatten() {
        let Some(accelerator) = accelerator.as_str().filter(|a| !a.is_empty()) else { continue };
        if let Err(error) = register(name, accelerator) {
            failed.push((name.clone(), accelerator.to_string()));
            errors.push((accelerator.to_string(), error));
        }
    }
    *last = Some((wanted, failed));
    errors
}

/// Why an accelerator did not register, in the page's language (global-hotkey's errors are
/// English Debug text). A combination that parses fails because something already holds it.
fn hotkey_problem(accelerator: &str) -> &'static str {
    if accelerator.parse::<tauri_plugin_global_shortcut::Shortcut>().is_err() {
        "按键组合无法识别"
    } else {
        "已被其他程序或另一项快捷键占用，请换一个组合"
    }
}

/// Register the user's global shortcuts; an invalid one is reported, not fatal.
fn apply_hotkeys(app: &AppHandle, prefs: &Value) {
    let shortcuts = app.global_shortcut();
    let clear = || {
        let _ = shortcuts.unregister_all();
    };
    let errors = sync_hotkeys(&LAST_HOTKEYS, prefs, clear, |name, accelerator| {
        let name = name.to_string();
        shortcuts
            .on_shortcut(accelerator, move |app, _shortcut, event| {
                if event.state() == ShortcutState::Pressed {
                    app.state::<Arc<CoreClient>>().notify("hotkey", json!({ "name": name }));
                }
            })
            .map_err(|error| error.to_string())
    });
    for (accelerator, error) in errors {
        let _ = app.emit(
            "core://event",
            json!({ "event": "notice", "data": format!("快捷键 {accelerator} 无法注册：{}", hotkey_problem(&accelerator)) }),
        );
        // The raw reason, for diagnosis only (runtime/ui-errors.log).
        if let Some(core) = app.try_state::<Arc<CoreClient>>() {
            core.notify("log_ui_error", json!({ "where": "hotkey", "detail": format!("{accelerator}: {error}") }));
        }
    }
}

/// Core events that the shell itself acts on (tray, hotkeys, overlay position).
fn on_core_event(app: &AppHandle, event: &str, data: &Value) {
    match event {
        "ready" => {
            reconcile_elevation(app, data["prefs"]["general"]["run_elevated"].as_bool());
            let phase = data["state"]["phase"].as_str().unwrap_or("disconnected");
            shortcut::apply_prefs(app, &data["prefs"]);
            if let Some(zoom) = data["prefs"]["present"]["zoom"].as_f64() {
                magnifier::set_zoom(zoom);
            }
            tray::update_phase(app, phase, data["armed"].as_bool().unwrap_or(false));
            tray::update_mode(app, data["destination"].as_str().unwrap_or("input"));
            tray::set_core_available(app, true);
            apply_hotkeys(app, &data["prefs"]);
            appearance::apply(app, &data["prefs"]);
            overlay::apply_prefs(app, &data["prefs"]);
            // A core restarted while the window sits in the tray starts visible: tell it. Not
            // before the page load first shows the window on a normal start (that would hide it).
            let hidden = app.get_webview_window("main").and_then(|w| w.is_visible().ok()) == Some(false);
            if hidden && (start_hidden() || PAGE_SHOWN.load(Ordering::SeqCst)) {
                if let Some(core) = app.try_state::<Arc<CoreClient>>() {
                    core.notify("set_visible", json!({ "visible": false }));
                }
            }
        }
        "state" => {
            let phase = data["phase"].as_str().unwrap_or("disconnected");
            tray::update_phase(app, phase, data["armed"].as_bool().unwrap_or(false));
        }
        "mode" => {
            if let Some(value) = data["value"].as_str() {
                tray::update_mode(app, value);
                if value != "present" {
                    app.state::<Magnifier>().stop();
                    stage::hide(app);
                }
            }
        }
        "pointer" => {
            let active = data["active"].as_bool().unwrap_or(false);
            let tool = data["tool"].as_str().unwrap_or("");
            let magnifier = tool == "magnifier";
            stage::pointer(
                app,
                tool,
                active,
                data["latched"].as_bool().unwrap_or(false),
                data["hide_cursor"].as_bool().unwrap_or(false),
            );
            if let Some(zoom) = data["zoom"].as_f64() {
                magnifier::set_zoom(zoom);
            }
            if magnifier {
                if active { app.state::<Magnifier>().start() } else { app.state::<Magnifier>().stop() }
            } else if active {
                stage::show(app);
            }
            // The stage page asks to be hidden once nothing is drawn (stage_idle).
        }
        "magnifier" => {
            if let Some(zoom) = data["zoom"].as_f64() {
                magnifier::set_zoom(zoom);
            }
        }
        "annotate" => {
            let down = data["down"].as_bool() == Some(true);
            stage::annotate(app, down);
            if down {
                stage::show(app);
            }
        }
        "prefs" => {
            if let Some(zoom) = data["present"]["zoom"].as_f64() {
                magnifier::set_zoom(zoom);
            }
            apply_hotkeys(app, data);
            appearance::apply(app, data);
            shortcut::apply_prefs(app, data);
            overlay::apply_prefs(app, data);
        }
        // A core that died mid-presentation can no longer release its tools.
        "core_exit" => {
            app.state::<Magnifier>().stop();
            stage::hide(app);
            // Not "ready" any more; the next core's ready snapshot sets the real state.
            tray::update_phase(app, "error", false);
            tray::set_core_available(app, false);
        }
        _ => {}
    }
}

#[tauri::command]
async fn core_call(
    core: tauri::State<'_, Arc<CoreClient>>,
    method: String,
    params: Option<Value>,
) -> Result<Value, String> {
    // Tidepool hand-off may legitimately wait up to ~20 s for its receiver.
    core.call(&method, params.unwrap_or(Value::Null), Duration::from_secs(40)).await
}

#[tauri::command]
fn core_ready(core: tauri::State<'_, Arc<CoreClient>>) -> Option<Value> {
    core.ready.lock().unwrap().clone()
}

#[tauri::command]
fn core_restart(core: tauri::State<'_, Arc<CoreClient>>) -> Result<(), String> {
    core.restart()
}

#[tauri::command]
fn overlay_show(window: tauri::WebviewWindow) {
    if overlay::is_overlay(window.label()) { overlay::show(&window); }
}

#[tauri::command]
fn overlay_hide(window: tauri::WebviewWindow) {
    if overlay::is_overlay(window.label()) { overlay::hide(&window); }
}

#[tauri::command]
fn overlay_interactive(window: tauri::WebviewWindow, rect: Option<Rect>) {
    overlay::set_interactive(&window, rect);
}

/// Downscaled screen pixels behind the (still transparent) capsule window, for
/// the liquid-glass material. Raw bytes: [w u32][h u32][RGBA…].
#[tauri::command]
fn overlay_backdrop(window: tauri::WebviewWindow, width: Option<u32>) -> Result<tauri::ipc::Response, String> {
    if !overlay::is_overlay(window.label()) { return Err("no overlay".into()) }
    backdrop::capture(&window, width.unwrap_or(200))
        .map(tauri::ipc::Response::new)
        .ok_or_else(|| "capture failed".into())
}

#[tauri::command]
fn overlay_layout(window: tauri::WebviewWindow) -> overlay::Layout { overlay::layout(&window) }

#[tauri::command]
fn overlay_snapshot(app: AppHandle) -> Option<overlay::PublishedModel> { overlay::snapshot(&app) }

#[tauri::command]
fn overlay_publish(window: tauri::WebviewWindow, model: Value) -> Result<overlay::PublishedModel, String> {
    overlay::publish(&window, model)
}

#[tauri::command]
fn overlay_action(window: tauri::WebviewWindow, action: Value) -> Result<(), String> { overlay::action(&window, action) }

#[tauri::command]
fn overlay_monitors(app: AppHandle) -> Result<Vec<overlay::MonitorInfo>, String> {
    let window = app.get_webview_window("overlay").ok_or("胶囊窗口尚未就绪")?;
    overlay::monitors(&window)
}

#[tauri::command]
async fn overlay_reset_position(app: AppHandle) -> Result<(), String> {
    let core = Arc::clone(&app.state::<Arc<CoreClient>>());
    let result = core.call("prefs_set", json!({"patch": {"overlay": {"position": null, "positions": {}}}}), Duration::from_secs(10)).await?;
    if let Some(error) = result["error"].as_str() { return Err(error.to_string()) }
    overlay::apply_prefs(&app, &result);
    overlay::reset_placement(&app);
    Ok(())
}

#[tauri::command]
fn stage_idle(app: AppHandle) {
    stage::hide_if_idle(&app);
}

#[tauri::command]
fn show_main_window(app: AppHandle) {
    show_main(&app);
}

#[tauri::command]
fn hide_main_window(app: AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.hide();
    }
    if let Some(core) = app.try_state::<Arc<CoreClient>>() {
        core.notify("set_visible", json!({ "visible": false }));
    }
}

#[tauri::command]
fn quit_app(app: AppHandle) {
    quit(&app);
}

fn is_smoke() -> bool {
    std::env::args().any(|a| a == "--smoke")
}

/// prefs general.run_elevated mirrors whether the logon task exists for this DotVoice.exe
/// (it is gone after an uninstall, or was deleted by hand): the task is the truth.
fn reconcile_elevation(app: &AppHandle, saved: Option<bool>) {
    if is_smoke() {
        return;
    }
    let app = app.clone();
    std::thread::spawn(move || {
        let task = elevation::query_task() == elevation::TaskState::Ours;
        if saved != Some(task) {
            app.state::<Arc<CoreClient>>()
                .notify("prefs_set", json!({ "patch": { "general": { "run_elevated": task } } }));
        }
    });
}

/// Settings → 通用 → 兼容管理员权限窗口: elevated or not, possible or not, the task.
#[tauri::command]
async fn elevation_status() -> Result<Value, String> {
    let smoke = is_smoke();
    tauri::async_runtime::spawn_blocking(move || elevation::status(smoke)).await.map_err(|e| e.to_string())
}

async fn refuse_while_busy(core: &CoreClient) -> Result<(), String> {
    if let Ok(snapshot) = core.call("snapshot", Value::Null, Duration::from_secs(10)).await {
        if matches!(snapshot["state"]["phase"].as_str(), Some("starting" | "recording" | "processing")) {
            return Err("正在录音或识别，请等本段结束后再切换".into());
        }
    }
    Ok(())
}

/// How long a restart the user asked for waits for a session that began meanwhile.
const IDLE_WAIT: Duration = Duration::from_secs(30);

/// Ask `idle` every `every` until it says yes; false once `wait` has passed.
async fn wait_for<F, Fut>(mut idle: F, wait: Duration, every: Duration) -> bool
where
    F: FnMut() -> Fut,
    Fut: std::future::Future<Output = bool>,
{
    let deadline = std::time::Instant::now() + wait;
    loop {
        if idle().await {
            return true;
        }
        if std::time::Instant::now() >= deadline {
            return false;
        }
        tokio::time::sleep(every).await;
    }
}

/// The relauncher waits until this instance is gone; quit gracefully after a moment, so
/// the page can say why it closes.
fn restart_into(app: &AppHandle, how: elevation::Relaunch) -> Result<(), String> {
    elevation::spawn_relauncher(how)?;
    RELAUNCH_QUEUED.store(true, Ordering::SeqCst);
    let app = app.clone();
    std::thread::spawn(move || {
        std::thread::sleep(Duration::from_millis(600));
        quit(&app);
    });
    Ok(())
}

/// 兼容管理员权限窗口 is on but this copy is not elevated (a hand-over that failed):
/// restart through the logon task - no prompt.
#[tauri::command]
async fn elevation_restart(app: AppHandle) -> Result<Value, String> {
    if is_smoke() {
        return Err("界面自检模式不更改管理员设置。".into());
    }
    if elevation::token().elevated {
        return Ok(json!({ "restarting": false }));
    }
    let task = tauri::async_runtime::spawn_blocking(elevation::query_task).await.map_err(|e| e.to_string())?;
    if task != elevation::TaskState::Ours {
        return Err("没有找到点声的管理员启动任务，请重新开启兼容管理员权限窗口".into());
    }
    let core = Arc::clone(&app.state::<Arc<CoreClient>>());
    refuse_while_busy(&core).await?;
    restart_into(&app, elevation::Relaunch::Task { show: true })?;
    Ok(json!({ "restarting": true }))
}

/// Turn 兼容管理员权限窗口 on or off (only ever from the user's switch): the logon task
/// (one UAC prompt when not elevated), the mirrored setting, then a graceful restart
/// into the other integrity level when this instance is on the wrong side.
#[tauri::command]
async fn elevation_set(app: AppHandle, enabled: bool) -> Result<Value, String> {
    if is_smoke() {
        return Err("界面自检模式不更改管理员设置。".into());
    }
    let core = Arc::clone(&app.state::<Arc<CoreClient>>());
    refuse_while_busy(&core).await?;
    let token = elevation::token();
    if enabled && !token.can_elevate {
        return Err("当前 Windows 账户不是管理员，无法以管理员身份运行点声".into());
    }
    let owner = app
        .get_webview_window("main")
        .and_then(|w| w.hwnd().ok())
        .map(|h| h.0 as isize)
        .unwrap_or(0);
    let op = if enabled { elevation::TaskOp::Create } else { elevation::TaskOp::Delete };
    tauri::async_runtime::spawn_blocking(move || elevation::change_task(op, owner))
        .await
        .map_err(|e| e.to_string())??;
    // The mirror (the core emits 'prefs' itself); the task is what counts, so a failed save
    // is corrected by the next start's reconcile_elevation.
    let _ = core
        .call("prefs_set", json!({ "patch": { "general": { "run_elevated": enabled } } }), Duration::from_secs(10))
        .await;
    // On the wrong side now: restart there (the relauncher waits until this one is gone).
    let restart = if enabled && !token.elevated {
        Some(elevation::Relaunch::Task { show: true })
    } else if !enabled && token.elevated {
        Some(elevation::Relaunch::Normal)
    } else {
        None
    };
    let Some(how) = restart else { return Ok(json!({ "restarting": false })) };
    // The UAC prompt can stay open for minutes, and the pen still starts sessions meanwhile:
    // let one that began finish instead of cutting it off.
    let client: &CoreClient = &core;
    let idle = || async move { refuse_while_busy(client).await.is_ok() };
    if !wait_for(idle, IDLE_WAIT, Duration::from_millis(500)).await {
        return Ok(json!({ "restarting": false, "warning": "设置已更改；本段录音结束后请从托盘退出并重新打开点声" }));
    }
    match restart_into(&app, how) {
        Ok(()) => Ok(json!({ "restarting": true })),
        Err(error) => Ok(json!({ "restarting": false, "warning": format!("{error}；请从托盘退出点声后重新打开") })),
    }
}

fn main() {
    // `--elevation-task …` (elevated, from a UAC prompt) and `--relaunch …` are helper runs
    // for 兼容管理员权限窗口: they do their one job and exit, never becoming an instance.
    if let Some(code) = elevation::run_helper() {
        std::process::exit(code);
    }
    // `--smoke-cleanup <pid>`: removes a finished smoke run's temp folder, then exits.
    if let Some(code) = smoke::run_cleanup_helper() {
        std::process::exit(code);
    }
    let smoke = std::env::args().any(|a| a == "--smoke");
    let mut context = tauri::generate_context!();
    if smoke {
        // A native check must not hand over to the user's live voice instance.
        context.config_mut().identifier.push_str(".smoke");
        // Nor leave a WebView2 profile behind: setup makes the windows with theirs in the
        // run's temp folder (smoke::create_windows).
        for window in &mut context.config_mut().app.windows {
            window.create = false;
        }
    } else if !elevation::token().elevated && elevation::forward_to_running(&context.config().identifier) {
        // DotVoice already runs (elevated, which the single-instance plugin cannot see from a
        // non-elevated start): it brings its window up, and this start ends here.
        std::process::exit(0);
    }
    start_hidden();
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| on_second_start(app)))
        .plugin(tauri_plugin_global_shortcut::Builder::new().build())
        .manage(OverlayState::default())
        .manage(appearance::AppearanceState::default())
        .manage(StageState::default())
        .manage(Magnifier::default())
        .invoke_handler(tauri::generate_handler![
            core_call,
            core_ready,
            core_restart,
            overlay_show,
            overlay_hide,
            overlay_interactive,
            overlay_backdrop,
            overlay_reset_position,
            overlay_monitors,
            overlay_layout,
            overlay_snapshot,
            overlay_publish,
            overlay_action,
            stage_idle,
            show_main_window,
            hide_main_window,
            quit_app,
            elevation_status,
            elevation_set,
            elevation_restart
        ])
        .setup(move |app| {
            let handle = app.handle().clone();
            if smoke {
                smoke::create_windows(&handle)?;
            }
            if !smoke {
                // 兼容管理员权限窗口 is on but this start is not elevated (a shortcut): hand
                // over to the elevated logon task - no prompt - and leave before anything runs.
                let args: Vec<String> = std::env::args().collect();
                if let elevation::Startup::HandOver { show } =
                    elevation::startup(elevation::token(), &args, elevation::query_task)
                {
                    if elevation::spawn_relauncher(elevation::Relaunch::Task { show }).is_ok() {
                        std::process::exit(0);
                    }
                }
            }
            stage::recover_cursor();
            let core = CoreClient::new(handle.clone(), smoke, on_core_event);
            app.manage(Arc::clone(&core));
            // Elevated: a later non-elevated start must still be able to bring this window up.
            elevation::accept_handover(&app.config().identifier);
            shortcut::init(&handle, smoke);
            tray::build(&handle)?;
            if let Some(window) = app.get_webview_window("overlay") {
                overlay::prepare(&window);
                overlay::place(&window, None, None);
            }
            if let Some(window) = app.get_webview_window("stage") {
                stage::prepare(&window);
            }
            overlay::start_hit_test(handle.clone());
            overlay::start_placement_watch(handle.clone());
            stage::start_cursor_stream(handle.clone());
            if let Err(error) = core.spawn() {
                let _ = handle.emit("core://event", json!({ "event": "fatal", "data": error }));
            } else if start_hidden() {
                // The logon task starts in the tray; WebView2 never reports the page as hidden.
                core.notify("set_visible", json!({ "visible": false }));
            }
            if smoke {
                smoke::run(handle.clone());
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            if window.label() != "main" {
                return;
            }
            if let WindowEvent::CloseRequested { api, .. } = event {
                // Not managed yet while setup still builds the windows.
                let core = window.app_handle().try_state::<Arc<CoreClient>>();
                if !core.as_ref().is_some_and(|core| core.quitting.load(Ordering::SeqCst)) {
                    // Closing keeps DotVoice in the tray with the same armed state.
                    api.prevent_close();
                    let _ = window.hide();
                    if let Some(core) = core {
                        core.notify("set_visible", json!({ "visible": false }));
                    }
                }
            }
        })
        .on_page_load(|webview, payload| {
            let finished = payload.event() == tauri::webview::PageLoadEvent::Finished;
            if first_page_load(webview.label(), finished, start_hidden(), &PAGE_SHOWN) {
                if let Some(window) = webview.app_handle().get_webview_window("main") {
                    let _ = window.show();
                }
            }
        })
        .build(context)
        .expect("failed to build DotVoice");
    app.run(|app, event| {
        if let RunEvent::Exit = event {
            shortcut::exit(app);
            if let Some(lens) = app.try_state::<Magnifier>() {
                lens.stop();
            }
            if app.try_state::<StageState>().is_some() {
                stage::set_cursor_hidden(app, false);
            }
            // Whatever any thread did last, never leave a laser or blank pointer behind.
            stage::restore_system_cursor();
            if let Some(core) = app.try_state::<Arc<CoreClient>>() {
                core.shutdown(Duration::from_secs(3));
            }
        }
    });
}

#[cfg(test)]
mod tests {
    use super::{
        first_page_load, hotkey_problem, on_second_start, second_start, show_main, sync_hotkeys, wait_for, SecondStart,
    };
    use serde_json::json;
    use std::cell::RefCell;
    use std::sync::atomic::AtomicBool;
    use std::sync::Mutex;
    use std::time::Duration;

    #[test]
    fn a_hand_over_before_setup_managed_the_core_only_shows_the_window() {
        // The plugin calls back from its window procedure, where a panic aborts the process.
        let app = tauri::test::mock_app();
        on_second_start(app.handle());
        show_main(app.handle());
    }

    #[test]
    fn a_start_while_quitting_is_relaunched_once_instead_of_swallowed() {
        let queued = AtomicBool::new(false);
        assert_eq!(second_start(None, &queued), SecondStart::Show, "the core is not managed yet");
        assert_eq!(second_start(Some(false), &queued), SecondStart::Show);
        assert_eq!(second_start(Some(true), &queued), SecondStart::Relaunch);
        assert_eq!(second_start(Some(true), &queued), SecondStart::Ignore, "one relauncher is enough");
        // A restart (兼容管理员权限窗口) already queued its own relauncher.
        assert_eq!(second_start(Some(true), &AtomicBool::new(true)), SecondStart::Ignore);
    }

    /// Registers through a fake OS: accelerators in `taken` fail; returns (cleared, attempts, errors).
    fn sync(last: &Mutex<super::AppliedHotkeys>, prefs: &serde_json::Value, taken: &[&str]) -> (bool, Vec<String>, Vec<String>) {
        let cleared = RefCell::new(false);
        let attempts = RefCell::new(Vec::new());
        let errors = sync_hotkeys(last, prefs, || *cleared.borrow_mut() = true, |_name, accelerator| {
            attempts.borrow_mut().push(accelerator.to_string());
            if taken.contains(&accelerator) { Err("HotKey already registered".into()) } else { Ok(()) }
        });
        (cleared.into_inner(), attempts.into_inner(), errors.into_iter().map(|(accelerator, _)| accelerator).collect())
    }

    #[test]
    fn an_unchanged_hotkey_map_is_not_registered_again() {
        let last = Mutex::new(None);
        let prefs = json!({ "hotkeys": { "toggle_mode": "Ctrl+Alt+M" }, "present": { "zoom": 2.0 } });
        assert_eq!(sync(&last, &prefs, &[]), (true, vec!["Ctrl+Alt+M".into()], vec![]), "the first map is applied");
        // A zoom step or another setting re-sends the same map: no re-register, no repeated notice.
        let zoomed = json!({ "hotkeys": { "toggle_mode": "Ctrl+Alt+M" }, "present": { "zoom": 2.5 } });
        assert_eq!(sync(&last, &zoomed, &[]), (false, vec![], vec![]));
        assert_eq!(sync(&last, &json!({ "hotkeys": { "toggle_mode": "Ctrl+Alt+N" } }), &[]).1, ["Ctrl+Alt+N"]);
        assert!(sync(&last, &json!({ "hotkeys": {} }), &[]).0, "clearing them applies too");
    }

    #[test]
    fn a_hotkey_that_was_taken_is_retried_silently_until_it_registers() {
        let last = Mutex::new(None);
        let prefs = json!({ "hotkeys": { "toggle_mode": "Ctrl+Alt+M", "quick_note": "Ctrl+Alt+N" } });
        let (_, _, errors) = sync(&last, &prefs, &["Ctrl+Alt+M"]);
        assert_eq!(errors, ["Ctrl+Alt+M"], "reported once");
        // Still held by the other program: retried, not reported again; the working one stays.
        assert_eq!(sync(&last, &prefs, &["Ctrl+Alt+M"]), (false, vec!["Ctrl+Alt+M".into()], vec![]));
        // The other program closed: the next 'prefs' (or re-saving the same key) registers it.
        assert_eq!(sync(&last, &prefs, &[]), (false, vec!["Ctrl+Alt+M".into()], vec![]));
        assert_eq!(sync(&last, &prefs, &[]), (false, vec![], vec![]), "nothing left to retry");
    }

    #[test]
    fn hotkey_notices_explain_the_problem_in_chinese() {
        assert_eq!(hotkey_problem("Ctrl+Alt+M"), "已被其他程序或另一项快捷键占用，请换一个组合");
        assert_eq!(hotkey_problem("Ctrl+Alt+Nope"), "按键组合无法识别");
        assert_eq!(hotkey_problem("Ctrl+Alt"), "按键组合无法识别");
    }

    #[test]
    fn the_main_window_is_shown_once_its_page_has_finished_loading() {
        let shown = AtomicBool::new(false);
        assert!(!first_page_load("main", false, false, &shown), "Started: nothing painted yet");
        assert!(!first_page_load("overlay", true, false, &shown));
        assert!(!first_page_load("main", true, true, &shown), "the logon task starts in the tray");
        assert!(first_page_load("main", true, false, &shown));
        assert!(!first_page_load("main", true, false, &shown), "a reload keeps the window where the user put it");
    }

    #[test]
    fn a_restart_waits_for_a_session_that_began_during_the_uac_prompt() {
        let mut asked = 0;
        let idle = || {
            asked += 1;
            let done = asked >= 3;
            async move { done }
        };
        assert!(tauri::async_runtime::block_on(wait_for(idle, Duration::from_secs(5), Duration::from_millis(1))));
        assert_eq!(asked, 3, "asked again until the session ended");
        let busy = || async { false };
        assert!(!tauri::async_runtime::block_on(wait_for(busy, Duration::from_millis(30), Duration::from_millis(5))));
    }
}
