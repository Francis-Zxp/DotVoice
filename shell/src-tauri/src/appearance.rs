//! Match the native backdrop to the selected page palette. Only main uses
//! Acrylic; the capsule keeps its own independently rendered material.
use std::sync::Mutex;
use serde_json::Value;
use tauri::{window::{Color, Effect, EffectsBuilder}, AppHandle, Manager, Theme};
use windows_sys::Win32::Graphics::Dwm::{DwmExtendFrameIntoClientArea, DwmSetWindowAttribute, DWMWA_USE_IMMERSIVE_DARK_MODE};
use windows_sys::Win32::UI::Controls::MARGINS;

#[derive(Default)]
pub struct AppearanceState(Mutex<Option<String>>);

pub fn apply(app: &AppHandle, prefs: &Value) {
    let theme = prefs["appearance"]["theme"].as_str().unwrap_or("system").to_string();
    let handle = app.clone();
    let _ = app.run_on_main_thread(move || {
        let state = handle.state::<AppearanceState>();
        let mut previous = state.0.lock().unwrap();
        if previous.as_deref() == Some(theme.as_str()) { return }
        let Some(window) = handle.get_webview_window("main") else { return };
        let native_theme = match theme.as_str() {
            "dark" | "glass" => Some(Theme::Dark),
            "light" => Some(Theme::Light),
            _ => None,
        };
        let result = window.set_theme(native_theme).and_then(|_| {
            window.set_background_color(Some(Color(0, 0, 0, 0)))?;
            // The frameless shadow has a one-pixel glass frame by default.
            // Extend it through the client area so the backdrop can show there.
            if let Ok(hwnd) = window.hwnd() {
                let margin = if theme == "glass" { -1 } else { 1 };
                let margins = MARGINS { cxLeftWidth: margin, cxRightWidth: margin, cyTopHeight: margin, cyBottomHeight: margin };
                unsafe {
                    if let Some(dark) = immersive_dark(native_theme) {
                        DwmSetWindowAttribute(hwnd.0 as _, DWMWA_USE_IMMERSIVE_DARK_MODE as u32, &dark as *const _ as _, 4);
                    }
                    DwmExtendFrameIntoClientArea(hwnd.0 as _, &margins);
                }
            }
            window.set_effects(if theme == "glass" {
                Some(EffectsBuilder::new().effect(Effect::Acrylic)
                    .color(Color(32, 35, 43, 120)).build())
            } else { None })
        });
        if result.is_ok() { *previous = Some(theme); }
    });
}

/// The DWM dark frame for an explicit theme. "system" leaves it to tao, which follows the
/// Windows setting (writing light here would put a light frame round a dark page).
fn immersive_dark(native_theme: Option<Theme>) -> Option<i32> {
    native_theme.map(|theme| i32::from(theme == Theme::Dark))
}

#[cfg(test)]
mod tests {
    use super::immersive_dark;
    use tauri::Theme;

    #[test]
    fn the_system_theme_leaves_the_dark_frame_to_windows() {
        assert_eq!(immersive_dark(None), None);
        assert_eq!(immersive_dark(Some(Theme::Dark)), Some(1));
        assert_eq!(immersive_dark(Some(Theme::Light)), Some(0));
    }
}
