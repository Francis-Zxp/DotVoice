//! Notification-area icon: status, enable/pause, input/notes mode, quit.

use serde_json::json;
use std::sync::Mutex;
use tauri::menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Manager, Wry};

use crate::core_client::CoreClient;
use std::sync::Arc;

const ONLINE: &[u8] = include_bytes!("../icons/status-online.ico");
const OFFLINE: &[u8] = include_bytes!("../icons/status-offline.ico");

pub struct TrayHandles {
    open: MenuItem<Wry>,
    arm: MenuItem<Wry>,
    mode_input: CheckMenuItem<Wry>,
    mode_notes: CheckMenuItem<Wry>,
    last: Mutex<(String, bool)>,
}

pub fn phase_label(phase: &str) -> &'static str {
    match phase {
        "idle" => "已就绪",
        "starting" => "正在接入",
        "recording" => "正在聆听",
        "processing" => "识别中",
        "error" => "连接异常",
        _ => "已暂停",
    }
}

pub fn build(app: &AppHandle) -> tauri::Result<()> {
    let open = MenuItem::with_id(app, "open", "打开点声", true, None::<&str>)?;
    let arm = MenuItem::with_id(app, "arm", "启用语音输入", true, None::<&str>)?;
    let mode_input = CheckMenuItem::with_id(app, "mode_input", "输入到光标", true, true, None::<&str>)?;
    let mode_notes = CheckMenuItem::with_id(app, "mode_notes", "留作笔记", true, false, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "退出点声", true, None::<&str>)?;
    let menu = Menu::with_items(
        app,
        &[
            &open,
            &arm,
            &PredefinedMenuItem::separator(app)?,
            &mode_input,
            &mode_notes,
            &PredefinedMenuItem::separator(app)?,
            &quit,
        ],
    )?;
    TrayIconBuilder::with_id("main")
        .icon(crate::shortcut::small_icon(OFFLINE)?)
        .tooltip("点声 DotVoice · 双击打开")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| {
            let core = app.state::<Arc<CoreClient>>();
            match event.id().as_ref() {
                "open" => crate::show_main(app),
                "arm" => {
                    let armed = app
                        .state::<TrayHandles>()
                        .last
                        .lock()
                        .map(|l| l.1)
                        .unwrap_or(false);
                    core.notify("action", json!({ "action": if armed { "disarm" } else { "arm" } }));
                }
                "mode_input" => core.notify("set_mode", json!({ "value": "input", "source": "tray" })),
                "mode_notes" => core.notify("set_mode", json!({ "value": "notes", "source": "tray" })),
                "quit" => crate::quit(app),
                _ => {}
            }
        })
        .on_tray_icon_event(|tray, event| match event {
            TrayIconEvent::DoubleClick { button: MouseButton::Left, .. } => crate::show_main(tray.app_handle()),
            TrayIconEvent::Click { button: MouseButton::Left, button_state: MouseButtonState::Up, .. } => {}
            _ => {}
        })
        .build(app)?;
    app.manage(TrayHandles {
        open,
        arm,
        mode_input,
        mode_notes,
        last: unreported(),
    });
    Ok(())
}

/// Nothing reported yet. No real phase matches it, so the first report is always applied
/// (a core's first one, ("disconnected", false), included).
fn unreported() -> Mutex<(String, bool)> {
    Mutex::new((String::new(), false))
}

/// Remember this report; false when it repeats the last one.
fn record(last: &Mutex<(String, bool)>, phase: &str, armed: bool) -> bool {
    let mut last = last.lock().unwrap();
    if last.0 == phase && last.1 == armed {
        return false;
    }
    *last = (phase.to_string(), armed);
    true
}

pub fn update_phase(app: &AppHandle, phase: &str, armed: bool) {
    // Every report, even an unchanged one: the shortcut worker de-duplicates itself.
    crate::shortcut::update_phase(app, phase);
    let Some(handles) = app.try_state::<TrayHandles>() else { return };
    if !record(&handles.last, phase, armed) {
        return;
    }
    let online = matches!(phase, "idle" | "starting" | "recording" | "processing");
    let label = phase_label(phase);
    if let Some(tray) = app.tray_by_id("main") {
        if let Ok(icon) = crate::shortcut::small_icon(if online { ONLINE } else { OFFLINE }) {
            let _ = tray.set_icon(Some(icon));
        }
        let _ = tray.set_tooltip(Some(format!("点声 · {label} · 双击打开")));
    }
    let _ = handles.open.set_text(format!("打开点声 · {label}"));
    let _ = handles.arm.set_text(if armed { "暂停语音输入" } else { "启用语音输入" });
    // One light only: the dot of the d itself. No taskbar overlay badge on top of it
    // (an overlay plus a lit pin showed two dots); the window icon carries the state.
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.set_overlay_icon(None);
        if let Ok(icon) = crate::shortcut::big_icon(if online { ONLINE } else { OFFLINE }) {
            let _ = window.set_icon(icon);
        }
    }
}

pub fn update_mode(app: &AppHandle, value: &str) {
    if let Some(handles) = app.try_state::<TrayHandles>() {
        let _ = handles.mode_input.set_checked(value == "input");
        let _ = handles.mode_notes.set_checked(value == "notes");
    }
}

#[cfg(test)]
mod tests {
    use super::{record, unreported};

    #[test]
    fn the_first_report_is_applied_even_when_it_is_disconnected_and_paused() {
        let last = unreported();
        assert!(record(&last, "disconnected", false), "the core's first report sets icon and tooltip");
        assert!(!record(&last, "disconnected", false));
        assert!(record(&last, "disconnected", true));
        assert!(record(&last, "error", true));
        assert!(!record(&last, "error", true));
    }
}
