//! The capsule window: transparent, topmost and never activated, so OpenLess
//! keeps typing into the user's own edit box. Transparent pixels stay
//! click-through; only the card rectangle reported by the page is interactive.

use serde::Deserialize;
use serde_json::json;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Condvar, Mutex};
use std::time::Duration;
use tauri::{AppHandle, Manager, PhysicalPosition, PhysicalSize, WebviewWindow};
use windows_sys::Win32::Foundation::{HWND, LPARAM, LRESULT, POINT, RECT, WPARAM};
use windows_sys::Win32::UI::Shell::{DefSubclassProc, RemoveWindowSubclass, SetWindowSubclass};
use windows_sys::Win32::UI::WindowsAndMessaging::{
    GetCursorPos, GetWindowLongPtrW, GetWindowRect, SetWindowLongPtrW, SetWindowPos, ShowWindow, GWL_EXSTYLE,
    HWND_TOPMOST, WS_EX_LAYERED, WS_EX_TRANSPARENT,
    SWP_NOACTIVATE, SWP_NOMOVE, SWP_NOSIZE, SWP_SHOWWINDOW, SW_HIDE, SW_SHOWNOACTIVATE,
    WM_DISPLAYCHANGE, WM_DPICHANGED, WM_ENTERSIZEMOVE, WM_EXITSIZEMOVE, WM_NCDESTROY,
    WM_POWERBROADCAST, WM_SETTINGCHANGE,
};

#[derive(Clone, Copy, Debug, Deserialize)]
pub struct Rect {
    pub x: f64,
    pub y: f64,
    pub width: f64,
    pub height: f64,
}

#[derive(Default)]
pub struct OverlayState {
    pub interactive: Mutex<Option<Rect>>,
    pub changed: Condvar,
    pub passthrough: AtomicBool,
    pub visible: AtomicBool,
    placement: Mutex<PlacementState>,
    reposition: Condvar,
}

#[derive(Default)]
struct PlacementState {
    saved: Option<(i32, i32)>,
    pending: bool,
    drag_origin: Option<(i32, i32)>,
}

impl OverlayState {
    pub fn set_interactive(&self, rect: Option<Rect>) {
        *self.interactive.lock().unwrap() = rect;
        self.changed.notify_all();
    }
}

fn hwnd(window: &WebviewWindow) -> Option<HWND> {
    window.hwnd().ok().map(|h| h.0 as HWND)
}

pub fn ex_style(window: &WebviewWindow) -> isize {
    hwnd(window).map(|h| unsafe { GetWindowLongPtrW(h, GWL_EXSTYLE) }).unwrap_or(0)
}

pub fn prepare(window: &WebviewWindow) {
    // tao recomputes the extended style on every flag change (e.g. cursor
    // pass-through), so WS_EX_NOACTIVATE must come from its own FOCUSABLE flag.
    let _ = window.set_focusable(false);
    let _ = window.set_ignore_cursor_events(true);
    if let Some(h) = hwnd(window) {
        crate::winstyle::make_tool_window(h);
        // The HWND owns this handle until WM_NCDESTROY. No window operations
        // are performed inside display notifications: placement is debounced.
        let data = Box::into_raw(Box::new(window.app_handle().clone()));
        if unsafe { SetWindowSubclass(h, Some(placement_proc), 0x4457, data as usize) } == 0 {
            unsafe { drop(Box::from_raw(data)); }
        }
    }
    if let Some(state) = window.app_handle().try_state::<OverlayState>() {
        state.passthrough.store(true, Ordering::SeqCst);
    }
}

#[derive(Clone, Copy, Debug)]
struct Screen {
    x: i32,
    y: i32,
    width: u32,
    height: u32,
    scale: f64,
}

impl Screen {
    fn from_monitor(m: &tauri::Monitor) -> Self {
        let area = m.work_area();
        Self { x: area.position.x, y: area.position.y, width: area.size.width,
            height: area.size.height, scale: m.scale_factor() }
    }

    fn size(self) -> PhysicalSize<u32> {
        // Same logical extent as tauri.conf.json; visible cards animate inside it.
        PhysicalSize::new((500.0 * self.scale).round() as u32, (580.0 * self.scale).round() as u32)
    }

    fn contains_saved(self, x: i32, y: i32) -> bool {
        let cx = x as i64 + self.size().width as i64 / 2;
        cx >= self.x as i64 && cx < self.x as i64 + self.width as i64
            && y as i64 >= self.y as i64 && (y as i64) < self.y as i64 + self.height as i64
    }

    fn position(self, saved: Option<(i32, i32)>) -> (i32, i32) {
        let size = self.size();
        let right = self.x + self.width.saturating_sub(size.width) as i32;
        let bottom = self.y + self.height.saturating_sub(size.height) as i32;
        match saved {
            Some((x, y)) => (x.clamp(self.x, right), y.clamp(self.y, bottom)),
            None => (self.x + self.width.saturating_sub(size.width) as i32 / 2,
                (self.y + (12.0 * self.scale).round() as i32).min(bottom)),
        }
    }
}

/// Place using the destination screen's DPI, not the previous screen's width.
pub fn place(window: &WebviewWindow, saved: Option<(i32, i32)>) {
    window.app_handle().state::<OverlayState>().placement.lock().unwrap().saved = saved;
    let monitors = window.available_monitors().unwrap_or_default();
    let restored = saved.and_then(|(x, y)| monitors.iter()
        .map(Screen::from_monitor).find(|m| m.contains_saved(x, y)));
    let target = restored.or_else(|| window.primary_monitor().ok().flatten()
        .as_ref().map(Screen::from_monitor));
    let Some(target) = target else { return };
    let (x, y) = target.position(if restored.is_some() { saved } else { None });
    let position = PhysicalPosition::new(x, y);
    if window.outer_position().ok() != Some(position) {
        let _ = window.set_position(position);
    }
    if window.outer_size().ok() != Some(target.size()) {
        let _ = window.set_size(target.size());
    }
    // Moving across DPI boundaries can itself resize/move the HWND.
    if window.outer_position().ok() != Some(position) {
        let _ = window.set_position(position);
    }
}

pub fn update_position(app: &AppHandle, saved: Option<(i32, i32)>) {
    let state = app.state::<OverlayState>();
    let mut placement = state.placement.lock().unwrap();
    if placement.saved != saved {
        placement.saved = saved;
        placement.pending = true;
        state.reposition.notify_all();
    }
}

unsafe extern "system" fn placement_proc(
    hwnd: HWND, message: u32, wparam: WPARAM, lparam: LPARAM, id: usize, data: usize,
) -> LRESULT {
    if message == WM_NCDESTROY {
        RemoveWindowSubclass(hwnd, Some(placement_proc), id);
        drop(Box::from_raw(data as *mut AppHandle));
        return DefSubclassProc(hwnd, message, wparam, lparam);
    }
    if !matches!(message, WM_DISPLAYCHANGE | WM_DPICHANGED | WM_SETTINGCHANGE
        | WM_POWERBROADCAST | WM_ENTERSIZEMOVE | WM_EXITSIZEMOVE) {
        return DefSubclassProc(hwnd, message, wparam, lparam);
    }
    let app = &*(data as *const AppHandle);
    if let Some(state) = app.try_state::<OverlayState>() {
        let mut p = state.placement.lock().unwrap();
        match message {
            WM_DISPLAYCHANGE | WM_DPICHANGED | WM_SETTINGCHANGE => p.pending = true,
            // PBT_APMRESUMESUSPEND / PBT_APMRESUMEAUTOMATIC: refresh after docking on wake.
            WM_POWERBROADCAST if wparam == 7 || wparam == 18 => p.pending = true,
            WM_ENTERSIZEMOVE | WM_EXITSIZEMOVE => {
                let mut rect = RECT { left: 0, top: 0, right: 0, bottom: 0 };
                if GetWindowRect(hwnd, &mut rect) != 0 {
                    let position = (rect.left, rect.top);
                    if message == WM_ENTERSIZEMOVE {
                        p.drag_origin = Some(position);
                    } else if p.drag_origin.take().is_some_and(|start| start != position) {
                        p.saved = Some(position);
                        // Save only an actual native drag, regardless of its duration.
                        if let Some(core) = app.try_state::<Arc<crate::CoreClient>>() {
                            core.notify("prefs_set", json!({"patch": {"overlay": {"position": {
                                "x": position.0, "y": position.1
                            }}}}));
                        }
                    }
                } else if message == WM_EXITSIZEMOVE {
                    p.drag_origin = None;
                }
            }
            _ => {}
        }
        state.reposition.notify_all();
    }
    DefSubclassProc(hwnd, message, wparam, lparam)
}

/// Sleeps until Windows reports a display/work-area/DPI change. No idle polling.
pub fn start_placement_watch(app: AppHandle) {
    std::thread::Builder::new().name("overlay-placement".into()).spawn(move || loop {
        let state = app.state::<OverlayState>();
        let guard = state.placement.lock().unwrap();
        let mut guard = state.reposition.wait_while(guard, |p| !p.pending || p.drag_origin.is_some()).unwrap();
        // Docking produces a burst of messages; use the final monitor layout.
        loop {
            let (next, result) = state.reposition.wait_timeout(guard, Duration::from_millis(200)).unwrap();
            guard = next;
            if result.timed_out() { break }
        }
        if guard.drag_origin.is_some() { continue }
        guard.pending = false;
        drop(guard);
        let handle = app.clone();
        let _ = app.run_on_main_thread(move || {
            let state = handle.state::<OverlayState>();
            let mut p = state.placement.lock().unwrap();
            if p.drag_origin.is_some() {
                p.pending = true;
                state.reposition.notify_all();
                return;
            }
            let saved = p.saved;
            drop(p);
            if let Some(window) = handle.get_webview_window("overlay") {
                place(&window, saved);
            }
        });
    }).expect("overlay placement thread");
}

pub fn show(window: &WebviewWindow) {
    if let Some(h) = hwnd(window) {
        unsafe {
            ShowWindow(h, SW_SHOWNOACTIVATE);
            SetWindowPos(
                h,
                HWND_TOPMOST,
                0,
                0,
                0,
                0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW,
            );
        }
    }
    if let Some(state) = window.app_handle().try_state::<OverlayState>().filter(|_| window.label() == "overlay") {
        state.visible.store(true, Ordering::SeqCst);
    }
}

pub fn hide(window: &WebviewWindow) {
    if let Some(h) = hwnd(window) {
        unsafe {
            ShowWindow(h, SW_HIDE);
        }
    }
    if let Some(state) = window.app_handle().try_state::<OverlayState>() {
        state.visible.store(false, Ordering::SeqCst);
        state.set_interactive(None);
    }
    set_passthrough(window, true);
}

fn set_passthrough(window: &WebviewWindow, passthrough: bool) {
    if let Some(state) = window.app_handle().try_state::<OverlayState>() {
        if state.passthrough.swap(passthrough, Ordering::SeqCst) != passthrough {
            set_click_through(window, passthrough);
        }
    }
}

/// Click-through on/off straight on the HWND's extended style.
///
/// Not through tao's set_ignore_cursor_events: the capsule is shown with a raw
/// ShowWindow (so it never takes focus), which tao does not know about. Any tao
/// flag change then rebuilds the style from tao's own record of the window -
/// "hidden" - and calls ShowWindow(SW_HIDE). That made the capsule vanish the
/// moment the pointer moved onto an expanded note card.
pub fn set_click_through(window: &WebviewWindow, through: bool) {
    let Some(h) = hwnd(window) else { return };
    unsafe {
        let ex = GetWindowLongPtrW(h, GWL_EXSTYLE);
        let bits = (WS_EX_TRANSPARENT | WS_EX_LAYERED) as isize;
        let next = if through { ex | bits } else { ex & !(WS_EX_TRANSPARENT as isize) };
        if next != ex {
            SetWindowLongPtrW(h, GWL_EXSTYLE, next);
        }
    }
}

/// While a card is expanded, follow the cursor at ~30 Hz and accept mouse
/// input only inside the card. Otherwise the thread sleeps on a condvar.
pub fn start_hit_test(app: AppHandle) {
    std::thread::Builder::new()
        .name("overlay-hit-test".into())
        .spawn(move || loop {
            let Some(window) = app.get_webview_window("overlay") else {
                std::thread::sleep(Duration::from_millis(500));
                continue;
            };
            let state = app.state::<OverlayState>();
            let rect = {
                let guard = state.interactive.lock().unwrap();
                if guard.is_none() {
                    drop(guard);
                    set_passthrough(&window, true);
                    let guard = state.interactive.lock().unwrap();
                    let (guard, _) = state
                        .changed
                        .wait_timeout_while(guard, Duration::from_secs(30), |r| r.is_none())
                        .unwrap();
                    *guard
                } else {
                    *guard
                }
            };
            match rect {
                Some(rect) if state.visible.load(Ordering::SeqCst) => {
                    let mut point = POINT { x: 0, y: 0 };
                    let inside = unsafe { GetCursorPos(&mut point) } != 0
                        && match (window.inner_position(), window.scale_factor()) {
                            (Ok(origin), Ok(scale)) => {
                                let x = (point.x - origin.x) as f64 / scale;
                                let y = (point.y - origin.y) as f64 / scale;
                                x >= rect.x
                                    && y >= rect.y
                                    && x <= rect.x + rect.width
                                    && y <= rect.y + rect.height
                            }
                            _ => false,
                        };
                    set_passthrough(&window, !inside);
                    std::thread::sleep(Duration::from_millis(33));
                }
                _ => {
                    set_passthrough(&window, true);
                    std::thread::sleep(Duration::from_millis(120));
                }
            }
        })
        .expect("overlay hit-test thread");
}

#[cfg(test)]
mod tests {
    use super::Screen;

    fn main_screen() -> Screen {
        Screen { x: 0, y: 0, width: 3840, height: 2088, scale: 1.5 }
    }

    #[test]
    fn docking_from_laptop_to_external_uses_new_center() {
        let laptop = Screen { width: 2560, height: 1528, ..main_screen() };
        assert_eq!(laptop.position(None), (905, 18));
        assert_eq!(main_screen().position(None), (1545, 18));
    }

    #[test]
    fn destination_dpi_determines_width_before_centering() {
        let screen = Screen { scale: 2.0, ..main_screen() };
        assert_eq!(screen.size().width, 1000);
        assert_eq!(screen.position(None), (1420, 24));
    }

    #[test]
    fn negative_monitor_origin_and_vertical_offset_are_supported() {
        let portrait = Screen { x: -2160, y: -200, width: 2160, height: 3744, scale: 2.0 };
        assert_eq!(portrait.position(None), (-1580, -176));
        assert!(portrait.contains_saved(-2000, 100));
        assert_eq!(portrait.position(Some((-2000, 100))), (-2000, 100));
    }

    #[test]
    fn saved_card_is_clamped_fully_into_work_area() {
        let screen = main_screen();
        assert!(screen.contains_saved(-200, 1800));
        assert_eq!(screen.position(Some((-200, 1800))), (0, 1218));
        assert_eq!(screen.position(Some((3400, 100))), (3090, 100));
    }

    #[test]
    fn removed_screen_and_right_boundary_are_not_valid_saved_positions() {
        let screen = main_screen();
        assert!(!screen.contains_saved(5000, 300));
        assert!(!screen.contains_saved(3510, 100));
        assert!(!screen.contains_saved(100, -20));
    }

    #[test]
    fn left_or_top_taskbar_is_respected() {
        let screen = Screen { x: 72, y: 48, width: 3768, height: 2040, scale: 1.5 };
        assert_eq!(screen.position(None), (1581, 66));
        assert_eq!(screen.position(Some((0, 0))), (72, 48));
    }
}
