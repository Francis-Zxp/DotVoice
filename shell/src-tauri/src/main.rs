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
use std::sync::atomic::Ordering;
use std::sync::{Arc, Mutex, OnceLock};
use std::time::Duration;
use tauri::{AppHandle, Emitter, Manager, RunEvent, WindowEvent};
use tauri_plugin_global_shortcut::{GlobalShortcutExt, ShortcutState};

/// `--hidden` (the logon task) starts in the tray, unless a restart the user asked for
/// (兼容管理员权限窗口) left the marker that says "show the window this once".
static START_HIDDEN: OnceLock<bool> = OnceLock::new();

fn start_hidden() -> bool {
    *START_HIDDEN.get_or_init(|| {
        std::env::args().any(|a| a == elevation::HIDDEN) && !elevation::take_show_marker()
    })
}

pub fn show_main(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
    }
    app.state::<Arc<CoreClient>>().notify("set_visible", json!({ "visible": true }));
}

pub fn quit(app: &AppHandle) {
    let core = Arc::clone(&app.state::<Arc<CoreClient>>());
    shortcut::quitting(app);
    let app = app.clone();
    std::thread::spawn(move || {
        // Includes an in-flight device handoff, audio release and Tidepool restore.
        core.shutdown(Duration::from_secs(45));
        app.exit(0);
    });
}

/// The hotkey map last applied. 'prefs' arrives on every settings change and zoom step;
/// re-registering an unchanged map would repeat its "cannot register" notice each time.
static LAST_HOTKEYS: Mutex<Option<Value>> = Mutex::new(None);

/// Remember `prefs.hotkeys`; false when it is the map already applied.
fn hotkeys_changed(last: &Mutex<Option<Value>>, prefs: &Value) -> bool {
    let wanted = prefs.get("hotkeys").cloned();
    let mut last = last.lock().unwrap();
    if last.is_some() && *last == wanted {
        return false;
    }
    *last = wanted;
    true
}

/// Register the user's global shortcuts; an invalid one is reported, not fatal.
fn apply_hotkeys(app: &AppHandle, prefs: &Value) {
    if !hotkeys_changed(&LAST_HOTKEYS, prefs) {
        return;
    }
    let shortcuts = app.global_shortcut();
    let _ = shortcuts.unregister_all();
    let Some(hotkeys) = prefs.get("hotkeys").and_then(Value::as_object) else { return };
    for (name, accelerator) in hotkeys {
        let Some(accelerator) = accelerator.as_str().filter(|a| !a.is_empty()) else { continue };
        let name = name.clone();
        let result = shortcuts.on_shortcut(accelerator, move |app, _shortcut, event| {
            if event.state() == ShortcutState::Pressed {
                app.state::<Arc<CoreClient>>().notify("hotkey", json!({ "name": name }));
            }
        });
        if let Err(error) = result {
            let _ = app.emit(
                "core://event",
                json!({ "event": "notice", "data": format!("快捷键 {accelerator} 无法注册：{error}") }),
            );
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
            apply_hotkeys(app, &data["prefs"]);
            appearance::apply(app, &data["prefs"]);
            overlay::apply_prefs(app, &data["prefs"]);
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
            if data["down"].as_bool() == Some(true) {
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
    app.state::<Arc<CoreClient>>().notify("set_visible", json!({ "visible": false }));
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

/// The relauncher waits until this instance is gone; quit gracefully after a moment, so
/// the page can say why it closes.
fn restart_into(app: &AppHandle, how: elevation::Relaunch) -> Result<(), String> {
    elevation::spawn_relauncher(how)?;
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
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| show_main(app)))
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
                let app = window.app_handle();
                if !app.state::<Arc<CoreClient>>().quitting.load(Ordering::SeqCst) {
                    // Closing keeps DotVoice in the tray with the same armed state.
                    api.prevent_close();
                    let _ = window.hide();
                    app.state::<Arc<CoreClient>>().notify("set_visible", json!({ "visible": false }));
                }
            }
        })
        .on_page_load(|webview, _payload| {
            if webview.label() == "main" && !start_hidden() {
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
    use super::hotkeys_changed;
    use serde_json::json;
    use std::sync::Mutex;

    #[test]
    fn an_unchanged_hotkey_map_is_not_registered_again() {
        let last = Mutex::new(None);
        let prefs = json!({ "hotkeys": { "toggle_mode": "Ctrl+Alt+M" }, "present": { "zoom": 2.0 } });
        assert!(hotkeys_changed(&last, &prefs), "the first map is applied");
        // A zoom step or another setting re-sends the same map: no re-register, no repeated notice.
        let zoomed = json!({ "hotkeys": { "toggle_mode": "Ctrl+Alt+M" }, "present": { "zoom": 2.5 } });
        assert!(!hotkeys_changed(&last, &zoomed));
        assert!(hotkeys_changed(&last, &json!({ "hotkeys": { "toggle_mode": "Ctrl+Alt+N" } })));
        assert!(hotkeys_changed(&last, &json!({ "hotkeys": {} })), "clearing them applies too");
    }
}
