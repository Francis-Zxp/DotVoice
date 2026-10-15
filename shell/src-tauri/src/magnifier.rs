//! Present-mode magnifier: a rounded lens that follows the cursor, drawn by the
//! Windows Magnification API on its own thread with its own message loop.
//! The lens never takes focus or mouse input and excludes itself from capture.

use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use windows_sys::Win32::Foundation::{HWND, LPARAM, LRESULT, POINT, RECT, WPARAM};
use windows_sys::Win32::Graphics::Dwm::DwmFlush;
use windows_sys::Win32::Graphics::Gdi::{CreateRoundRectRgn, InvalidateRect, SetWindowRgn};
use windows_sys::Win32::System::LibraryLoader::GetModuleHandleW;
use windows_sys::Win32::System::Threading::GetCurrentThreadId;
use windows_sys::Win32::UI::Magnification::{
    MagInitialize, MagSetWindowFilterList, MagSetWindowSource, MagSetWindowTransform, MagUninitialize,
    MAGTRANSFORM, MW_FILTERMODE_EXCLUDE, WC_MAGNIFIERW,
};
use windows_sys::Win32::UI::WindowsAndMessaging::{
    CreateWindowExW, DefWindowProcW, DestroyWindow, DispatchMessageW, GetCursorPos, PeekMessageW,
    PostThreadMessageW, RegisterClassW, SetLayeredWindowAttributes, SetWindowPos, ShowWindow,
    TranslateMessage, HWND_TOPMOST, LWA_ALPHA, MSG, PM_REMOVE, SWP_NOACTIVATE, SW_SHOWNOACTIVATE,
    WM_QUIT, WNDCLASSW, WS_CHILD, WS_EX_LAYERED, WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW,
    WS_EX_TOPMOST, WS_EX_TRANSPARENT, WS_POPUP, WS_VISIBLE,
};

const WIDTH: i32 = 380;
const HEIGHT: i32 = 240;
/// Zoom as f32 bits; prefs present.zoom (125–400 %) and the pen's volume keys set it.
static ZOOM: AtomicU32 = AtomicU32::new(0x4000_0000); // 2.0

/// Clamped to the prefs range; the running lens picks it up on its next frame.
pub fn set_zoom(percent: f64) {
    let zoom = (percent / 100.0).clamp(1.25, 4.0) as f32;
    ZOOM.store(zoom.to_bits(), Ordering::Relaxed);
}

fn zoom() -> f32 {
    f32::from_bits(ZOOM.load(Ordering::Relaxed))
}

fn wide(text: &str) -> Vec<u16> {
    text.encode_utf16().chain(std::iter::once(0)).collect()
}

unsafe extern "system" fn host_proc(hwnd: HWND, message: u32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
    DefWindowProcW(hwnd, message, wparam, lparam)
}

#[derive(Default)]
pub struct Magnifier {
    thread: Mutex<Option<(u32, Arc<AtomicBool>, std::thread::JoinHandle<()>)>>,
}

impl Magnifier {
    pub fn start(&self) {
        let mut slot = self.thread.lock().unwrap();
        if slot.is_some() {
            return;
        }
        let (tx, rx) = std::sync::mpsc::channel();
        let quit = Arc::new(AtomicBool::new(false));
        let flag = quit.clone();
        let handle = std::thread::spawn(move || unsafe { run(tx, flag) });
        // The thread reports its id before any slow initialisation, so the handle
        // is always kept and stop() can always end the lens.
        let id = rx.recv().unwrap_or(0);
        *slot = Some((id, quit, handle));
    }

    pub fn stop(&self) {
        if let Some((id, quit, handle)) = self.thread.lock().unwrap().take() {
            quit.store(true, Ordering::SeqCst);
            if id != 0 {
                unsafe {
                    PostThreadMessageW(id, WM_QUIT, 0, 0);
                }
            }
            let _ = handle.join();
        }
    }
}

unsafe fn run(ready: std::sync::mpsc::Sender<u32>, quit: Arc<AtomicBool>) {
    let _ = ready.send(GetCurrentThreadId());
    if quit.load(Ordering::SeqCst) || MagInitialize() == 0 {
        return;
    }
    let instance = GetModuleHandleW(std::ptr::null());
    let class = wide("DotVoiceLens");
    let wc = WNDCLASSW {
        lpfnWndProc: Some(host_proc),
        hInstance: instance,
        lpszClassName: class.as_ptr(),
        ..std::mem::zeroed()
    };
    RegisterClassW(&wc);
    let host = CreateWindowExW(
        WS_EX_TOPMOST | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
        class.as_ptr(),
        std::ptr::null(),
        WS_POPUP,
        0,
        0,
        WIDTH,
        HEIGHT,
        std::ptr::null_mut(),
        std::ptr::null_mut(),
        instance,
        std::ptr::null(),
    );
    if host.is_null() {
        MagUninitialize();
        return;
    }
    SetLayeredWindowAttributes(host, 0, 255, LWA_ALPHA);
    SetWindowRgn(host, CreateRoundRectRgn(0, 0, WIDTH + 1, HEIGHT + 1, 44, 44), 1);
    let lens = CreateWindowExW(
        0,
        WC_MAGNIFIERW,
        std::ptr::null(),
        WS_CHILD | WS_VISIBLE,
        0,
        0,
        WIDTH,
        HEIGHT,
        host,
        std::ptr::null_mut(),
        instance,
        std::ptr::null(),
    );
    let mut applied = zoom();
    let mut transform = MAGTRANSFORM { v: [applied, 0.0, 0.0, 0.0, applied, 0.0, 0.0, 0.0, 1.0] };
    MagSetWindowTransform(lens, &mut transform);
    let mut exclude = host;
    MagSetWindowFilterList(lens, MW_FILTERMODE_EXCLUDE, 1, &mut exclude);
    if quit.load(Ordering::SeqCst) {
        DestroyWindow(host);
        MagUninitialize();
        return;
    }
    ShowWindow(host, SW_SHOWNOACTIVATE);
    // One update per composed frame (DwmFlush) instead of a 16 ms timer, whose
    // ~15.6 ms system granularity made the lens step unevenly.
    let mut message: MSG = std::mem::zeroed();
    let mut last = POINT { x: i32::MIN, y: i32::MIN };
    'lens: loop {
        if quit.load(Ordering::SeqCst) {
            break 'lens; // stop() before this thread had a message queue
        }
        while PeekMessageW(&mut message, std::ptr::null_mut(), 0, 0, PM_REMOVE) != 0 {
            if message.message == WM_QUIT {
                break 'lens;
            }
            TranslateMessage(&message);
            DispatchMessageW(&message);
        }
        if DwmFlush() != 0 {
            std::thread::sleep(Duration::from_millis(8));
        }
        let mut point = POINT { x: 0, y: 0 };
        GetCursorPos(&mut point);
        let moved = point.x != last.x || point.y != last.y;
        last = point;
        if moved {
            SetWindowPos(
                host,
                HWND_TOPMOST,
                point.x - WIDTH / 2,
                point.y - HEIGHT / 2,
                0,
                0,
                SWP_NOACTIVATE | 0x0001, // SWP_NOSIZE
            );
        }
        let wanted = zoom();
        if wanted != applied {
            applied = wanted;
            let mut transform = MAGTRANSFORM { v: [applied, 0.0, 0.0, 0.0, applied, 0.0, 0.0, 0.0, 1.0] };
            MagSetWindowTransform(lens, &mut transform);
        }
        let half_w = (WIDTH as f32 / applied / 2.0) as i32;
        let half_h = (HEIGHT as f32 / applied / 2.0) as i32;
        MagSetWindowSource(
            lens,
            RECT { left: point.x - half_w, top: point.y - half_h, right: point.x + half_w, bottom: point.y + half_h },
        );
        InvalidateRect(lens, std::ptr::null(), 1);
    }
    DestroyWindow(host);
    MagUninitialize();
}
