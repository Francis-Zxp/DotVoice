//! Liquid-glass backdrop for the capsule. A transparent window cannot blur
//! what is behind it, so just before the card appears (while the overlay is
//! still fully transparent) the page asks for a small, downscaled copy of the
//! screen area behind the overlay window and blurs/refracts it itself.
//! The pixels stay in memory, are never written anywhere, and only the
//! overlay's own rectangle is read.

use tauri::WebviewWindow;
use windows_sys::Win32::Foundation::{HWND, RECT};
use windows_sys::Win32::Graphics::Gdi::{
    CreateCompatibleDC, CreateDIBSection, DeleteDC, DeleteObject, GetDC, ReleaseDC, SelectObject,
    SetBrushOrgEx, SetStretchBltMode, StretchBlt, BITMAPINFO, BITMAPINFOHEADER, BI_RGB,
    DIB_RGB_COLORS, HALFTONE, SRCCOPY,
};
use windows_sys::Win32::UI::WindowsAndMessaging::GetWindowRect;

/// Returns `[width u32 LE][height u32 LE][RGBA…]`, `width` pixels wide.
pub fn capture(window: &WebviewWindow, width: u32) -> Option<Vec<u8>> {
    let hwnd = window.hwnd().ok()?.0 as HWND;
    let mut rect = RECT { left: 0, top: 0, right: 0, bottom: 0 };
    unsafe {
        if GetWindowRect(hwnd, &mut rect) == 0 {
            return None;
        }
    }
    let (rw, rh) = (rect.right - rect.left, rect.bottom - rect.top);
    if rw <= 0 || rh <= 0 {
        return None;
    }
    let w = width.clamp(16, 640) as i32;
    let h = ((rh as f64) * (w as f64) / (rw as f64)).round().max(1.0) as i32;
    unsafe {
        let screen = GetDC(std::ptr::null_mut());
        if screen.is_null() {
            return None;
        }
        let dc = CreateCompatibleDC(screen);
        let mut info: BITMAPINFO = std::mem::zeroed();
        info.bmiHeader = BITMAPINFOHEADER {
            biSize: std::mem::size_of::<BITMAPINFOHEADER>() as u32,
            biWidth: w,
            biHeight: -h, // top-down
            biPlanes: 1,
            biBitCount: 32,
            biCompression: BI_RGB,
            ..std::mem::zeroed()
        };
        let mut bits: *mut core::ffi::c_void = std::ptr::null_mut();
        let bitmap = CreateDIBSection(dc, &info, DIB_RGB_COLORS, &mut bits, std::ptr::null_mut(), 0);
        let mut out = None;
        if !bitmap.is_null() && !bits.is_null() {
            let old = SelectObject(dc, bitmap);
            SetStretchBltMode(dc, HALFTONE);
            SetBrushOrgEx(dc, 0, 0, std::ptr::null_mut());
            if StretchBlt(dc, 0, 0, w, h, screen, rect.left, rect.top, rw, rh, SRCCOPY) != 0 {
                let pixels = std::slice::from_raw_parts(bits as *const u8, (w * h * 4) as usize);
                let mut data = Vec::with_capacity(8 + pixels.len());
                data.extend_from_slice(&(w as u32).to_le_bytes());
                data.extend_from_slice(&(h as u32).to_le_bytes());
                for px in pixels.chunks_exact(4) {
                    data.extend_from_slice(&[px[2], px[1], px[0], 255]);
                }
                out = Some(data);
            }
            SelectObject(dc, old);
            DeleteObject(bitmap);
        }
        DeleteDC(dc);
        ReleaseDC(std::ptr::null_mut(), screen);
        out
    }
}
