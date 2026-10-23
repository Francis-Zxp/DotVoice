//! One controller owns the capsule state machine; mirror windows only render its model.
//! All windows share one CoreClient and keep their own placement and click-through state.
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet};
use std::hash::{Hash, Hasher};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::time::Duration;
use tauri::{AppHandle, Emitter, Manager, PhysicalPosition, PhysicalSize, WebviewWindow, WebviewWindowBuilder};
use windows_sys::Win32::Foundation::{HWND, LPARAM, LRESULT, POINT, RECT, WPARAM};
use windows_sys::Win32::UI::Shell::{DefSubclassProc, RemoveWindowSubclass, SetWindowSubclass};
use windows_sys::Win32::UI::WindowsAndMessaging::{
    GetCursorPos, GetWindowLongPtrW, GetWindowRect, SetWindowLongPtrW, SetWindowPos, ShowWindow,
    GWL_EXSTYLE, HWND_TOPMOST, SWP_NOACTIVATE, SWP_NOMOVE, SWP_NOSIZE, SWP_SHOWWINDOW,
    SW_HIDE, SW_SHOWNOACTIVATE, WM_DISPLAYCHANGE, WM_DPICHANGED, WM_ENTERSIZEMOVE,
    WM_EXITSIZEMOVE, WM_NCDESTROY, WM_POWERBROADCAST, WM_SETTINGCHANGE, WS_EX_LAYERED, WS_EX_TRANSPARENT,
};

const CONTROLLER: &str = "overlay";
const MIRROR_PREFIX: &str = "overlay-mirror-";
const ANCHORS: [&str; 9] = ["top-left", "top-center", "top-right", "center-left", "center", "center-right",
    "bottom-left", "bottom-center", "bottom-right"];

#[derive(Clone, Copy, Debug, Deserialize)]
pub struct Rect { pub x: f64, pub y: f64, pub width: f64, pub height: f64 }

#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct Layout { pub anchor: String, pub controller: bool, pub active: bool, pub monitor: Option<String> }
impl Layout {
    fn inactive(label: &str) -> Self {
        Self { anchor: "top-center".into(), controller: label == CONTROLLER, active: false, monitor: None }
    }
}

struct WindowState {
    layout: Layout, screen: Option<Screen>, interactive: Option<Rect>,
    passthrough: bool, visible: bool, drag_origin: Option<((i32, i32), u64)>,
}
impl WindowState {
    fn new(label: &str) -> Self {
        Self { layout: Layout::inactive(label), screen: None, interactive: None,
            passthrough: true, visible: false, drag_origin: None }
    }
}

#[derive(Clone, Debug, PartialEq)]
struct Settings {
    enabled: bool, monitor: Option<String>, all_monitors: bool, anchor: String,
    legacy: Option<(i32, i32)>, positions: BTreeMap<String, (i32, i32)>,
}
impl Default for Settings {
    fn default() -> Self {
        Self { enabled: true, monitor: None, all_monitors: false, anchor: "top-center".into(),
            legacy: None, positions: BTreeMap::new() }
    }
}
fn point(value: &Value) -> Option<(i32, i32)> {
    Some((value.get("x")?.as_i64()?.try_into().ok()?, value.get("y")?.as_i64()?.try_into().ok()?))
}
impl Settings {
    fn from_prefs(prefs: &Value) -> Self {
        let o = &prefs["overlay"];
        Self {
            enabled: o["enabled"].as_bool().unwrap_or(true),
            monitor: o["monitor"].as_str().filter(|s| !s.is_empty()).map(str::to_owned),
            all_monitors: o["all_monitors"].as_bool().unwrap_or(false),
            anchor: o["anchor"].as_str().filter(|s| ANCHORS.contains(s)).unwrap_or("top-center").into(),
            legacy: point(&o["position"]),
            positions: o["positions"].as_object().into_iter().flatten()
                .filter_map(|(name, value)| point(value).map(|p| (name.clone(), p))).collect(),
        }
    }
}
/// `cursor`: where the drag ended, on the card itself (the host's corner can lie off-screen).
struct Drag { monitor: String, position: (i32, i32), cursor: Option<(i32, i32)>, generation: u64 }
#[derive(Default)]
struct PlacementState {
    settings: Settings, initialized: bool, pending: bool, generation: u64,
    dragging: BTreeSet<String>, completed: Vec<Drag>,
    save_pending: Option<(Settings, u64)>, save_inflight: Option<(Settings, u64)>, save_running: bool,
    /// The core's settings as last confirmed (a snapshot, or a save it acknowledged).
    confirmed: Option<Settings>,
}
impl PlacementState {
    fn reset_drags(&mut self) {
        self.generation = self.generation.wrapping_add(1);
        self.completed.clear();
        self.pending = true;
    }
    /// 恢复预设位置 is a local decision: it wins over any older snapshot and is saved next.
    fn reset_placement(&mut self) {
        self.settings.positions.clear();
        self.settings.legacy = None;
        self.save_pending = None;
        self.reset_drags();
    }
    fn update(&mut self, mut settings: Settings) -> bool {
        if self.save_inflight.as_ref().is_some_and(|(sent, _)| &settings == sent) {
            self.confirmed = Some(settings);
            return false; // A delayed complete snapshot also contains stale anchor/enabled fields.
        }
        // The core echoes each save before replying. An older in-flight save must not
        // overwrite a newer local drag/reset; unrelated settings in that echo still apply.
        let echo = self.save_inflight.as_ref().is_some_and(|(sent, _)| settings.monitor == sent.monitor
            && settings.positions == sent.positions && settings.legacy.is_none());
        // Another prefs_set (theme, zoom step) that committed just before the in-flight save
        // emits what the core had before it: not a decision, so the drag stays and stays queued.
        let stale = self.save_inflight.is_some() && self.confirmed.as_ref() == Some(&settings);
        if !stale { self.confirmed = Some(settings.clone()); }
        if echo || stale {
            settings.monitor = self.settings.monitor.clone();
            settings.positions = self.settings.positions.clone();
            settings.legacy = self.settings.legacy;
        }
        if self.initialized && self.settings == settings { return false }
        self.settings = settings; self.initialized = true; self.pending = true;
        if !stale {
            self.save_pending = None;
            self.reset_drags();
        }
        true
    }
    fn finish_drags(&mut self, screens: &[Display]) -> bool {
        let mut changed = false;
        for drag in std::mem::take(&mut self.completed) {
            if drag.generation == self.generation {
                apply_drag(&mut self.settings, screens, drag);
                changed = true;
            }
        }
        changed
    }
    fn queue_save(&mut self) -> bool {
        self.save_pending = Some((self.settings.clone(), self.generation));
        if self.save_running { false } else { self.save_running = true; true }
    }
    fn next_save(&mut self) -> Option<(Settings, u64)> {
        let next = self.save_pending.take();
        self.save_inflight = next.clone();
        if next.is_none() { self.save_running = false; }
        next
    }
    fn saved(&mut self, sent: &Settings, generation: u64, ok: bool) {
        self.save_inflight = None;
        if let Some(confirmed) = self.confirmed.as_mut().filter(|_| ok) {
            confirmed.monitor = sent.monitor.clone();
            confirmed.positions = sent.positions.clone();
            confirmed.legacy = None;
        }
        if ok && self.save_pending.is_none() && (self.generation != generation
            || self.settings.monitor != sent.monitor || self.settings.positions != sent.positions) {
            // If Reset won while the request was already in flight, persist that newer
            // decision next. No other save can overtake this worker's requests.
            self.save_pending = Some((self.settings.clone(), self.generation));
        }
    }
}
#[derive(Clone, Serialize)]
pub struct PublishedModel { pub seq: u64, pub model: Value }
#[derive(Default)]
pub struct OverlayState {
    windows: Mutex<BTreeMap<String, WindowState>>, changed: Condvar,
    /// Visibility requested by the controller, separate from each renderer's native visibility.
    pub visible: AtomicBool,
    placement: Mutex<PlacementState>, reposition: Condvar,
    model: Mutex<Option<PublishedModel>>,
}

#[derive(Serialize)]
pub struct MonitorInfo { name: Option<String>, primary: bool, width: u32, height: u32, scale_factor: f64 }
#[derive(Clone, Copy, Debug, PartialEq)]
struct Screen { x: i32, y: i32, width: u32, height: u32, scale: f64 }
impl Screen {
    fn from_monitor(m: &tauri::Monitor) -> Self {
        let area = m.work_area();
        Self { x: area.position.x, y: area.position.y, width: area.size.width, height: area.size.height, scale: m.scale_factor() }
    }
    fn size(self) -> PhysicalSize<u32> {
        PhysicalSize::new(((500.0 * self.scale).round() as u32).min(self.width).max(1),
            ((580.0 * self.scale).round() as u32).min(self.height).max(1))
    }
    fn contains_saved(self, x: i32, y: i32) -> bool {
        let cx = x as i64 + self.size().width as i64 / 2;
        cx >= self.x as i64 && cx < self.x as i64 + self.width as i64
            && y as i64 >= self.y as i64 && (y as i64) < self.y as i64 + self.height as i64
    }
    fn contains(self, (x, y): (i32, i32)) -> bool {
        x as i64 >= self.x as i64 && (x as i64) < self.x as i64 + self.width as i64
            && y as i64 >= self.y as i64 && (y as i64) < self.y as i64 + self.height as i64
    }
    fn position(self, saved: Option<(i32, i32)>, anchor: &str) -> (i32, i32) {
        let size = self.size();
        let dx = self.width.saturating_sub(size.width) as i32;
        let dy = self.height.saturating_sub(size.height) as i32;
        if let Some((x, y)) = saved { return (x.clamp(self.x, self.x + dx), y.clamp(self.y, self.y + dy)) }
        // The page aligns the real card to this same anchor, with 12 logical pixels of padding.
        let x = if anchor.ends_with("left") { 0 } else if anchor.ends_with("right") { dx } else { dx / 2 };
        let y = if anchor.starts_with("bottom") { dy } else if anchor.starts_with("center") { dy / 2 } else { 0 };
        (self.x + x, self.y + y)
    }
}
#[derive(Clone)]
struct Display { name: String, screen: Screen, primary: bool }
fn display_name(m: &tauri::Monitor) -> String {
    m.name().filter(|s| !s.trim().is_empty()).cloned()
        .unwrap_or_else(|| format!("display@{},{}", m.position().x, m.position().y))
}
pub fn monitors(window: &WebviewWindow) -> Result<Vec<MonitorInfo>, String> {
    let primary = window.primary_monitor().map_err(|e| e.to_string())?;
    Ok(window.available_monitors().map_err(|e| e.to_string())?.iter().map(|m| MonitorInfo {
        name: Some(display_name(m)),
        primary: primary.as_ref().is_some_and(|p| p.position() == m.position() && p.size() == m.size()),
        width: m.size().width, height: m.size().height, scale_factor: m.scale_factor(),
    }).collect())
}
fn displays(window: &WebviewWindow) -> Vec<Display> {
    let primary = window.primary_monitor().ok().flatten();
    let mut result: Vec<_> = window.available_monitors().unwrap_or_default().iter().map(|m| Display {
        name: display_name(m), screen: Screen::from_monitor(m),
        primary: primary.as_ref().is_some_and(|p| p.position() == m.position() && p.size() == m.size()),
    }).collect();
    result.sort_by_key(|m| (!m.primary, m.name.clone()));
    result
}
fn mirror_label(name: &str) -> String {
    let mut hash = std::collections::hash_map::DefaultHasher::new();
    name.hash(&mut hash);
    format!("{MIRROR_PREFIX}{:016x}", hash.finish())
}
#[derive(Debug)]
struct Target { label: String, monitor: String, screen: Screen, saved: Option<(i32, i32)> }
fn targets(displays: &[Display], settings: &Settings) -> Vec<Target> {
    let first = displays.iter().find(|d| d.primary).or(displays.first());
    let selected = settings.monitor.as_ref().and_then(|name| displays.iter().find(|d| &d.name == name));
    let legacy = if settings.positions.is_empty() { settings.legacy } else { None };
    let old_screen = legacy.and_then(|(x, y)| displays.iter().find(|d| d.screen.contains_saved(x, y)));
    let chosen = if settings.all_monitors { first } else { old_screen.or(selected).or(first) };
    let Some(chosen) = chosen else { return Vec::new() };
    let mut shown = vec![chosen];
    if settings.all_monitors { shown.extend(displays.iter().filter(|d| d.name != chosen.name)); }
    shown.into_iter().enumerate().map(|(i, d)| Target {
        label: if i == 0 { CONTROLLER.into() } else { mirror_label(&d.name) },
        monitor: d.name.clone(), screen: d.screen,
        saved: settings.positions.get(&d.name).copied().or_else(|| legacy.filter(|(x, y)| d.screen.contains_saved(*x, *y))),
    }).collect()
}
fn apply_drag(settings: &mut Settings, screens: &[Display], drag: Drag) {
    let owner = screens.iter().find(|d| d.name == drag.monitor);
    let destination = if settings.all_monitors { owner } else {
        drag.cursor.and_then(|p| screens.iter().find(|d| d.screen.contains(p)))
            .or_else(|| screens.iter().find(|d| d.screen.contains_saved(drag.position.0, drag.position.1))).or(owner)
    };
    if let Some(destination) = destination {
        settings.positions.insert(destination.name.clone(), destination.screen.position(Some(drag.position), &settings.anchor));
        settings.legacy = None;
        // "Follow the primary screen" survives a drag there; targets() would show it there anyway.
        let first = screens.iter().find(|d| d.primary).or(screens.first());
        if !settings.all_monitors && (settings.monitor.is_some() || first.map(|d| &d.name) != Some(&destination.name)) {
            settings.monitor = Some(destination.name.clone());
        }
    }
}

pub fn is_overlay(label: &str) -> bool { label == CONTROLLER || label.starts_with(MIRROR_PREFIX) }
fn hwnd(window: &WebviewWindow) -> Option<HWND> { window.hwnd().ok().map(|h| h.0 as HWND) }
pub fn ex_style(window: &WebviewWindow) -> isize {
    hwnd(window).map(|h| unsafe { GetWindowLongPtrW(h, GWL_EXSTYLE) }).unwrap_or(0)
}
struct SubclassData { app: AppHandle, label: String }
pub fn prepare(window: &WebviewWindow) {
    let _ = window.set_focusable(false);
    let _ = window.set_ignore_cursor_events(true);
    let app = window.app_handle();
    app.state::<OverlayState>().windows.lock().unwrap().entry(window.label().into())
        .or_insert_with(|| WindowState::new(window.label()));
    if let Some(h) = hwnd(window) {
        crate::winstyle::make_tool_window(h);
        let data = Box::into_raw(Box::new(SubclassData { app: app.clone(), label: window.label().into() }));
        if unsafe { SetWindowSubclass(h, Some(placement_proc), 0x4457, data as usize) } == 0 { unsafe { drop(Box::from_raw(data)); } }
    }
}
pub fn layout(window: &WebviewWindow) -> Layout {
    window.app_handle().state::<OverlayState>().windows.lock().unwrap().get(window.label())
        .map(|w| w.layout.clone()).unwrap_or_else(|| Layout::inactive(window.label()))
}
pub fn snapshot(app: &AppHandle) -> Option<PublishedModel> { app.state::<OverlayState>().model.lock().unwrap().clone() }
pub fn publish(window: &WebviewWindow, model: Value) -> Result<PublishedModel, String> {
    if window.label() != CONTROLLER { return Err("只有胶囊控制窗口可以发布状态".into()) }
    if model.to_string().len() > 1024 * 1024 { return Err("胶囊状态过大".into()) }
    let state = window.app_handle().state::<OverlayState>();
    let value = {
        let mut cached = state.model.lock().unwrap();
        let value = PublishedModel { seq: cached.as_ref().map_or(1, |v| v.seq.saturating_add(1)), model };
        *cached = Some(value.clone()); value
    };
    let labels: Vec<_> = state.windows.lock().unwrap().keys().filter(|label| label.as_str() != CONTROLLER).cloned().collect();
    for label in labels { let _ = window.app_handle().emit_to(label, "overlay://model", &value); }
    Ok(value)
}
pub fn action(window: &WebviewWindow, action: Value) -> Result<(), String> {
    if !is_overlay(window.label()) || !layout(window).active { return Err("胶囊窗口未启用".into()) }
    if !matches!(action["type"].as_str(), Some("pin" | "dismiss")) || action.to_string().len() > 8192 {
        return Err("不支持的胶囊操作".into())
    }
    window.app_handle().emit_to(CONTROLLER, "overlay://action", action).map_err(|e| e.to_string())
}
pub fn set_interactive(window: &WebviewWindow, rect: Option<Rect>) {
    let state = window.app_handle().state::<OverlayState>();
    let rect = rect.filter(|r| [r.x, r.y, r.width, r.height].iter().all(|v| v.is_finite()) && r.width > 0.0 && r.height > 0.0);
    if let Some(w) = state.windows.lock().unwrap().get_mut(window.label()) { w.interactive = rect; }
    state.changed.notify_all();
}
pub fn apply_prefs(app: &AppHandle, prefs: &Value) {
    let settings = Settings::from_prefs(prefs);
    let state = app.state::<OverlayState>();
    let mut placement = state.placement.lock().unwrap();
    let enabled = settings.enabled;
    if placement.update(settings) {
        state.visible.store(enabled, Ordering::SeqCst);
        state.reposition.notify_all();
    }
}

pub fn reset_placement(app: &AppHandle) {
    let state = app.state::<OverlayState>();
    state.placement.lock().unwrap().reset_placement();
    state.reposition.notify_all();
}

/// The main thread constructs/destroys WebViews and reconciles their complete target set.
fn reconcile(app: &AppHandle) {
    let Some(controller) = app.get_webview_window(CONTROLLER) else { return };
    let state = app.state::<OverlayState>();
    let screens = displays(&controller);
    let (settings, start_save) = {
        let mut placement = state.placement.lock().unwrap();
        let save = placement.finish_drags(&screens);
        let start = save && placement.queue_save();
        (placement.settings.clone(), start)
    };
    if start_save { start_save_worker(app.clone()); }
    let targets = targets(&screens, &settings);
    let desired: BTreeSet<_> = targets.iter().map(|t| t.label.clone()).collect();
    for target in targets {
        let window = match app.get_webview_window(&target.label) {
            Some(w) => w,
            None => {
                let Some(mut config) = app.config().app.windows.iter().find(|w| w.label == CONTROLLER).cloned() else { continue };
                config.label = target.label.clone(); config.visible = false; config.focus = false;
                let Ok(builder) = WebviewWindowBuilder::from_config(app, &config) else { continue };
                let builder = if crate::is_smoke() { builder.data_directory(crate::smoke::scratch_dir().join("webview")) } else { builder };
                let window = match builder.build() {
                    Ok(w) => w,
                    Err(error) => {
                        let _ = app.emit("core://event", json!({"event":"notice","data":format!("无法在显示器 {} 创建胶囊：{error}", target.monitor)}));
                        continue;
                    }
                };
                prepare(&window); window
            }
        };
        let layout = Layout { anchor: settings.anchor.clone(), controller: target.label == CONTROLLER,
            active: true, monitor: Some(target.monitor.clone()) };
        let changed = {
            let mut windows = state.windows.lock().unwrap();
            let w = windows.entry(target.label.clone()).or_insert_with(|| WindowState::new(&target.label));
            let changed = w.layout != layout || w.screen != Some(target.screen);
            w.layout = layout.clone(); w.screen = Some(target.screen); changed
        };
        let (x, y) = target.screen.position(target.saved, &settings.anchor);
        let position = PhysicalPosition::new(x, y);
        if window.outer_position().ok() != Some(position) { let _ = window.set_position(position); }
        if window.outer_size().ok() != Some(target.screen.size()) { let _ = window.set_size(target.screen.size()); }
        if window.outer_position().ok() != Some(position) { let _ = window.set_position(position); }
        if state.visible.load(Ordering::SeqCst) { show_one(&window); } else { hide_one(&window); }
        if changed { let _ = window.emit_to(window.label(), "overlay://layout", &layout); }
    }
    let existing: Vec<_> = state.windows.lock().unwrap().keys().cloned().collect();
    for label in existing.into_iter().filter(|label| !desired.contains(label)) {
        if let Some(window) = app.get_webview_window(&label) {
            hide_one(&window);
            if label == CONTROLLER {
                let inactive = Layout::inactive(CONTROLLER);
                if let Some(w) = state.windows.lock().unwrap().get_mut(&label) { w.layout = inactive.clone(); }
                let _ = window.emit_to(window.label(), "overlay://layout", inactive);
            } else {
                let _ = window.destroy();
                state.windows.lock().unwrap().remove(&label);
            }
        }
    }
    state.changed.notify_all();
}

fn start_save_worker(app: AppHandle) {
    std::thread::Builder::new().name("overlay-save".into()).spawn(move || loop {
        let job = app.state::<OverlayState>().placement.lock().unwrap().next_save();
        let Some((settings, generation)) = job else { return };
        let positions: BTreeMap<_, _> = settings.positions.iter().map(|(name, (x, y))| (name, json!({"x":x,"y":y}))).collect();
        let core = Arc::clone(&app.state::<Arc<crate::CoreClient>>());
        let result = tauri::async_runtime::block_on(core.call("prefs_set",
            json!({"patch":{"overlay":{"position":null,"monitor":settings.monitor,"positions":positions}}}),
            Duration::from_secs(10)));
        let error = match result {
            Ok(value) => value["error"].as_str().map(str::to_owned),
            Err(error) => Some(error),
        };
        app.state::<OverlayState>().placement.lock().unwrap().saved(&settings, generation, error.is_none());
        if let Some(error) = error {
            let _ = app.emit("core://event", json!({"event":"notice","data":format!("胶囊位置未能保存：{error}")}));
        }
    }).expect("overlay save thread");
}

/// Initial placement and the isolated native test's legacy position path.
pub fn place(window: &WebviewWindow, saved: Option<(i32, i32)>, selected: Option<&str>) {
    {
        let state = window.app_handle().state::<OverlayState>();
        let mut placement = state.placement.lock().unwrap();
        placement.settings.legacy = saved; placement.settings.monitor = selected.map(str::to_owned);
    }
    let app = window.app_handle().clone();
    let target = app.clone();
    let _ = app.run_on_main_thread(move || reconcile(&target));
}

unsafe extern "system" fn placement_proc(hwnd: HWND, message: u32, wparam: WPARAM, lparam: LPARAM, id: usize, data: usize) -> LRESULT {
    let context = &*(data as *const SubclassData);
    if message == WM_NCDESTROY {
        RemoveWindowSubclass(hwnd, Some(placement_proc), id);
        if let Some(state) = context.app.try_state::<OverlayState>() {
            state.windows.lock().unwrap().remove(&context.label);
            state.placement.lock().unwrap().dragging.remove(&context.label);
            state.changed.notify_all(); state.reposition.notify_all();
        }
        drop(Box::from_raw(data as *mut SubclassData));
        return DefSubclassProc(hwnd, message, wparam, lparam);
    }
    if !matches!(message, WM_DISPLAYCHANGE | WM_DPICHANGED | WM_SETTINGCHANGE | WM_POWERBROADCAST | WM_ENTERSIZEMOVE | WM_EXITSIZEMOVE) {
        return DefSubclassProc(hwnd, message, wparam, lparam);
    }
    if let Some(state) = context.app.try_state::<OverlayState>() {
        let mut completed = None;
        if matches!(message, WM_ENTERSIZEMOVE | WM_EXITSIZEMOVE) {
            let generation = state.placement.lock().unwrap().generation;
            let mut rect = RECT { left: 0, top: 0, right: 0, bottom: 0 };
            let got = GetWindowRect(hwnd, &mut rect) != 0;
            let position = (rect.left, rect.top);
            // A real drag ends with the cursor over the host (a synthetic one may not).
            let mut pt = POINT { x: 0, y: 0 };
            let cursor = (message == WM_EXITSIZEMOVE && got && GetCursorPos(&mut pt) != 0
                && pt.x >= rect.left && pt.x < rect.right && pt.y >= rect.top && pt.y < rect.bottom).then_some((pt.x, pt.y));
            if let Some(w) = state.windows.lock().unwrap().get_mut(&context.label) {
                if message == WM_ENTERSIZEMOVE { w.drag_origin = got.then_some((position, generation)); }
                else if let Some((_, started)) = w.drag_origin.take().filter(|(origin, _)| got && *origin != position) {
                    if let Some(name) = w.layout.monitor.clone() { completed = Some(Drag { monitor: name, position, cursor, generation: started }); }
                }
            }
        }
        let mut p = state.placement.lock().unwrap();
        match message {
            WM_DISPLAYCHANGE | WM_DPICHANGED | WM_SETTINGCHANGE => p.pending = true,
            WM_POWERBROADCAST if wparam == 7 || wparam == 18 => p.pending = true,
            WM_ENTERSIZEMOVE => { p.dragging.insert(context.label.clone()); }
            WM_EXITSIZEMOVE => {
                p.dragging.remove(&context.label);
                if let Some(drag) = completed { p.completed.push(drag); p.pending = true; }
            }
            _ => {}
        }
        state.reposition.notify_all();
    }
    DefSubclassProc(hwnd, message, wparam, lparam)
}

/// Display changes are debounced; idle windows do not poll monitors.
pub fn start_placement_watch(app: AppHandle) {
    std::thread::Builder::new().name("overlay-placement".into()).spawn(move || loop {
        let state = app.state::<OverlayState>();
        let guard = state.placement.lock().unwrap();
        let mut guard = state.reposition.wait_while(guard, |p| !p.pending || !p.dragging.is_empty()).unwrap();
        loop {
            let (next, timeout) = state.reposition.wait_timeout(guard, Duration::from_millis(200)).unwrap();
            guard = next;
            if timeout.timed_out() { break }
        }
        if !guard.dragging.is_empty() { continue }
        guard.pending = false; drop(guard);
        let target = app.clone();
        let _ = app.run_on_main_thread(move || {
            let state = target.state::<OverlayState>();
            if !state.placement.lock().unwrap().dragging.is_empty() {
                state.placement.lock().unwrap().pending = true;
                state.reposition.notify_all(); return;
            }
            reconcile(&target);
        });
    }).expect("overlay placement thread");
}
fn show_one(window: &WebviewWindow) {
    if is_overlay(window.label()) && !layout(window).active { return }
    if let Some(h) = hwnd(window) { unsafe {
        ShowWindow(h, SW_SHOWNOACTIVATE);
        SetWindowPos(h, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW);
    } }
    if let Some(w) = window.app_handle().state::<OverlayState>().windows.lock().unwrap().get_mut(window.label()) { w.visible = true; }
    window.app_handle().state::<OverlayState>().changed.notify_all();
}
fn hide_one(window: &WebviewWindow) {
    if let Some(h) = hwnd(window) { unsafe { ShowWindow(h, SW_HIDE); } }
    let state = window.app_handle().state::<OverlayState>();
    if let Some(w) = state.windows.lock().unwrap().get_mut(window.label()) { w.visible = false; w.interactive = None; }
    set_passthrough(window, true); state.changed.notify_all();
}
pub fn show(window: &WebviewWindow) {
    if window.label() == CONTROLLER {
        let app = window.app_handle(); let state = app.state::<OverlayState>();
        state.visible.store(true, Ordering::SeqCst);
        let labels: Vec<_> = state.windows.lock().unwrap().keys().cloned().collect();
        for label in labels { if let Some(w) = app.get_webview_window(&label) { show_one(&w); } }
    } else { show_one(window); }
}
pub fn hide(window: &WebviewWindow) {
    if window.label() == CONTROLLER {
        let app = window.app_handle(); let state = app.state::<OverlayState>();
        state.visible.store(false, Ordering::SeqCst);
        let labels: Vec<_> = state.windows.lock().unwrap().keys().cloned().collect();
        for label in labels { if let Some(w) = app.get_webview_window(&label) { hide_one(&w); } }
    } else { hide_one(window); }
}
pub fn passthrough(window: &WebviewWindow) -> bool {
    window.app_handle().state::<OverlayState>().windows.lock().unwrap().get(window.label()).is_none_or(|w| w.passthrough)
}
fn set_passthrough(window: &WebviewWindow, passthrough: bool) {
    let changed = {
        let state = window.app_handle().state::<OverlayState>(); let mut windows = state.windows.lock().unwrap();
        windows.get_mut(window.label()).is_some_and(|w| { let changed = w.passthrough != passthrough; w.passthrough = passthrough; changed })
    };
    if changed { set_click_through(window, passthrough); }
}
/// Direct HWND flags: tao's stale hidden flag would otherwise hide a shown capsule on hover.
pub fn set_click_through(window: &WebviewWindow, through: bool) {
    let Some(h) = hwnd(window) else { return };
    unsafe {
        let ex = GetWindowLongPtrW(h, GWL_EXSTYLE);
        let next = if through { ex | (WS_EX_TRANSPARENT | WS_EX_LAYERED) as isize } else { ex & !(WS_EX_TRANSPARENT as isize) };
        if next != ex { SetWindowLongPtrW(h, GWL_EXSTYLE, next); }
    }
}
/// One cursor sampler serves every expanded card; it sleeps when no card is interactive.
pub fn start_hit_test(app: AppHandle) {
    std::thread::Builder::new().name("overlay-hit-test".into()).spawn(move || loop {
        let state = app.state::<OverlayState>();
        let windows: Vec<_> = state.windows.lock().unwrap().iter()
            .map(|(label, w)| (label.clone(), w.visible.then_some(w.interactive).flatten())).collect();
        let mut point = POINT { x: 0, y: 0 }; let cursor = unsafe { GetCursorPos(&mut point) } != 0;
        for (label, rect) in &windows {
            let Some(window) = app.get_webview_window(label) else { continue };
            let inside = rect.is_some_and(|r| cursor && match (window.inner_position(), window.scale_factor()) {
                (Ok(origin), Ok(scale)) => {
                    let x = (point.x - origin.x) as f64 / scale; let y = (point.y - origin.y) as f64 / scale;
                    x >= r.x && y >= r.y && x <= r.x + r.width && y <= r.y + r.height
                }
                _ => false,
            });
            set_passthrough(&window, !inside);
        }
        if windows.iter().any(|(_, rect)| rect.is_some()) { std::thread::sleep(Duration::from_millis(33)); }
        else {
            let guard = state.windows.lock().unwrap();
            drop(state.changed.wait_while(guard, |windows| !windows.values().any(|w| w.visible && w.interactive.is_some())).unwrap());
        }
    }).expect("overlay hit-test thread");
}

#[cfg(test)]
mod tests {
    use super::{apply_drag, mirror_label, targets, Display, Drag, PlacementState, Screen, Settings};
    fn main_screen() -> Screen { Screen { x: 0, y: 0, width: 3840, height: 2088, scale: 1.5 } }
    fn displays() -> Vec<Display> {
        vec![Display { name: "main".into(), screen: main_screen(), primary: true },
            Display { name: "laptop".into(), screen: Screen { x: 3840, y: 247, width: 2560, height: 1528, scale: 1.5 }, primary: false },
            Display { name: "portrait".into(), screen: Screen { x: -2160, y: -200, width: 2160, height: 3744, scale: 2.0 }, primary: false }]
    }
    #[test]
    fn nine_anchors_align_the_host_to_the_work_area() {
        for (anchor, expected) in [("top-left", (0, 0)), ("top-center", (1545, 0)), ("top-right", (3090, 0)),
            ("center-left", (0, 609)), ("center", (1545, 609)), ("center-right", (3090, 609)),
            ("bottom-left", (0, 1218)), ("bottom-center", (1545, 1218)), ("bottom-right", (3090, 1218))] {
            assert_eq!(main_screen().position(None, anchor), expected, "{anchor}");
        }
    }
    #[test]
    fn docking_and_destination_dpi_determine_center_before_move() {
        assert_eq!(Screen { width: 2560, height: 1528, ..main_screen() }.position(None, "top-center"), (905, 0));
        assert_eq!(Screen { scale: 2.0, ..main_screen() }.position(None, "top-center"), (1420, 0));
    }
    #[test]
    fn negative_origins_and_taskbars_are_respected() {
        assert_eq!(displays()[2].screen.position(None, "bottom-right"), (-1000, 2384));
        let screen = Screen { x: 72, y: 48, width: 3768, height: 2040, ..main_screen() };
        assert_eq!(screen.position(None, "top-center"), (1581, 48));
        assert_eq!(screen.position(Some((0, 0)), "bottom-right"), (72, 48));
    }
    #[test]
    fn positions_and_host_sizes_are_clamped_to_small_work_areas() {
        assert_eq!(main_screen().position(Some((-200, 1800)), "center"), (0, 1218));
        let small = Screen { width: 320, height: 480, ..main_screen() };
        assert_eq!(small.size(), tauri::PhysicalSize::new(320, 480));
        assert_eq!(small.position(Some((2000, -20)), "bottom-right"), (0, 0));
    }
    #[test]
    fn selected_display_replug_restores_identity_without_rewriting_settings() {
        let settings = Settings { monitor: Some("laptop".into()), ..Settings::default() };
        assert_eq!(targets(&displays()[..1], &settings)[0].monitor, "main");
        let shown = targets(&displays(), &settings);
        assert_eq!(shown.len(), 1); assert_eq!(shown[0].monitor, "laptop");
        assert_eq!(shown[0].screen.position(None, "top-center"), (4745, 247));
        assert_eq!(settings.monitor.as_deref(), Some("laptop"));
    }
    #[test]
    fn all_screens_have_one_controller_and_stable_mirror_identities() {
        let settings = Settings { all_monitors: true, ..Settings::default() };
        let shown = targets(&displays(), &settings);
        assert_eq!(shown.len(), 3); assert_eq!(shown[0].label, "overlay");
        assert_eq!(shown[1].label, mirror_label("laptop")); assert_eq!(shown[2].label, mirror_label("portrait"));
        let removed = vec![displays()[0].clone(), displays()[2].clone()];
        assert_eq!(targets(&removed, &settings)[1].label, shown[2].label);
        assert!(targets(&[], &settings).is_empty());
    }
    #[test]
    fn dragging_one_screen_preserves_other_screen_positions() {
        let mut settings = Settings { all_monitors: true, ..Settings::default() };
        settings.positions.insert("main".into(), (100, 80));
        apply_drag(&mut settings, &displays(), Drag { monitor: "laptop".into(), position: (3900, 300), cursor: None, generation: 0 });
        let shown = targets(&displays(), &settings);
        assert_eq!(shown[0].saved, Some((100, 80))); assert_eq!(shown[1].saved, Some((3900, 300)));
        assert_eq!(shown[2].saved, None);
    }
    #[test]
    fn single_drag_can_change_monitor_but_all_mode_stays_on_its_own_screen() {
        let mut settings = Settings::default();
        apply_drag(&mut settings, &displays(), Drag { monitor: "main".into(), position: (3900, 300), cursor: None, generation: 0 });
        assert_eq!(settings.monitor.as_deref(), Some("laptop"));
        assert_eq!(targets(&displays(), &settings)[0].saved, Some((3900, 300)));
        settings.all_monitors = true;
        apply_drag(&mut settings, &displays(), Drag { monitor: "main".into(), position: (3900, 300), cursor: None, generation: 0 });
        assert_eq!(settings.positions["main"], (3090, 300));
        assert_eq!(settings.positions["laptop"], (3900, 300));
    }
    #[test]
    fn the_card_under_the_cursor_picks_the_screen_not_the_hosts_corner() {
        // The host's top edge lies above the staggered laptop (or off-screen); the card, and
        // the cursor on it, are on the laptop.
        for (anchor, position, cursor) in [("top-center", (4000, 229), (4375, 265)),
            ("center", (3940, -135), (4315, 300)), ("bottom-center", (3940, -65), (4315, 740))] {
            let mut settings = Settings { anchor: anchor.into(), ..Settings::default() };
            apply_drag(&mut settings, &displays(), Drag { monitor: "main".into(), position, cursor: Some(cursor), generation: 0 });
            assert_eq!(settings.monitor.as_deref(), Some("laptop"), "{anchor}");
            assert_eq!(settings.positions["laptop"], (position.0, 247), "{anchor}: clamped to the laptop's work area");
            assert!(!settings.positions.contains_key("main"), "{anchor}");
        }
        // The host's corner is on the laptop, the card under the cursor still on main.
        let mut settings = Settings::default();
        apply_drag(&mut settings, &displays(), Drag { monitor: "main".into(), position: (3500, 300), cursor: Some((3700, 330)), generation: 0 });
        assert_eq!(settings.positions["main"], (3090, 300));
        assert!(!settings.positions.contains_key("laptop"));
    }
    #[test]
    fn a_drag_on_the_primary_screen_keeps_following_the_primary_screen() {
        let mut settings = Settings::default();
        apply_drag(&mut settings, &displays(), Drag { monitor: "main".into(), position: (100, 80), cursor: Some((475, 110)), generation: 0 });
        assert_eq!(settings.monitor, None);
        assert_eq!(targets(&displays(), &settings)[0].saved, Some((100, 80)));
        let mut swapped = displays();
        swapped[0].primary = false;
        swapped[1].primary = true;
        assert_eq!(targets(&swapped, &settings)[0].monitor, "laptop", "follows a new primary screen");
        // A legacy position on another screen is pinned there, or the capsule would jump to main.
        let mut settings = Settings { legacy: Some((3900, 300)), ..Settings::default() };
        apply_drag(&mut settings, &displays(), Drag { monitor: "laptop".into(), position: (3950, 320), cursor: Some((4325, 350)), generation: 0 });
        assert_eq!(settings.monitor.as_deref(), Some("laptop"));
        assert_eq!(targets(&displays(), &settings)[0].monitor, "laptop");
        // A chosen screen stays chosen.
        let mut settings = Settings { monitor: Some("main".into()), ..Settings::default() };
        apply_drag(&mut settings, &displays(), Drag { monitor: "main".into(), position: (100, 80), cursor: Some((475, 110)), generation: 0 });
        assert_eq!(settings.monitor.as_deref(), Some("main"));
    }
    #[test]
    fn legacy_position_is_used_until_new_per_screen_positions_exist() {
        let mut settings = Settings { legacy: Some((3900, 300)), ..Settings::default() };
        assert_eq!(targets(&displays(), &settings)[0].monitor, "laptop");
        settings.positions.insert("main".into(), (100, 80));
        assert_eq!(targets(&displays(), &settings)[0].monitor, "main");
    }
    #[test]
    fn invalid_anchor_and_old_settings_have_safe_defaults() {
        let settings = Settings::from_prefs(&serde_json::json!({"overlay":{"anchor":"bad"}}));
        assert_eq!(settings.anchor, "top-center"); assert!(!settings.all_monitors); assert!(settings.enabled);
    }
    #[test]
    fn reset_or_anchor_change_wins_over_a_queued_or_still_running_drag() {
        let mut p = PlacementState::default();
        p.update(Settings::default());
        let started = p.generation;
        p.completed.push(Drag { monitor: "main".into(), position: (100, 80), cursor: None, generation: started });
        p.update(Settings { anchor: "bottom-right".into(), ..Settings::default() });
        assert!(!p.finish_drags(&displays()));
        p.completed.push(Drag { monitor: "main".into(), position: (100, 80), cursor: None, generation: started });
        assert!(!p.finish_drags(&displays()));
        assert!(p.settings.positions.is_empty());
        assert_eq!(p.settings.anchor, "bottom-right");
    }
    #[test]
    fn a_drag_save_echo_does_not_invalidate_another_current_drag() {
        let mut p = PlacementState::default();
        p.update(Settings::default());
        p.completed.push(Drag { monitor: "main".into(), position: (100, 80), cursor: None, generation: p.generation });
        assert!(p.finish_drags(&displays()));
        let generation = p.generation;
        assert!(!p.update(p.settings.clone()));
        assert_eq!(p.generation, generation);
    }
    #[test]
    fn explicit_reset_invalidates_a_drag_even_if_saved_preferences_were_already_empty() {
        let mut p = PlacementState::default();
        p.update(Settings::default());
        p.completed.push(Drag { monitor: "main".into(), position: (100, 80), cursor: None, generation: p.generation });
        assert!(!p.update(Settings::default()));
        p.reset_drags();
        assert!(!p.finish_drags(&displays()));
        assert!(p.settings.positions.is_empty());
    }
    #[test]
    fn older_save_echo_cannot_erase_another_monitors_newer_drag() {
        let mut p = PlacementState::default();
        p.update(Settings { all_monitors: true, ..Settings::default() });
        p.settings.positions.insert("main".into(), (100, 80));
        assert!(p.queue_save());
        let (a, generation) = p.next_save().unwrap();
        p.settings.positions.insert("laptop".into(), (3900, 300));
        assert!(!p.queue_save());
        assert!(!p.update(a.clone()), "late A echo is not an external layout decision");
        assert_eq!(p.settings.positions.len(), 2);
        assert_eq!(p.generation, generation);
        p.saved(&a, generation, true);
        let (ab, _) = p.next_save().unwrap();
        assert_eq!(ab.positions.len(), 2, "the serialized next request contains both screens");
    }
    #[test]
    fn reset_during_an_inflight_save_is_reapplied_after_its_old_echo() {
        let mut p = PlacementState::default();
        p.update(Settings::default());
        p.settings.positions.insert("main".into(), (100, 80));
        p.queue_save();
        let (a, generation) = p.next_save().unwrap();
        p.update(Settings { anchor: "bottom-right".into(), ..Settings::default() });
        let mut echo = a.clone();
        echo.anchor = "bottom-right".into();
        assert!(!p.update(echo));
        assert!(p.settings.positions.is_empty());
        p.saved(&a, generation, true);
        let (reset, _) = p.next_save().unwrap();
        assert!(reset.positions.is_empty());
        assert_eq!(reset.anchor, "bottom-right");
    }
    #[test]
    fn an_unrelated_snapshot_from_before_an_inflight_save_keeps_the_drag() {
        let mut p = PlacementState::default();
        let mut before = Settings::default();
        before.positions.insert("main".into(), (100, 80));
        p.update(before.clone());
        p.completed.push(Drag { monitor: "main".into(), position: (900, 300), cursor: None, generation: p.generation });
        assert!(p.finish_drags(&displays()));
        assert!(p.queue_save());
        let (sent, generation) = p.next_save().unwrap();
        // A second drag is queued behind the request.
        p.completed.push(Drag { monitor: "main".into(), position: (1200, 400), cursor: None, generation });
        assert!(p.finish_drags(&displays()));
        assert!(!p.queue_save());
        // A theme change or zoom step committed just before the save: its 'prefs' still
        // carries the old position.
        assert!(!p.update(before.clone()), "the capsule does not snap back");
        assert_eq!(p.settings.positions["main"], (1200, 400));
        assert_eq!(p.generation, generation);
        assert!(!p.update(sent.clone()), "the request's own echo");
        p.saved(&sent, generation, true);
        let (next, _) = p.next_save().expect("the queued drag is still saved");
        assert_eq!(next.positions["main"], (1200, 400), "not the old position");
        p.saved(&next, generation, true);
        assert!(p.next_save().is_none());
    }
    #[test]
    fn reset_during_an_inflight_save_wins_even_when_it_restores_the_old_empty_state() {
        let mut p = PlacementState::default();
        p.update(Settings::default());
        p.completed.push(Drag { monitor: "main".into(), position: (900, 300), cursor: None, generation: p.generation });
        assert!(p.finish_drags(&displays()));
        p.queue_save();
        let (sent, generation) = p.next_save().unwrap();
        // 恢复预设位置: the core's answer equals the state before the drag.
        p.update(Settings::default());
        p.reset_placement();
        assert!(p.settings.positions.is_empty());
        assert!(!p.update(sent.clone()));
        p.saved(&sent, generation, true);
        let (reset, _) = p.next_save().expect("the reset is saved after the drag's request");
        assert!(reset.positions.is_empty() && p.settings.positions.is_empty());
    }
    #[test]
    fn an_old_complete_save_snapshot_cannot_restore_the_previous_anchor() {
        let mut p = PlacementState::default();
        p.update(Settings::default());
        p.settings.positions.insert("main".into(), (100, 80));
        p.queue_save();
        let (a, generation) = p.next_save().unwrap();
        p.update(Settings { anchor: "bottom-right".into(), ..Settings::default() });
        assert!(!p.update(a.clone()), "the old response must not restore top-center");
        assert_eq!(p.settings.anchor, "bottom-right");
        assert!(p.settings.positions.is_empty());
        p.saved(&a, generation, true);
        assert_eq!(p.next_save().unwrap().0.anchor, "bottom-right");
    }
}
