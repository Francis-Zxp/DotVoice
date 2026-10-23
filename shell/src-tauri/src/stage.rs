//! Present-mode stage: a transparent, click-through, never-activated window
//! that covers the monitor under the cursor. It draws the laser dot, the
//! spotlight and annotations. While something is shown, the cursor position is
//! streamed to it once per display frame (DwmFlush), and the stage follows the
//! cursor to another monitor.
//!
//! While the laser or spotlight is on, the system pointer can be hidden so only
//! the red dot shows, like a hardware presenter. For the laser the pointer is not
//! just hidden but *becomes* the dot's bright core: the hardware cursor plane is
//! drawn by the display with no composition delay, so the dot is exactly as quick
//! as a mouse, while the page adds the glow and trail around it. It is always
//! restored: when the tool is released, the mode changes, the app exits, and on
//! the next launch if the app ever died while it was replaced (a marker file
//! records that).

use serde_json::json;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Condvar, Mutex};
use std::time::Duration;
use tauri::{AppHandle, Emitter, Manager, PhysicalPosition, PhysicalSize, Runtime, WebviewWindow};
use windows_sys::Win32::Foundation::POINT;
use windows_sys::Win32::Graphics::Dwm::DwmFlush;
use windows_sys::Win32::Graphics::Gdi::{
    CreateBitmap, CreateDIBSection, DeleteObject, BITMAPINFO, BITMAPINFOHEADER, BI_RGB, DIB_RGB_COLORS,
};
use windows_sys::Win32::System::LibraryLoader::GetModuleHandleW;
use windows_sys::Win32::UI::WindowsAndMessaging::{
    CopyIcon, CreateCursor, CreateIconIndirect, DestroyCursor, GetCursorPos, SetSystemCursor, ShowWindow,
    SystemParametersInfoW, HCURSOR, ICONINFO, SPI_SETCURSORS, SW_HIDE,
};

/// The monitor the stage covers (to notice the cursor leaving it) and the window's
/// actual origin and scale (to map the cursor). They differ right after a move to a
/// monitor with another DPI, when Windows rescales the window.
#[derive(Clone, Copy)]
struct Placement {
    x: i32,
    y: i32,
    width: u32,
    height: u32,
    origin_x: i32,
    origin_y: i32,
    scale: f64,
}

#[derive(Default)]
pub struct StageState {
    pub active: Mutex<bool>,
    pub changed: Condvar,
    pub shown: AtomicBool,
    placement: Mutex<Option<Placement>>,
    /// (⑤ held: Some(is laser) / None, ② laser latched, annotation pen down)
    pointer: Mutex<(Option<bool>, bool, bool)>,
    look: Mutex<Look>,
}

/// What the system pointer currently is.
#[derive(Clone, Copy, PartialEq, Eq, Default, Debug)]
pub enum Look {
    #[default]
    Normal,
    Hidden,
    /// The laser core, drawn for this monitor scale (percent).
    Laser(u32),
}

fn cursor() -> (i32, i32) {
    let mut point = POINT { x: 0, y: 0 };
    unsafe { GetCursorPos(&mut point) };
    (point.x, point.y)
}

pub fn prepare(window: &WebviewWindow) {
    let _ = window.set_focusable(false);
    let _ = window.set_ignore_cursor_events(true);
    if let Ok(h) = window.hwnd() {
        crate::winstyle::make_tool_window(h.0 as _);
    }
}

/// Move the stage over the monitor that contains (x, y).
fn place(window: &WebviewWindow, state: &StageState, x: i32, y: i32) {
    let monitors = window.available_monitors().unwrap_or_default();
    if let Some(m) = monitors.iter().find(|m| {
        let (p, s) = (m.position(), m.size());
        x >= p.x && y >= p.y && x < p.x + s.width as i32 && y < p.y + s.height as i32
    }) {
        let (p, s) = (m.position(), m.size());
        // Crossing to a monitor with another DPI makes Windows rescale the window
        // (WM_DPICHANGED) while it moves, so size and position are set again after it.
        let _ = window.set_position(PhysicalPosition::new(p.x, p.y));
        let _ = window.set_size(PhysicalSize::new(s.width, s.height));
        let _ = window.set_position(PhysicalPosition::new(p.x, p.y));
        let origin = window.inner_position().map(|o| (o.x, o.y)).unwrap_or((p.x, p.y));
        let scale = window.scale_factor().unwrap_or_else(|_| m.scale_factor());
        *state.placement.lock().unwrap() = Some(Placement {
            x: p.x,
            y: p.y,
            width: s.width,
            height: s.height,
            origin_x: origin.0,
            origin_y: origin.1,
            scale,
        });
    }
}

/// Cover the monitor under the cursor and start streaming its position.
pub fn show(app: &AppHandle) {
    let Some(window) = app.get_webview_window("stage") else { return };
    let state = app.state::<StageState>();
    if !state.shown.swap(true, Ordering::SeqCst) {
        let (x, y) = cursor();
        place(&window, &state, x, y);
        crate::overlay::show(&window);
    }
    *state.active.lock().unwrap() = true;
    state.changed.notify_all();
    rescale_laser(&state);
}

/// The page found nothing left to draw. Ignored while ⑤ is held, ② latched or the pen
/// down: an idle report can arrive just after a new press and must not hide its laser or
/// stroke. Checked and hidden under one lock, so a press cannot slip in between.
pub fn hide_if_idle<R: Runtime>(app: &AppHandle<R>) {
    let state = app.state::<StageState>();
    // Taken: a press, stroke or hide is being recorded right now (not idle, or hidden anyway).
    // Never wait for it on the main thread: the reader's hide() holds it while ShowWindow
    // waits for the main thread.
    let Ok(mut pointer) = state.pointer.try_lock() else { return };
    if *pointer == (None, false, false) {
        hide_locked(app, &state, &mut pointer);
    }
}

pub fn hide<R: Runtime>(app: &AppHandle<R>) {
    let state = app.state::<StageState>();
    let mut pointer = state.pointer.lock().unwrap();
    hide_locked(app, &state, &mut pointer);
}

fn hide_locked<R: Runtime>(app: &AppHandle<R>, state: &StageState, pointer: &mut (Option<bool>, bool, bool)) {
    *state.active.lock().unwrap() = false;
    state.changed.notify_all();
    if state.shown.swap(false, Ordering::SeqCst) {
        if let Some(window) = app.get_webview_window("stage") {
            // tao's hide() is unreliable for this unfocusable tool window; same as the capsule.
            if let Ok(h) = window.hwnd() {
                unsafe { ShowWindow(h.0 as _, SW_HIDE) };
            }
        }
    }
    *pointer = (None, false, false);
    set_cursor_look(app, Look::Normal);
}

/// The annotation pen went down (a stroke starts) or up.
pub fn annotate<R: Runtime>(app: &AppHandle<R>, down: bool) {
    app.state::<StageState>().pointer.lock().unwrap().2 = down;
}

/// Pointer events from the core: track what is shown and hide/restore the system cursor.
pub fn pointer<R: Runtime>(app: &AppHandle<R>, tool: &str, active: bool, latched: bool, hide_cursor: bool) {
    let state = app.state::<StageState>();
    let draws = tool == "laser" || tool == "spotlight";
    let (held, latch) = {
        let mut pointer = state.pointer.lock().unwrap();
        if latched {
            pointer.1 = active;
        } else {
            pointer.0 = (active && draws).then_some(tool == "laser");
        }
        (pointer.0, pointer.1)
    };
    let look = if !hide_cursor {
        Look::Normal
    } else if latch || held == Some(true) {
        Look::Laser(current_scale(&state))
    } else if held == Some(false) {
        Look::Hidden
    } else {
        Look::Normal
    };
    set_cursor_look(app, look);
}

fn current_scale(state: &StageState) -> u32 {
    let scale = state.placement.lock().unwrap().map(|p| p.scale).unwrap_or(1.0);
    (scale * 100.0).round().clamp(100.0, 400.0) as u32
}

fn marker() -> std::path::PathBuf {
    crate::core_client::data_dir().join("runtime").join("cursor-hidden")
}

pub fn set_cursor_hidden(app: &AppHandle, hidden: bool) {
    set_cursor_look(app, if hidden { Look::Hidden } else { Look::Normal });
}

pub fn set_cursor_look<R: Runtime>(app: &AppHandle<R>, look: Look) {
    let state = app.state::<StageState>();
    // Held for the whole apply: the cursor thread (rescale), the core reader
    // (pointer) and the main thread (hide) can never interleave, so the cursor
    // Windows shows is always the one `look` records.
    let mut current = state.look.lock().unwrap();
    if *current == look {
        return;
    }
    *current = look;
    let laser = match look {
        Look::Normal => {
            restore_system_cursor();
            false
        }
        Look::Hidden => {
            let _ = std::fs::write(marker(), b"1");
            unsafe { replace_system_cursor(None) };
            false
        }
        Look::Laser(scale) => {
            let _ = std::fs::write(marker(), b"1");
            let core = unsafe { laser_cursor(scale) };
            let ok = !core.is_null();
            unsafe { replace_system_cursor(ok.then_some(core)) };
            ok
        }
    };
    drop(current);
    // The page then draws only the glow and trail around the pointer dot.
    let _ = app.emit_to("stage", "stage://laser-cursor", laser);
}

/// The cursor moved to a monitor with another scale: redraw the laser core for it.
fn rescale_laser(state: &StageState) {
    let now = current_scale(state);
    let mut look = state.look.lock().unwrap();
    // Only while the laser is still on (a release may have restored the pointer meanwhile).
    if let Look::Laser(scale) = *look {
        if scale != now {
            *look = Look::Laser(now);
            let core = unsafe { laser_cursor(now) };
            if !core.is_null() {
                unsafe { replace_system_cursor(Some(core)) };
            }
        }
    }
}

const CURSOR_IDS: [u32; 13] =
    [32512, 32513, 32514, 32515, 32516, 32642, 32643, 32644, 32645, 32646, 32648, 32649, 32650];

/// Every standard pointer becomes `cursor` (consumed), or a blank one for None.
unsafe fn replace_system_cursor(cursor: Option<HCURSOR>) {
    let shape = match cursor {
        Some(c) => c,
        None => {
            let and_plane = [0xFFu8; 128];
            let xor_plane = [0u8; 128];
            CreateCursor(GetModuleHandleW(std::ptr::null()), 0, 0, 32, 32, and_plane.as_ptr() as _, xor_plane.as_ptr() as _)
        }
    };
    if shape.is_null() {
        return;
    }
    for id in CURSOR_IDS {
        // SetSystemCursor takes ownership of the handle, so each slot gets a copy.
        let copy = CopyIcon(shape);
        if !copy.is_null() {
            SetSystemCursor(copy, id);
        }
    }
    DestroyCursor(shape);
}

/// RGBA (straight alpha) of the laser core at `r` logical pixels from its centre:
/// the white-hot centre and the bloom of the page laser, so the two line up.
pub fn laser_pixel(r: f64) -> [f64; 4] {
    fn ramp(stops: &[(f64, [f64; 4])], t: f64) -> [f64; 4] {
        let t = t.clamp(0.0, 1.0);
        for pair in stops.windows(2) {
            let ((t0, a), (t1, b)) = (pair[0], pair[1]);
            if t <= t1 {
                let k = if t1 > t0 { (t - t0) / (t1 - t0) } else { 0.0 };
                return std::array::from_fn(|i| a[i] + (b[i] - a[i]) * k);
            }
        }
        stops[stops.len() - 1].1
    }
    let bloom = ramp(
        &[(0.0, [255.0, 90.0, 110.0, 0.85]), (0.3, [255.0, 45.0, 75.0, 0.5]), (0.65, [255.0, 30.0, 60.0, 0.14]), (1.0, [255.0, 30.0, 60.0, 0.0])],
        r / 24.0,
    );
    let core = ramp(
        &[(0.0, [255.0, 255.0, 255.0, 1.0]), (0.42, [255.0, 236.0, 238.0, 1.0]), (0.7, [255.0, 70.0, 92.0, 1.0]), (1.0, [255.0, 40.0, 70.0, 0.0])],
        r / 5.6,
    );
    // core over bloom
    let a = core[3] + bloom[3] * (1.0 - core[3]);
    if a <= 0.0 {
        return [0.0; 4];
    }
    let mut out = [0.0; 4];
    for i in 0..3 {
        out[i] = (core[i] * core[3] + bloom[i] * bloom[3] * (1.0 - core[3])) / a;
    }
    out[3] = a;
    out
}

/// A 32-bit alpha cursor of the laser core, hotspot in the centre. Null on failure.
unsafe fn laser_cursor(scale_percent: u32) -> HCURSOR {
    let scale = scale_percent as f64 / 100.0;
    let size = ((52.0 * scale).round() as i32 / 2 * 2).clamp(32, 208);
    let mut info: BITMAPINFO = std::mem::zeroed();
    info.bmiHeader = BITMAPINFOHEADER {
        biSize: std::mem::size_of::<BITMAPINFOHEADER>() as u32,
        biWidth: size,
        biHeight: -size, // top-down
        biPlanes: 1,
        biBitCount: 32,
        biCompression: BI_RGB,
        ..std::mem::zeroed()
    };
    let mut bits: *mut std::ffi::c_void = std::ptr::null_mut();
    let color = CreateDIBSection(std::ptr::null_mut(), &info, DIB_RGB_COLORS, &mut bits, std::ptr::null_mut(), 0);
    if color.is_null() || bits.is_null() {
        return std::ptr::null_mut();
    }
    let pixels = std::slice::from_raw_parts_mut(bits as *mut u8, (size * size * 4) as usize);
    let centre = size as f64 / 2.0;
    for y in 0..size {
        for x in 0..size {
            let r = (x as f64 + 0.5 - centre).hypot(y as f64 + 0.5 - centre) / scale;
            let [red, green, blue, alpha] = laser_pixel(r);
            let i = ((y * size + x) * 4) as usize;
            pixels[i] = blue as u8;
            pixels[i + 1] = green as u8;
            pixels[i + 2] = red as u8;
            pixels[i + 3] = (alpha * 255.0).round() as u8;
        }
    }
    let mask = CreateBitmap(size, size, 1, 1, std::ptr::null());
    let icon = ICONINFO { fIcon: 0, xHotspot: (size / 2) as u32, yHotspot: (size / 2) as u32, hbmMask: mask, hbmColor: color };
    let cursor = CreateIconIndirect(&icon);
    DeleteObject(color);
    if !mask.is_null() {
        DeleteObject(mask);
    }
    cursor
}

/// Reload the user's cursor scheme. Safe to call any time.
pub fn restore_system_cursor() {
    unsafe {
        SystemParametersInfoW(SPI_SETCURSORS, 0, std::ptr::null_mut(), 0);
    }
    let _ = std::fs::remove_file(marker());
}

/// A previous run that died with the pointer hidden left a marker: restore it.
pub fn recover_cursor() {
    if marker().exists() {
        restore_system_cursor();
    }
}

/// Align the cached mapping with the window's actual origin/scale, and put the
/// window back over its monitor if Windows moved or resized it.
fn verify_placement(app: &AppHandle, state: &StageState) {
    let Some(window) = app.get_webview_window("stage") else { return };
    let Some(mut p) = *state.placement.lock().unwrap() else { return };
    let (x, y) = cursor();
    // A resolution/layout change may leave the cursor inside the old bounds.
    // Compare with the current monitor, rather than resizing to stale geometry.
    if let Ok(Some(m)) = window.monitor_from_point(x as f64, y as f64) {
        let (position, size) = (m.position(), m.size());
        if (p.x, p.y, p.width, p.height) != (position.x, position.y, size.width, size.height)
            || (p.scale - m.scale_factor()).abs() > 0.001 {
            place(&window, state, x, y);
            return;
        }
    }
    let (Ok(origin), Ok(size)) = (window.inner_position(), window.inner_size()) else {
        return;
    };
    if (origin.x, origin.y) != (p.x, p.y) || size.width != p.width || size.height != p.height {
        let _ = window.set_position(PhysicalPosition::new(p.x, p.y));
        let _ = window.set_size(PhysicalSize::new(p.width, p.height));
        let _ = window.set_position(PhysicalPosition::new(p.x, p.y));
    }
    // Read after correction so this frame never uses the pre-repair origin/DPI.
    if let (Ok(origin), Ok(scale)) = (window.inner_position(), window.scale_factor()) {
        p.origin_x = origin.x;
        p.origin_y = origin.y;
        p.scale = scale;
    }
    *state.placement.lock().unwrap() = Some(p);
}

pub fn start_cursor_stream(app: AppHandle) {
    std::thread::Builder::new()
        .name("stage-cursor".into())
        .spawn(move || {
            let mut last = (i32::MIN, i32::MIN);
            let mut verified = std::time::Instant::now();
            loop {
                let state = app.state::<StageState>();
                {
                    let guard = state.active.lock().unwrap();
                    let _ = state.changed.wait_timeout_while(guard, Duration::from_secs(30), |a| !*a).unwrap();
                }
                if !*state.active.lock().unwrap() {
                    last = (i32::MIN, i32::MIN);
                    continue;
                }
                // Once per composed frame: smooth at any refresh rate, no timer jitter.
                if unsafe { DwmFlush() } != 0 {
                    std::thread::sleep(Duration::from_millis(8));
                }
                let (x, y) = cursor();
                // Every 300 ms, re-read where the window really is: a DPI change can move
                // or resize it after place() measured it, which would misplace the dot.
                let verify = verified.elapsed() >= Duration::from_millis(300);
                if (x, y) == last && !verify {
                    continue;
                }
                last = (x, y);
                if verify {
                    verified = std::time::Instant::now();
                    verify_placement(&app, &state);
                }
                let mut placement = *state.placement.lock().unwrap();
                let outside = placement.map_or(true, |p| {
                    x < p.x || y < p.y || x >= p.x + p.width as i32 || y >= p.y + p.height as i32
                });
                if outside {
                    if let Some(window) = app.get_webview_window("stage") {
                        place(&window, &state, x, y);
                        placement = *state.placement.lock().unwrap();
                        rescale_laser(&state);
                    }
                }
                if let Some(p) = placement {
                    let _ = app.emit_to(
                        "stage",
                        "stage://cursor",
                        json!({ "x": (x - p.origin_x) as f64 / p.scale, "y": (y - p.origin_y) as f64 / p.scale }),
                    );
                }
            }
        })
        .expect("stage cursor thread");
}

#[cfg(test)]
mod tests {
    use super::{annotate, hide, hide_if_idle, laser_cursor, laser_pixel, pointer, StageState};
    use std::sync::atomic::Ordering;
    use tauri::Manager;
    use windows_sys::Win32::Graphics::Gdi::{DeleteObject, GetObjectW, BITMAP};
    use windows_sys::Win32::UI::WindowsAndMessaging::{DestroyCursor, GetIconInfo, ICONINFO};

    /// hide_cursor stays off throughout: the real system pointer is never touched here.
    #[test]
    fn an_idle_report_does_not_hide_a_stroke_or_laser_that_just_started() {
        let app = tauri::test::mock_app();
        app.manage(StageState::default());
        let app = app.handle();
        let state = app.state::<StageState>();
        let show = || {
            state.shown.store(true, Ordering::SeqCst);
            *state.active.lock().unwrap() = true;
        };
        let shown = || state.shown.load(Ordering::SeqCst) && *state.active.lock().unwrap();
        // The pen goes down just before the page's (450 ms) idle report arrives.
        show();
        annotate(app, true);
        hide_if_idle(app);
        assert!(shown(), "the stroke that just started stays");
        annotate(app, false);
        hide_if_idle(app);
        assert!(!shown());
        show();
        pointer(app, "laser", true, false, false);
        hide_if_idle(app);
        assert!(shown(), "the laser that was just pressed stays");
        pointer(app, "laser", false, false, false);
        hide_if_idle(app);
        assert!(!shown());
        // A press being recorded right now: the report is dropped, never waited for.
        show();
        let recording = state.pointer.lock().unwrap();
        hide_if_idle(app);
        drop(recording);
        assert!(shown());
        // A mode change hides it all and lifts the pen.
        annotate(app, true);
        hide(app);
        assert!(!shown());
        assert_eq!(*state.pointer.lock().unwrap(), (None, false, false));
    }

    /// The real Windows cursor is created (32-bit colour, centred hotspot, sized per scale).
    #[test]
    fn laser_cursor_is_a_colour_cursor_sized_for_the_monitor() {
        for (scale, side) in [(100, 52), (200, 104)] {
            unsafe {
                let cursor = laser_cursor(scale);
                assert!(!cursor.is_null());
                let mut info: ICONINFO = std::mem::zeroed();
                assert!(GetIconInfo(cursor, &mut info) != 0);
                assert!(!info.hbmColor.is_null(), "alpha colour bitmap");
                let mut bitmap: BITMAP = std::mem::zeroed();
                GetObjectW(info.hbmColor, std::mem::size_of::<BITMAP>() as i32, &mut bitmap as *mut _ as *mut _);
                assert_eq!((bitmap.bmWidth, bitmap.bmBitsPixel), (side, 32));
                assert_eq!((info.xHotspot, info.yHotspot), (side as u32 / 2, side as u32 / 2));
                DeleteObject(info.hbmColor);
                DeleteObject(info.hbmMask);
                DestroyCursor(cursor);
            }
        }
    }

    #[test]
    fn laser_core_is_white_hot_and_fades_out() {
        let centre = laser_pixel(0.0);
        assert!(centre[3] > 0.99 && centre[0] > 254.0 && centre[1] > 250.0);
        let rim = laser_pixel(12.0);
        assert!(rim[3] > 0.05 && rim[3] < 0.6 && rim[0] > 250.0 && rim[1] < 90.0, "{rim:?}");
        assert_eq!(laser_pixel(24.5)[3], 0.0);
    }
}
