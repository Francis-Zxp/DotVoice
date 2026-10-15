//! Keep helper windows (capsule, stage) out of the taskbar and Alt+Tab.
//! tao recomputes the extended style on every flag change and always adds
//! WS_EX_APPWINDOW, so the style is corrected in WM_STYLECHANGING instead.

use windows_sys::Win32::Foundation::{HWND, LPARAM, LRESULT, WPARAM};
use windows_sys::Win32::UI::Shell::{DefSubclassProc, SetWindowSubclass};
use windows_sys::Win32::UI::WindowsAndMessaging::{
    GetWindowLongPtrW, SetWindowLongPtrW, GWL_EXSTYLE, STYLESTRUCT, WM_STYLECHANGING, WS_EX_APPWINDOW,
    WS_EX_TOOLWINDOW,
};

unsafe extern "system" fn tool_window_proc(
    hwnd: HWND,
    message: u32,
    wparam: WPARAM,
    lparam: LPARAM,
    _id: usize,
    _data: usize,
) -> LRESULT {
    if message == WM_STYLECHANGING && wparam as i32 == GWL_EXSTYLE {
        let style = &mut *(lparam as *mut STYLESTRUCT);
        style.styleNew = (style.styleNew & !WS_EX_APPWINDOW) | WS_EX_TOOLWINDOW;
    }
    DefSubclassProc(hwnd, message, wparam, lparam)
}

/// Must run on the thread that owns the window (the Tauri main thread).
pub fn make_tool_window(hwnd: HWND) {
    unsafe {
        SetWindowSubclass(hwnd, Some(tool_window_proc), 0x4456, 0);
        // Re-apply the current style once so the filter takes effect now.
        let style = GetWindowLongPtrW(hwnd, GWL_EXSTYLE);
        SetWindowLongPtrW(hwnd, GWL_EXSTYLE, style);
    }
}
