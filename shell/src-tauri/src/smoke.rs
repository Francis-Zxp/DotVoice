//! `DotVoice.exe --smoke`: native checks without microphone or HID.
//! The core runs with --smoke; results go to runtime/ui-smoke.json in its data folder
//! (<exe_dir>\runtime\smoke), the only file a run keeps there. Window captures and the
//! WebView2 profile go to a per-run temp folder (%TEMP%\dotvoice-smoke-<pid>) that is
//! removed once the run has exited; DOTVOICE_SMOKE_KEEP=1 keeps it (and the core's files).

use crate::core_client::{data_dir, CoreClient};
use crate::overlay;
use serde_json::{json, Map, Value};
use std::os::windows::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::Arc;
use std::time::{Duration, Instant};
use tauri::{AppHandle, Emitter, Manager, PhysicalPosition, WebviewWindowBuilder};
use windows_sys::Win32::Foundation::{CloseHandle, GetLastError, ERROR_ACCESS_DENIED, POINT, WAIT_TIMEOUT};
use windows_sys::Win32::System::Threading::{OpenProcess, WaitForSingleObject, PROCESS_SYNCHRONIZE};
use windows_sys::Win32::Graphics::Gdi::{
    BitBlt, CreateCompatibleDC, CreateDIBSection, DeleteDC, DeleteObject, GetDC, ReleaseDC, SelectObject,
    BITMAPINFO, BITMAPINFOHEADER, BI_RGB, CAPTUREBLT, DIB_RGB_COLORS, SRCCOPY,
};
use windows_sys::Win32::UI::WindowsAndMessaging::{
    FindWindowW, GetCursorPos, GetForegroundWindow, IsWindowVisible, WS_EX_NOACTIVATE,
    SendMessageW, PostMessageW, WM_DISPLAYCHANGE, WM_ENTERSIZEMOVE, WM_EXITSIZEMOVE,
};

const SCRATCH_PREFIX: &str = "dotvoice-smoke-";
const CLEANUP: &str = "--smoke-cleanup";
const KEEP: &str = "DOTVOICE_SMOKE_KEEP";
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

fn scratch_for(pid: u32) -> PathBuf {
    std::env::temp_dir().join(format!("{SCRATCH_PREFIX}{pid}"))
}

/// The pid in a temp folder name made by `scratch_for`, digits only.
fn scratch_pid(name: &str) -> Option<u32> {
    name.strip_prefix(SCRATCH_PREFIX)
        .filter(|pid| !pid.is_empty() && pid.bytes().all(|b| b.is_ascii_digit()))
        .and_then(|pid| pid.parse().ok())
}

/// This run's temp folder: everything it writes except ui-smoke.json.
pub fn scratch_dir() -> PathBuf {
    scratch_for(std::process::id())
}

/// The WebView2 profile of every smoke window (not %LOCALAPPDATA%\com.dotvoice.app.smoke).
fn webview_dir() -> PathBuf {
    scratch_dir().join("webview")
}

fn shots_dir() -> PathBuf {
    scratch_dir().join("shots")
}

/// The config's windows with their profile in this run's temp folder; main.rs turns their
/// automatic creation off for --smoke (a profile cannot be moved by the config alone).
pub fn create_windows(app: &AppHandle) -> tauri::Result<()> {
    for config in app.config().app.windows.clone() {
        WebviewWindowBuilder::from_config(app, &config)?.data_directory(webview_dir()).build()?;
    }
    Ok(())
}

fn alive(pid: u32) -> bool {
    unsafe {
        let handle = OpenProcess(PROCESS_SYNCHRONIZE, 0, pid);
        if handle.is_null() {
            return GetLastError() == ERROR_ACCESS_DENIED; // running, just not ours to open
        }
        let running = WaitForSingleObject(handle, 0) == WAIT_TIMEOUT;
        CloseHandle(handle);
        running
    }
}

/// Temp folders of earlier runs that never got their cleanup (killed, crashed), and the
/// profile that earlier builds kept in %LOCALAPPDATA%\com.dotvoice.app.smoke.
fn sweep_stale_scratch(app: &AppHandle) {
    if let Ok(old) = app.path().app_local_data_dir() {
        // Only the smoke identifier's folder: the real one holds the live app's WebView2 data.
        if old.file_name().is_some_and(|name| name.to_string_lossy().ends_with(".smoke")) {
            let _ = std::fs::remove_dir_all(old);
        }
    }
    let Ok(entries) = std::fs::read_dir(std::env::temp_dir()) else { return };
    for entry in entries.flatten() {
        let Some(pid) = entry.file_name().to_str().and_then(scratch_pid) else { continue };
        if pid != std::process::id() && !alive(pid) {
            let _ = std::fs::remove_dir_all(entry.path());
        }
    }
}

/// The smoke core's own files in its data folder (cue cache, smoke-* settings and records):
/// ui-smoke.json, a non-empty core.log and the cursor marker (a restore is pending) stay.
fn remove_core_files(runtime: &Path) {
    let Ok(entries) = std::fs::read_dir(runtime) else { return };
    for entry in entries.flatten() {
        let name = entry.file_name().to_string_lossy().to_lowercase();
        // Links and junctions are neither: never followed, never removed.
        let Ok(kind) = entry.file_type() else { continue };
        if kind.is_dir() && (name == "sounds" || name.starts_with("smoke-")) {
            let _ = std::fs::remove_dir_all(entry.path());
        } else if kind.is_file()
            && (name.starts_with("smoke-") || (name == "core.log" && entry.metadata().is_ok_and(|m| m.len() == 0)))
        {
            let _ = std::fs::remove_file(entry.path());
        }
    }
}

/// After the result is written: the core's files, the captures, and - once this process and
/// its WebView2 browser processes are gone, which only a helper can wait for - the profile.
fn clean_up(runtime: &Path) {
    if std::env::var_os(KEEP).is_some() {
        return;
    }
    remove_core_files(runtime);
    let _ = std::fs::remove_dir_all(shots_dir());
    if let Ok(exe) = std::env::current_exe() {
        let _ = Command::new(exe)
            .arg(CLEANUP)
            .arg(std::process::id().to_string())
            .creation_flags(CREATE_NO_WINDOW)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn();
    }
}

/// `DotVoice.exe --smoke-cleanup <pid>`: wait for that smoke run to exit, then remove its temp
/// folder. The folder is derived from the pid, so nothing else can ever be named for removal.
pub fn run_cleanup_helper() -> Option<i32> {
    let args: Vec<String> = std::env::args().collect();
    if args.get(1).map(String::as_str) != Some(CLEANUP) {
        return None;
    }
    let Some(pid) = args.get(2).and_then(|p| scratch_pid(&format!("{SCRATCH_PREFIX}{p}"))) else { return Some(2) };
    unsafe {
        let handle = OpenProcess(PROCESS_SYNCHRONIZE, 0, pid);
        if !handle.is_null() {
            WaitForSingleObject(handle, 120_000);
            CloseHandle(handle);
        }
    }
    let dir = scratch_for(pid);
    // The browser processes let go of the profile shortly after their host is gone.
    let removed = wait_until(Duration::from_secs(20), || std::fs::remove_dir_all(&dir).is_ok() || !dir.exists());
    Some(if removed { 0 } else { 1 })
}

/// Reddish pixels (the laser dot and its glow) in a square around a point on screen.
fn red_pixels_near(x: i32, y: i32, half: i32) -> usize {
    let size = half * 2;
    unsafe {
        let screen = GetDC(std::ptr::null_mut());
        let dc = CreateCompatibleDC(screen);
        let mut info: BITMAPINFO = std::mem::zeroed();
        info.bmiHeader = BITMAPINFOHEADER {
            biSize: std::mem::size_of::<BITMAPINFOHEADER>() as u32,
            biWidth: size,
            biHeight: -size,
            biPlanes: 1,
            biBitCount: 32,
            biCompression: BI_RGB,
            ..std::mem::zeroed()
        };
        let mut bits: *mut core::ffi::c_void = std::ptr::null_mut();
        let bitmap = CreateDIBSection(dc, &info, DIB_RGB_COLORS, &mut bits, std::ptr::null_mut(), 0);
        let mut count = 0;
        if !bitmap.is_null() && !bits.is_null() {
            let old = SelectObject(dc, bitmap);
            if BitBlt(dc, 0, 0, size, size, screen, x - half, y - half, SRCCOPY | CAPTUREBLT) != 0 {
                let px = std::slice::from_raw_parts(bits as *const u8, (size * size * 4) as usize);
                count = px.chunks_exact(4).filter(|p| p[2] > 200 && p[1] < 130 && p[0] < 150).count();
            }
            SelectObject(dc, old);
            DeleteObject(bitmap);
        }
        DeleteDC(dc);
        ReleaseDC(std::ptr::null_mut(), screen);
        count
    }
}

/// Save the on-screen pixels of a window's rectangle as a 32-bit BMP (visual check).
fn save_window_bmp(window: &tauri::WebviewWindow, path: &std::path::Path) -> bool {
    use windows_sys::Win32::Foundation::{HWND, RECT};
    use windows_sys::Win32::UI::WindowsAndMessaging::GetWindowRect;
    let Ok(h) = window.hwnd() else { return false };
    let mut r = RECT { left: 0, top: 0, right: 0, bottom: 0 };
    if unsafe { GetWindowRect(h.0 as HWND, &mut r) } == 0 {
        return false;
    }
    let (w, ht) = (r.right - r.left, r.bottom - r.top);
    if w <= 0 || ht <= 0 {
        return false;
    }
    unsafe {
        let screen = GetDC(std::ptr::null_mut());
        let dc = CreateCompatibleDC(screen);
        let mut info: BITMAPINFO = std::mem::zeroed();
        info.bmiHeader = BITMAPINFOHEADER {
            biSize: std::mem::size_of::<BITMAPINFOHEADER>() as u32,
            biWidth: w,
            biHeight: -ht,
            biPlanes: 1,
            biBitCount: 32,
            biCompression: BI_RGB,
            ..std::mem::zeroed()
        };
        let mut bits: *mut core::ffi::c_void = std::ptr::null_mut();
        let bitmap = CreateDIBSection(dc, &info, DIB_RGB_COLORS, &mut bits, std::ptr::null_mut(), 0);
        let mut ok = false;
        if !bitmap.is_null() && !bits.is_null() {
            let old = SelectObject(dc, bitmap);
            if BitBlt(dc, 0, 0, w, ht, screen, r.left, r.top, SRCCOPY | CAPTUREBLT) != 0 {
                let px = std::slice::from_raw_parts(bits as *const u8, (w * ht * 4) as usize);
                let mut file = Vec::with_capacity(54 + px.len());
                file.extend_from_slice(b"BM");
                file.extend_from_slice(&((54 + px.len()) as u32).to_le_bytes());
                file.extend_from_slice(&[0, 0, 0, 0]);
                file.extend_from_slice(&54u32.to_le_bytes());
                file.extend_from_slice(&40u32.to_le_bytes());
                file.extend_from_slice(&w.to_le_bytes());
                file.extend_from_slice(&(-ht).to_le_bytes());
                file.extend_from_slice(&1u16.to_le_bytes());
                file.extend_from_slice(&32u16.to_le_bytes());
                file.extend_from_slice(&[0u8; 24]);
                file.extend_from_slice(px);
                ok = std::fs::write(path, file).is_ok();
            }
            SelectObject(dc, old);
            DeleteObject(bitmap);
        }
        DeleteDC(dc);
        ReleaseDC(std::ptr::null_mut(), screen);
        ok
    }
}

/// Drive the real capsule with simulated core events and save what it looks like.
fn capsule_tour(app: &AppHandle, report: &mut Map<String, Value>) {
    let Some(window) = app.get_webview_window("overlay") else { return };
    overlay::show(&window);
    let dir = shots_dir();
    let send =|event: &str, data: Value| {
        let _ = app.emit_to("overlay", "core://event", json!({ "event": event, "data": data }));
    };
    let shot = |name: &str| save_window_bmp(&window, &dir.join(format!("smoke-capsule-{name}.bmp")));
    send("mode", json!({"value": "notes", "pending": null, "armed": true, "source": "key", "tool": "laser"}));
    std::thread::sleep(Duration::from_millis(1100));
    let mut saved = shot("1-chip");
    send("state", json!({"phase": "recording", "armed": true, "message": ""}));
    for (i, text) in ["今天想把", "今天想把胶囊的动画", "今天想把胶囊的动画做得更丝滑一些，像液态玻璃一样"].iter().enumerate() {
        std::thread::sleep(Duration::from_millis(300));
        send("partial", json!({"session": "smoke", "text": text, "final": false}));
        for k in 0..6 {
            send("level", json!(0.02 + 0.05 * (((i * 6 + k) as f64) * 0.7).sin().abs()));
            std::thread::sleep(Duration::from_millis(60));
        }
    }
    std::thread::sleep(Duration::from_millis(500));
    saved &= shot("2-note");
    send("state", json!({"phase": "processing", "armed": true, "message": ""}));
    send("result", json!({"text": "今天想把胶囊的动画做得更丝滑一些，像液态玻璃一样。", "seconds": 0.4, "envelope": [], "destination": "notes"}));
    send("state", json!({"phase": "idle", "armed": true, "message": ""}));
    std::thread::sleep(Duration::from_millis(1200));
    saved &= shot("3-saved");
    send("mode", json!({"value": "input", "pending": null, "armed": true, "source": "key", "tool": "laser"}));
    std::thread::sleep(Duration::from_millis(900));
    send("state", json!({"phase": "recording", "armed": true, "message": ""}));
    for k in 0..12 {
        send("level", json!(0.02 + 0.06 * ((k as f64) * 0.9).sin().abs()));
        if k == 6 {
            send("partial", json!({"session": "smoke2", "text": "打开设置页面", "final": false}));
        }
        std::thread::sleep(Duration::from_millis(80));
    }
    saved &= shot("4-input");
    send("state", json!({"phase": "disconnected", "armed": false, "message": ""}));
    std::thread::sleep(Duration::from_millis(600));
    report.insert("capsuleTourSaved".into(), json!(saved));
}

fn cursor() -> (i32, i32) {
    let mut point = POINT { x: 0, y: 0 };
    unsafe { GetCursorPos(&mut point) };
    (point.x, point.y)
}

fn wait_until(limit: Duration, mut check: impl FnMut() -> bool) -> bool {
    let end = Instant::now() + limit;
    while Instant::now() < end {
        if check() {
            return true;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    check()
}

/// Exercise the real HWND notification/drag path without changing Windows displays.
fn placement_checks(app: &AppHandle, report: &mut Map<String, Value>) {
    let Some(window) = app.get_webview_window("overlay") else { return };
    let Some(primary) = window.primary_monitor().ok().flatten() else { return };
    let Ok(hwnd) = window.hwnd() else { return };
    let area = primary.work_area();
    let expected = PhysicalPosition::new(
        area.position.x + (area.size.width as i32 - (500.0 * primary.scale_factor()).round() as i32) / 2,
        area.position.y + (12.0 * primary.scale_factor()).round() as i32,
    );
    overlay::place(&window, None);
    let before = unsafe { GetForegroundWindow() };
    let _ = window.set_position(PhysicalPosition::new(expected.x - 640, expected.y));
    unsafe { PostMessageW(hwnd.0 as _, WM_DISPLAYCHANGE, 32, 0); }
    let restored = wait_until(Duration::from_secs(4), || window.outer_position().ok() == Some(expected));
    report.insert("displayChangeRecentersCapsule".into(), json!(restored));
    report.insert("displayChangeKeepsFocus".into(), json!(unsafe { GetForegroundWindow() } == before));

    // Cross to each monitor, including negative origins and different DPI scales.
    let mut crosses = true;
    for m in window.available_monitors().unwrap_or_default() {
        let p = m.work_area().position;
        let _ = window.set_position(PhysicalPosition::new(p.x + 40, p.y + 40));
        overlay::place(&window, None);
        crosses &= wait_until(Duration::from_secs(3), || window.outer_position().ok() == Some(expected)
            && window.outer_size().ok().is_some_and(|s| s.width == (500.0 * primary.scale_factor()).round() as u32));
    }
    report.insert("crossDpiReturnsCentered".into(), json!(crosses));

    // Simulate a drag longer than the old four-second save window.
    unsafe { SendMessageW(hwnd.0 as _, WM_ENTERSIZEMOVE, 0, 0); }
    let saved = PhysicalPosition::new(expected.x + 90, expected.y + 40);
    let _ = window.set_position(saved);
    std::thread::sleep(Duration::from_millis(4200));
    unsafe { SendMessageW(hwnd.0 as _, WM_EXITSIZEMOVE, 0, 0); }
    let core = Arc::clone(&app.state::<Arc<CoreClient>>());
    let persisted = wait_until(Duration::from_secs(3), || {
        tauri::async_runtime::block_on(core.call("snapshot", Value::Null, Duration::from_secs(2))).ok()
            .is_some_and(|s| s["prefs"]["overlay"]["position"]["x"] == saved.x
                && s["prefs"]["overlay"]["position"]["y"] == saved.y)
    });
    report.insert("longDragPositionSaved".into(), json!(persisted));
    unsafe { PostMessageW(hwnd.0 as _, WM_DISPLAYCHANGE, 32, 0); }
    std::thread::sleep(Duration::from_millis(600));
    report.insert("displayChangePreservesManualPosition".into(), json!(window.outer_position().ok() == Some(saved)));
    let reset = tauri::async_runtime::block_on(crate::overlay_reset_position(app.clone())).is_ok();
    let cleared = tauri::async_runtime::block_on(core.call("snapshot", Value::Null, Duration::from_secs(3))).ok()
        .is_some_and(|s| s["prefs"]["overlay"]["position"].is_null());
    report.insert("resetClearsSavedPosition".into(), json!(reset && cleared && window.outer_position().ok() == Some(expected)));
}

/// A controlled window behind main distinguishes real compositor transparency
/// from a CSS-only tint. All settings/files belong to the isolated smoke core.
fn appearance_checks(app: &AppHandle, report: &mut Map<String, Value>) {
    use tauri::WebviewUrl;
    use windows_sys::Win32::Graphics::{Dwm::{DwmGetWindowAttribute, DWMWA_SYSTEMBACKDROP_TYPE}, Gdi::GetPixel};
    let Some(main) = app.get_webview_window("main") else { return };
    let Ok(hwnd) = main.hwnd() else { return };
    let core = app.state::<Arc<CoreClient>>();
    let set_theme = |name: &str| tauri::async_runtime::block_on(core.call("prefs_set",
        json!({"patch": {"appearance": {"theme": name}}}), Duration::from_secs(5))).is_ok();
    let backdrop = || {
        let mut value = 0u32;
        let ok = unsafe { DwmGetWindowAttribute(hwnd.0 as _, DWMWA_SYSTEMBACKDROP_TYPE as u32,
            &mut value as *mut _ as _, 4) } == 0;
        if ok { value } else { 0 }
    };
    let saved = core.ready.lock().unwrap().as_ref().and_then(|s| s["prefs"]["appearance"]["theme"].as_str()).unwrap_or("system").to_string();
    report.insert("glassNativeAcrylic".into(), json!(set_theme("glass") && wait_until(Duration::from_secs(4), || backdrop() == 3)));
    let dir = shots_dir();
    // about:blank is local, with no application handlers, microphone or network.
    let under = WebviewWindowBuilder::new(app, "smoke-underlay", WebviewUrl::External("about:blank".parse().unwrap()))
        .title("DotVoice visual test").decorations(false).skip_taskbar(true).visible(false)
        .data_directory(webview_dir()).build();
    if let Ok(under) = under {
        if let (Ok(pos), Ok(size)) = (main.outer_position(), main.outer_size()) {
            let _ = under.set_position(pos);
            let _ = under.set_size(size);
        }
        let _ = under.show();
        let _ = main.set_focus();
        std::thread::sleep(Duration::from_millis(700));
        let mut samples = Vec::new();
        report.insert("glassMainHasFocus".into(), json!(unsafe { GetForegroundWindow() } == hwnd.0 as _));
        for (name, color) in [("red", "#d66a69"), ("blue", "#668ddb")] {
            let _ = under.eval(format!("document.documentElement.style.background='{color}';document.body.style.margin='0';"));
            // WebView2 may stop painting a completely occluded test window.
            // Present its new color once before putting main back over it.
            let _ = under.set_focus();
            std::thread::sleep(Duration::from_millis(250));
            let _ = main.set_focus();
            std::thread::sleep(Duration::from_millis(900));
            let _ = save_window_bmp(&main, &dir.join(format!("smoke-main-glass-{name}.bmp")));
            if let (Ok(pos), Ok(scale)) = (main.outer_position(), main.scale_factor()) {
                unsafe {
                    let dc = GetDC(std::ptr::null_mut());
                    let pixel = GetPixel(dc, pos.x + (90. * scale) as i32, pos.y + (450. * scale) as i32);
                    ReleaseDC(std::ptr::null_mut(), dc);
                    samples.push(pixel);
                }
            }
        }
        report.insert("glassBackdropPixels".into(), json!(samples));
        let changes = samples.len() == 2 && [0, 8, 16].iter().map(|shift| {
            (((samples[0] >> shift) & 255) as i32 - ((samples[1] >> shift) & 255) as i32).abs()
        }).sum::<i32>() > 12;
        report.insert("glassShowsBehindWindow".into(), json!(changes));
        let _ = main.hide();
        let _ = main.show();
        let _ = main.set_focus();
        std::thread::sleep(Duration::from_millis(300));
        report.insert("glassSurvivesHideShow".into(), json!(backdrop() == 3));
        if let (Ok(original), Ok(size), Ok(monitors)) = (main.outer_position(), main.outer_size(), main.available_monitors()) {
            let mut across = true;
            for monitor in monitors {
                let p = monitor.position();
                let _ = main.set_position(PhysicalPosition::new(p.x + 30, p.y + 30));
                let _ = main.set_size(tauri::LogicalSize::new(960., 640.));
                std::thread::sleep(Duration::from_millis(250));
                if let (Ok(pos), Ok(size)) = (main.outer_position(), main.outer_size()) {
                    let _ = under.set_position(pos); let _ = under.set_size(size);
                    let mut pixels = Vec::new();
                    for color in ["#d66a69", "#668ddb"] {
                        let _ = under.eval(format!("document.documentElement.style.background='{color}'"));
                        let _ = under.set_focus(); std::thread::sleep(Duration::from_millis(200));
                        let _ = main.set_focus(); std::thread::sleep(Duration::from_millis(450));
                        let scale = main.scale_factor().unwrap_or(1.);
                        unsafe {
                            let dc = GetDC(std::ptr::null_mut());
                            pixels.push(GetPixel(dc, pos.x + (90. * scale) as i32, pos.y + (400. * scale) as i32));
                            ReleaseDC(std::ptr::null_mut(), dc);
                        }
                    }
                    across &= backdrop() == 3 && pixels[0] != pixels[1];
                } else { across = false; }
            }
            let _ = main.set_position(original); let _ = main.set_size(size);
            report.insert("glassAcrossCurrentMonitors".into(), json!(across));
        }
        let _ = under.close();
    }
    report.insert("darkClearsNativeBackdrop".into(), json!(set_theme("dark") && wait_until(Duration::from_secs(4), || backdrop() == 1)));
    std::thread::sleep(Duration::from_millis(400));
    let _ = save_window_bmp(&main, &dir.join("smoke-main-dark.bmp"));
    let _ = set_theme(&saved);
}

pub fn run(app: AppHandle) {
    std::thread::spawn(move || {
        if std::env::var_os(KEEP).is_none() {
            sweep_stale_scratch(&app);
        }
        let _ = std::fs::create_dir_all(shots_dir());
        let mut report = Map::new();
        let core = Arc::clone(&app.state::<Arc<CoreClient>>());
        report.insert("coreReady".into(), json!(wait_until(Duration::from_secs(20), || core
            .ready
            .lock()
            .unwrap()
            .is_some())));
        let snapshot = tauri::async_runtime::block_on(core.call("snapshot", Value::Null, Duration::from_secs(10)));
        report.insert("coreSnapshot".into(), json!(snapshot.as_ref().map(|s| s["smoke"] == true).unwrap_or(false)));
        placement_checks(&app, &mut report);
        let main = app.get_webview_window("main");
        report.insert("mainVisible".into(), json!(wait_until(Duration::from_secs(10), || main
            .as_ref()
            .and_then(|w| w.is_visible().ok())
            .unwrap_or(false))));
        appearance_checks(&app, &mut report);
        if let Some(window) = app.get_webview_window("overlay") {
            let style = overlay::ex_style(&window) as u32;
            report.insert("overlayNoActivate".into(), json!(style & WS_EX_NOACTIVATE != 0));
            // Pass-through toggles rebuild the style; activation must stay blocked.
            let _ = window.set_ignore_cursor_events(false);
            let _ = window.set_ignore_cursor_events(true);
            let style = overlay::ex_style(&window) as u32;
            report.insert("overlayNoActivateAfterToggle".into(), json!(style & WS_EX_NOACTIVATE != 0));
            std::thread::sleep(Duration::from_millis(600));
            let before = unsafe { GetForegroundWindow() } as isize;
            overlay::show(&window);
            std::thread::sleep(Duration::from_millis(400));
            let after = unsafe { GetForegroundWindow() } as isize;
            report.insert("overlayKeepsFocus".into(), json!(before == after));
            report.insert("overlayVisible".into(), json!(window.is_visible().unwrap_or(false)));
            // Hovering an expanded card makes the capsule clickable and back: it must stay on screen.
            overlay::set_click_through(&window, false);
            overlay::set_click_through(&window, true);
            report.insert("overlayStaysVisibleWhenInteractive".into(), json!(window.is_visible().unwrap_or(false)));
            let state = app.state::<overlay::OverlayState>();
            report.insert(
                "overlayClickThrough".into(),
                json!(state.passthrough.load(std::sync::atomic::Ordering::SeqCst)),
            );
            overlay::hide(&window);
            report.insert("overlayHidden".into(), json!(!window.is_visible().unwrap_or(true)));
        }
        report.insert("trayCreated".into(), json!(app.tray_by_id("main").is_some()));
        if let Some(window) = &main {
            let _ = window.close();
            report.insert("closedToTray".into(), json!(wait_until(Duration::from_secs(3), || !window
                .is_visible()
                .unwrap_or(true))));
            // A second launch must hand over to this instance and exit.
            let second = std::env::current_exe()
                .ok()
                .and_then(|exe| std::process::Command::new(exe).arg("--smoke").spawn().ok());
            report.insert("secondLaunchRestored".into(), json!(wait_until(Duration::from_secs(8), || window
                .is_visible()
                .unwrap_or(false))));
            if let Some(mut child) = second {
                let exited = wait_until(Duration::from_secs(5), || matches!(child.try_wait(), Ok(Some(_))));
                report.insert("secondInstanceExited".into(), json!(exited));
                if !exited {
                    let _ = child.kill();
                }
            }
        }
        capsule_tour(&app, &mut report);
        // Liquid-glass backdrop: pixels behind the capsule can be copied.
        if let Some(window) = app.get_webview_window("overlay") {
            let bytes = crate::backdrop::capture(&window, 200).map(|b| b.len()).unwrap_or(0);
            report.insert("backdropCaptured".into(), json!(bytes > 8 + 200 * 4));
        }
        // Present mode: the laser must actually reach the screen, then hide when idle.
        let (cx, cy) = cursor();
        let before = red_pixels_near(cx, cy, 40);
        let capsule_visible = app.state::<overlay::OverlayState>().visible.load(std::sync::atomic::Ordering::SeqCst);
        crate::stage::show(&app);
        report.insert("stageDoesNotChangeCapsuleVisibility".into(), json!(app.state::<overlay::OverlayState>().visible.load(std::sync::atomic::Ordering::SeqCst) == capsule_visible));
        let pointer = |active: bool| json!({"event": "pointer", "data": {"tool": "laser", "active": active}});
        let _ = app.emit_to("stage", "core://event", pointer(true));
        if let Some(stage) = app.get_webview_window("stage") {
            report.insert("stageVisible".into(), json!(wait_until(Duration::from_secs(3), || stage.is_visible().unwrap_or(false))));
            let covers = stage.current_monitor().ok().flatten().zip(stage.outer_size().ok())
                .map(|(m, s)| s.width >= m.size().width && s.height >= m.size().height).unwrap_or(false);
            report.insert("stageCoversMonitor".into(), json!(covers));
            // Sample at the current cursor each time: someone may be using the mouse.
            let mut best = 0;
            let drawn = wait_until(Duration::from_secs(3), || {
                let (cx, cy) = cursor();
                best = best.max(red_pixels_near(cx, cy, 40));
                best > before + 30
            });
            report.insert("laserDrawn".into(), json!(drawn));
            report.insert("laserRedPixels".into(), json!(format!("{before} -> {best}")));
            let _ = app.emit_to("stage", "core://event", pointer(false));
            report.insert("stageHidesWhenIdle".into(), json!(wait_until(Duration::from_secs(6), || !stage.is_visible().unwrap_or(true))));
        }
        // Hiding the pointer for the laser is recorded and always undone.
        let marker = data_dir().join("runtime").join("cursor-hidden");
        crate::stage::pointer(&app, "laser", true, false, true);
        let hidden = marker.exists();
        crate::stage::pointer(&app, "laser", false, false, true);
        report.insert("cursorHiddenAndRestored".into(), json!(hidden && !marker.exists()));
        // Magnifier lens window appears and goes away.
        let magnifier = app.state::<crate::magnifier::Magnifier>();
        magnifier.start();
        let class: Vec<u16> = "DotVoiceLens".encode_utf16().chain(std::iter::once(0)).collect();
        let lens = || unsafe {
            let h = FindWindowW(class.as_ptr(), std::ptr::null());
            !h.is_null() && IsWindowVisible(h) != 0
        };
        report.insert("magnifierShown".into(), json!(wait_until(Duration::from_secs(3), &lens)));
        magnifier.stop();
        report.insert("magnifierClosed".into(), json!(wait_until(Duration::from_secs(3), || !lens())));
        // Smoke cores refuse to arm; a phase other than disconnected would mean a device opened.
        let phase = tauri::async_runtime::block_on(core.call("snapshot", Value::Null, Duration::from_secs(5)))
            .ok()
            .and_then(|s| s["state"]["phase"].as_str().map(str::to_string));
        report.insert("microphoneOpened".into(), json!(phase.as_deref() != Some("disconnected")));
        core.shutdown(Duration::from_secs(5));
        report.insert("coreStoppedOnQuit".into(), json!(!core.is_running()));
        let runtime = data_dir().join("runtime");
        let _ = std::fs::write(runtime.join("ui-smoke.json"), serde_json::to_vec_pretty(&Value::Object(report)).unwrap_or_default());
        clean_up(&runtime);
        app.exit(0);
    });
}

#[cfg(test)]
mod tests {
    use super::{remove_core_files, scratch_for, scratch_pid};
    use std::fs;

    #[test]
    fn only_digit_pids_name_a_scratch_folder() {
        assert_eq!(scratch_pid("dotvoice-smoke-4242"), Some(4242));
        assert_eq!(scratch_for(4242).file_name().and_then(|n| n.to_str()).and_then(scratch_pid), Some(4242));
        for name in ["dotvoice-smoke-", "dotvoice-smoke-+1", "dotvoice-smoke-12a", "dotvoice-smoke-test-1",
                     "dotvoice-smoke-99999999999", "DotVoice", "dotvoice-core-12"] {
            assert_eq!(scratch_pid(name), None, "{name}");
        }
    }

    #[test]
    fn the_result_and_anything_unknown_stay() {
        let runtime = std::env::temp_dir().join(format!("dotvoice-smoke-test-{}", std::process::id())).join("runtime");
        let _ = fs::remove_dir_all(runtime.parent().unwrap());
        for name in ["sounds/v3-medium/armed.wav", "smoke-notes/a.md", "smoke-prefs.json", "smoke-usage.json",
                     "smoke-capsule-1-chip.bmp", "ui-smoke.json", "cursor-hidden", "prefs.json", "notes/a.md",
                     "secrets.json", "error.log"] {
            let path = runtime.join(name);
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(path, b"x").unwrap();
        }
        fs::write(runtime.join("core.log"), b"").unwrap();
        remove_core_files(&runtime);
        let mut left: Vec<String> = fs::read_dir(&runtime).unwrap().flatten()
            .map(|e| e.file_name().to_string_lossy().into_owned()).collect();
        left.sort();
        assert_eq!(left, ["cursor-hidden", "error.log", "notes", "prefs.json", "secrets.json", "ui-smoke.json"]);
        // A core.log with something in it is a failed run's diagnosis.
        fs::write(runtime.join("core.log"), b"Traceback").unwrap();
        remove_core_files(&runtime);
        assert!(runtime.join("core.log").exists());
        let _ = fs::remove_dir_all(runtime.parent().unwrap());
    }
}
