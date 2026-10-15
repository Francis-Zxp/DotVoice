//! Live shell icons.
//!
//! DotVoice's own shortcuts (Desktop, Start menu, taskbar pins) light the same
//! dot as the tray — the dot of the d in the mark: green = ready, red = paused /
//! error, white (unlit) = not running. There is no overlay badge, so each place
//! shows exactly one light. (A folder can be marked plain; none is today.) Only `.lnk` files that launch *this* DotVoice.exe and still show
//! DotVoice's own icon are touched; a shortcut the user re-iconed is left alone.
//!
//! Mechanism: the Shell's own `ShellLink` COM object (IShellLinkW +
//! IPersistFile) called in-process on a dedicated STA worker thread. A hidden
//! PowerShell / WScript.Shell child would cost 0.5–1.5 s and ~60 MB per
//! update, pass through AMSI script scanning and execution policy, and is
//! blocked outright on some managed PCs; the direct call takes a few
//! milliseconds per shortcut and every failure is a plain HRESULT we ignore.
//!
//! Each state has its own content-hashed icon file in
//! %LOCALAPPDATA%\DotVoice\shell-icons, so Explorer's icon cache (keyed by
//! path) can never show a stale picture, and the shell is told about each
//! rewritten shortcut with SHCNE_UPDATEITEM. That refreshes the desktop and
//! Start menu, but the taskbar keeps a pinned button's picture in its own cache
//! (the pin stayed red after connecting), so when a *taskbar pin* was rewritten
//! one SHCNE_ASSOCCHANGED follows. It only happens on ready/paused/exit changes,
//! never per frame.
//!
//! Also here: [`small_icon`], which hands the tray the ICO entry drawn for the
//! current small-icon size instead of letting Windows shrink the 64 px one.

use serde_json::Value;
use std::ffi::{c_void, OsString};
use std::os::windows::ffi::{OsStrExt, OsStringExt};
use std::path::{Path, PathBuf};
use std::ptr::{null, null_mut};
use std::sync::mpsc::{self, RecvTimeoutError, Sender};
use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, Instant};
use tauri::image::Image;
use tauri::{AppHandle, Manager};
use windows_sys::core::{GUID, HRESULT, PCWSTR, PWSTR};
use windows_sys::Win32::System::Com::{
    CoCreateInstance, CoInitializeEx, CoTaskMemFree, CoUninitialize, CLSCTX_INPROC_SERVER,
    COINIT_APARTMENTTHREADED, COINIT_DISABLE_OLE1DDE,
};
use windows_sys::Win32::UI::Shell::{
    SHChangeNotify, SHGetKnownFolderPath, ShellLink, FOLDERID_Desktop, FOLDERID_LocalAppData,
    FOLDERID_Programs, FOLDERID_RoamingAppData, SHCNE_ASSOCCHANGED, SHCNE_UPDATEITEM, SHCNF_IDLIST, SHCNF_PATHW,
};
use windows_sys::Win32::UI::WindowsAndMessaging::{GetSystemMetrics, SM_CXICON, SM_CXSMICON};

const READY_ICO: &[u8] = include_bytes!("../icons/shortcut-online.ico");
const PAUSED_ICO: &[u8] = include_bytes!("../icons/shortcut-offline.ico");
const IDLE_ICO: &[u8] = include_bytes!("../icons/shortcut-idle.ico");

/// Quiet time before a state is written: short flaps (a reconnect, a quick
/// pause/resume) never touch the disk or make the desktop repaint.
const DEBOUNCE: Duration = Duration::from_millis(1500);
/// Longest the exit path waits for the "not running" icon to be written.
const EXIT_WAIT: Duration = Duration::from_millis(1500);

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Live {
    Ready,
    Paused,
    Exited,
}

impl Live {
    /// Same split as the tray dot: an armed session is ready, anything else is paused.
    pub fn from_phase(phase: &str) -> Self {
        if matches!(phase, "idle" | "starting" | "recording" | "processing") {
            Live::Ready
        } else {
            Live::Paused
        }
    }

    fn icon(self) -> (&'static str, &'static [u8]) {
        match self {
            Live::Ready => ("ready", READY_ICO),
            Live::Paused => ("paused", PAUSED_ICO),
            Live::Exited => ("idle", IDLE_ICO),
        }
    }
}

enum Msg {
    Phase(Live),
    Enabled(bool),
    Exit,
}

/// Managed state: the worker's inbox and its "finished" flag.
pub struct LiveShortcuts {
    tx: Mutex<Option<Sender<Msg>>>,
    done: Arc<(Mutex<bool>, Condvar)>,
}

/// Start the worker. In `--smoke` mode nothing ever touches real shortcuts.
pub fn init(app: &AppHandle, smoke: bool) {
    let done = Arc::new((Mutex::new(smoke), Condvar::new()));
    let tx = if smoke { None } else { spawn_worker(Arc::clone(&done)) };
    if tx.is_none() {
        *done.0.lock().unwrap() = true;
    }
    app.manage(LiveShortcuts { tx: Mutex::new(tx), done });
}

fn send(app: &AppHandle, msg: Msg) {
    if let Some(state) = app.try_state::<LiveShortcuts>() {
        if let Some(tx) = state.tx.lock().unwrap().as_ref() {
            let _ = tx.send(msg);
        }
    }
}

/// A session phase change (called from the tray, which already de-duplicates).
pub fn update_phase(app: &AppHandle, phase: &str) {
    send(app, Msg::Phase(Live::from_phase(phase)));
}

/// `prefs.appearance.live_shortcut` (default on) from the ready snapshot or a prefs event.
pub fn apply_prefs(app: &AppHandle, prefs: &Value) {
    let enabled = prefs
        .get("appearance")
        .and_then(|a| a.get("live_shortcut"))
        .and_then(Value::as_bool)
        .unwrap_or(true);
    send(app, Msg::Enabled(enabled));
}

/// The app is quitting: show "not running" now, without waiting (the core's
/// own shutdown would otherwise flash the paused icon first).
pub fn quitting(app: &AppHandle) {
    send(app, Msg::Exit);
}

/// Final exit: make sure the "not running" icon is written, bounded wait.
pub fn exit(app: &AppHandle) {
    let Some(state) = app.try_state::<LiveShortcuts>() else { return };
    if let Some(tx) = state.tx.lock().unwrap().take() {
        let _ = tx.send(Msg::Exit);
    }
    let (lock, cvar) = &*state.done;
    let guard = lock.lock().unwrap();
    let _ = cvar.wait_timeout_while(guard, EXIT_WAIT, |finished| !*finished);
}

fn spawn_worker(done: Arc<(Mutex<bool>, Condvar)>) -> Option<Sender<Msg>> {
    let exe = std::env::current_exe().ok()?;
    let (tx, rx) = mpsc::channel::<Msg>();
    let thread = std::thread::Builder::new().name("dotvoice-live-shortcuts".into()).spawn(move || {
        let _apartment = Apartment::enter();
        let mut worker = Worker::new(exe, default_icon_dir(), default_folders());
        let mut enabled = None;
        let mut desired = None;
        let mut deadline: Option<Instant> = None;
        loop {
            let msg = match deadline {
                Some(at) => rx.recv_timeout(at.saturating_duration_since(Instant::now())),
                None => rx.recv().map_err(|_| RecvTimeoutError::Disconnected),
            };
            match msg {
                Ok(Msg::Phase(live)) => {
                    if desired != Some(live) {
                        desired = Some(live);
                        deadline = Some(Instant::now() + DEBOUNCE);
                    }
                }
                Ok(Msg::Enabled(on)) => {
                    if enabled != Some(on) {
                        enabled = Some(on);
                        deadline = Some(Instant::now() + DEBOUNCE);
                    }
                }
                Err(RecvTimeoutError::Timeout) => {
                    deadline = None;
                    worker.settle(enabled, desired);
                }
                Ok(Msg::Exit) | Err(RecvTimeoutError::Disconnected) => {
                    worker.finish();
                    break;
                }
            }
        }
        let (lock, cvar) = &*done;
        *lock.lock().unwrap() = true;
        cvar.notify_all();
    });
    thread.ok().map(|_| tx)
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Target {
    Live(Live),
    /// Back to the exe's own icon, exactly as the installer created the shortcut.
    Stock,
}

struct Worker {
    exe: PathBuf,
    exe_key: String,
    icon_dir: Option<PathBuf>,
    /// Folders to scan and how many sub-folder levels to enter.
    folders: Vec<(PathBuf, u8, bool)>, // (folder, depth, live dot)
    applied: Option<Target>,
    cleaned: bool,
}

impl Worker {
    fn new(exe: PathBuf, icon_dir: Option<PathBuf>, folders: Vec<(PathBuf, u8, bool)>) -> Self {
        let exe_key = path_key(&exe);
        Worker { exe, exe_key, icon_dir, folders, applied: None, cleaned: false }
    }

    /// Apply the settled state once the debounce has passed.
    fn settle(&mut self, enabled: Option<bool>, desired: Option<Live>) {
        let target = match (enabled, desired) {
            (Some(true), Some(live)) => Target::Live(live),
            // Switched off (or off since a previous run): hand back the stock icon.
            (Some(false), _) => Target::Stock,
            _ => return,
        };
        if self.applied != Some(target) {
            self.apply(target);
            self.applied = Some(target);
        }
    }

    /// App exit: shortcuts we made live now say "not running".
    fn finish(&mut self) {
        if matches!(self.applied, Some(Target::Live(_))) {
            self.apply(Target::Live(Live::Exited));
            self.applied = Some(Target::Live(Live::Exited));
        }
    }

    fn apply(&mut self, target: Target) -> usize {
        let icon = match target {
            Target::Live(live) => {
                // All three are written up front so exit never waits on a disk write.
                let mut wanted = None;
                for state in [Live::Ready, Live::Paused, Live::Exited] {
                    let path = self.icon_file(state);
                    if state == live {
                        wanted = path;
                    }
                }
                match wanted {
                    Some(path) => path,
                    None => return 0,
                }
            }
            Target::Stock => self.exe.clone(),
        };
        let mut changed = 0;
        let mut pin_changed = false;
        for (lnk, live) in self.candidates() {
            let icon = if live { &icon } else { &self.exe };
            if update_link(&lnk, &self.exe_key, self.icon_dir.as_deref(), icon) == Some(true) {
                notify_shell(&lnk);
                changed += 1;
                pin_changed |= is_taskbar_pin(&lnk);
            }
        }
        if pin_changed {
            unsafe { SHChangeNotify(SHCNE_ASSOCCHANGED as i32, SHCNF_IDLIST, null(), null()) };
        }
        if matches!(target, Target::Live(_)) && !self.cleaned {
            self.cleaned = true;
            self.remove_stale_icons();
        }
        changed
    }

    fn icon_file(&self, live: Live) -> Option<PathBuf> {
        let dir = self.icon_dir.as_ref()?;
        let (name, bytes) = live.icon();
        let path = dir.join(icon_file_name(name, bytes));
        if std::fs::read(&path).ok().as_deref() != Some(bytes) {
            std::fs::create_dir_all(dir).ok()?;
            let temp = path.with_extension("tmp");
            std::fs::write(&temp, bytes).ok()?;
            std::fs::rename(&temp, &path).ok()?;
        }
        Some(path)
    }

    /// Icons from an older DotVoice build (different content hash).
    fn remove_stale_icons(&self) {
        let Some(dir) = &self.icon_dir else { return };
        let keep: Vec<String> = [Live::Ready, Live::Paused, Live::Exited]
            .iter()
            .map(|l| {
                let (name, bytes) = l.icon();
                icon_file_name(name, bytes)
            })
            .collect();
        let Ok(entries) = std::fs::read_dir(dir) else { return };
        for entry in entries.flatten() {
            let name = entry.file_name().to_string_lossy().to_string();
            if name.starts_with("dotvoice-") && name.ends_with(".ico") && !keep.contains(&name) {
                let _ = std::fs::remove_file(entry.path());
            }
        }
    }

    fn candidates(&self) -> Vec<(PathBuf, bool)> {
        let mut out = Vec::new();
        for (folder, depth, live) in &self.folders {
            let mut links = Vec::new();
            collect_links(folder, *depth, &mut links);
            out.extend(links.into_iter().map(|lnk| (lnk, *live)));
        }
        out
    }
}

fn icon_file_name(name: &str, bytes: &[u8]) -> String {
    // FNV-1a: a new build with new art gets a new path, so no stale icon cache.
    let hash = bytes.iter().fold(0x811c_9dc5u32, |h, b| (h ^ u32::from(*b)).wrapping_mul(0x0100_0193));
    format!("dotvoice-{name}-{hash:08x}.ico")
}

fn collect_links(folder: &Path, depth: u8, out: &mut Vec<PathBuf>) {
    use std::os::windows::fs::MetadataExt;
    // Cloud placeholders (OneDrive "online-only") would be downloaded just to be read.
    const SKIP: u32 = 0x1000 /* OFFLINE */ | 0x40_0000 /* RECALL_ON_DATA_ACCESS */ | 0x4_0000 /* RECALL_ON_OPEN */;
    let Ok(entries) = std::fs::read_dir(folder) else { return };
    for entry in entries.flatten() {
        let Ok(meta) = entry.metadata() else { continue };
        let path = entry.path();
        if meta.is_dir() {
            if depth > 0 {
                collect_links(&path, depth - 1, out);
            }
        } else if meta.file_attributes() & SKIP == 0
            && path.extension().is_some_and(|e| e.eq_ignore_ascii_case("lnk"))
        {
            out.push(path);
        }
    }
}

/// Re-icon one shortcut if it is ours. Some(true) = rewritten.
fn update_link(lnk: &Path, exe_key: &str, icon_dir: Option<&Path>, icon: &Path) -> Option<bool> {
    let link = Link::load(lnk)?;
    let target = link.target()?;
    // Cheap name check first: never touch the disk (or a sleeping network share) for other apps.
    let exe_name = Path::new(exe_key).file_name().map(|n| n.to_string_lossy().to_string());
    let target_name = target.file_name().map(|n| n.to_string_lossy().to_lowercase());
    if target_name != exe_name || path_key(&target) != exe_key {
        return Some(false);
    }
    let (current, index) = link.icon_location()?;
    let current = expand_env(&current);
    let current_key = if current.is_empty() { String::new() } else { path_key(Path::new(&current)) };
    let ours = current.is_empty()
        || current_key == exe_key
        || icon_dir.is_some_and(|dir| current_key.starts_with(&(path_key(dir) + "\\")));
    if !ours {
        return Some(false); // the user picked their own icon
    }
    let wanted = path_key(icon);
    // Already showing it — or showing the exe icon implicitly (no icon set) when the exe is wanted.
    if (current_key == wanted && index == 0) || (current.is_empty() && wanted == exe_key) {
        return Some(false);
    }
    if !link.set_icon_location(icon, 0) || !link.save(lnk) {
        return None;
    }
    Some(true)
}

fn is_taskbar_pin(lnk: &Path) -> bool {
    lnk.to_string_lossy().to_lowercase().contains("\\user pinned\\taskbar\\")
}

fn notify_shell(lnk: &Path) {
    let wide = wide(lnk.as_os_str());
    unsafe { SHChangeNotify(SHCNE_UPDATEITEM as i32, SHCNF_PATHW, wide.as_ptr() as *const c_void, null()) };
}

/// Lower-cased absolute path for comparisons (resolves 8.3 names and links when the file exists).
fn path_key(path: &Path) -> String {
    let resolved = std::fs::canonicalize(path).unwrap_or_else(|_| path.to_path_buf());
    let text = resolved.to_string_lossy().replace('/', "\\");
    let text = text.strip_prefix("\\\\?\\UNC\\").map(|t| format!("\\\\{t}")).unwrap_or(text);
    let text = text.strip_prefix("\\\\?\\").map(str::to_string).unwrap_or(text);
    text.trim_end_matches('\\').to_lowercase()
}

/// `%VAR%` expansion for icon paths stored in environment form.
fn expand_env(text: &str) -> String {
    let mut out = String::new();
    let mut rest = text;
    while let Some(start) = rest.find('%') {
        let Some(len) = rest[start + 1..].find('%') else { break };
        let name = &rest[start + 1..start + 1 + len];
        out.push_str(&rest[..start]);
        match std::env::var(name) {
            Ok(value) if !name.is_empty() => out.push_str(&value),
            _ => out.push_str(&rest[start..start + len + 2]),
        }
        rest = &rest[start + len + 2..];
    }
    out.push_str(rest);
    out
}

fn wide(text: &std::ffi::OsStr) -> Vec<u16> {
    text.encode_wide().chain(std::iter::once(0)).collect()
}

fn from_wide(buffer: &[u16]) -> String {
    let len = buffer.iter().position(|c| *c == 0).unwrap_or(buffer.len());
    OsString::from_wide(&buffer[..len]).to_string_lossy().to_string()
}

fn known_folder(id: &GUID) -> Option<PathBuf> {
    unsafe {
        let mut raw: PWSTR = null_mut();
        let hr = SHGetKnownFolderPath(id, 0, null_mut(), &mut raw);
        let path = if hr >= 0 && !raw.is_null() {
            let len = (0..).take_while(|&i| *raw.add(i) != 0).count();
            Some(PathBuf::from(OsString::from_wide(std::slice::from_raw_parts(raw, len))))
        } else {
            None
        };
        if !raw.is_null() {
            CoTaskMemFree(raw as *const c_void);
        }
        path
    }
}

fn default_icon_dir() -> Option<PathBuf> {
    known_folder(&FOLDERID_LocalAppData)
        .or_else(|| std::env::var_os("LOCALAPPDATA").map(PathBuf::from))
        .map(|base| base.join("DotVoice").join("shell-icons"))
}

fn default_folders() -> Vec<(PathBuf, u8, bool)> {
    let mut folders = Vec::new();
    if let Some(desktop) = known_folder(&FOLDERID_Desktop) {
        folders.push((desktop, 0, true));
    }
    if let Some(programs) = known_folder(&FOLDERID_Programs) {
        folders.push((programs, 1, true));
    }
    if let Some(roaming) = known_folder(&FOLDERID_RoamingAppData) {
        let pinned = roaming.join(r"Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar");
        folders.push((pinned, 0, true));
    }
    folders
}

// ── Minimal COM: ShellLink through IShellLinkW + IPersistFile ─────────────

const IID_ISHELLLINKW: GUID = GUID::from_u128(0x000214f9_0000_0000_c000_000000000046);
const IID_IPERSISTFILE: GUID = GUID::from_u128(0x0000010b_0000_0000_c000_000000000046);
const STGM_READ: u32 = 0;

#[repr(C)]
struct IUnknownVtbl {
    query_interface: unsafe extern "system" fn(*mut c_void, *const GUID, *mut *mut c_void) -> HRESULT,
    add_ref: unsafe extern "system" fn(*mut c_void) -> u32,
    release: unsafe extern "system" fn(*mut c_void) -> u32,
}

/// IShellLinkW, in vtable order; unused slots are kept as raw pointers.
#[repr(C)]
struct IShellLinkWVtbl {
    base: IUnknownVtbl,
    get_path: unsafe extern "system" fn(*mut c_void, PWSTR, i32, *mut c_void, u32) -> HRESULT,
    _get_id_list: usize,
    _set_id_list: usize,
    _get_description: usize,
    _set_description: usize,
    _get_working_directory: usize,
    _set_working_directory: usize,
    _get_arguments: usize,
    _set_arguments: usize,
    _get_hotkey: usize,
    _set_hotkey: usize,
    _get_show_cmd: usize,
    _set_show_cmd: usize,
    get_icon_location: unsafe extern "system" fn(*mut c_void, PWSTR, i32, *mut i32) -> HRESULT,
    set_icon_location: unsafe extern "system" fn(*mut c_void, PCWSTR, i32) -> HRESULT,
    _set_relative_path: usize,
    _resolve: usize,
    set_path: unsafe extern "system" fn(*mut c_void, PCWSTR) -> HRESULT,
}

/// IPersistFile (after IUnknown and IPersist::GetClassID).
#[repr(C)]
struct IPersistFileVtbl {
    base: IUnknownVtbl,
    _get_class_id: usize,
    _is_dirty: usize,
    load: unsafe extern "system" fn(*mut c_void, PCWSTR, u32) -> HRESULT,
    save: unsafe extern "system" fn(*mut c_void, PCWSTR, i32) -> HRESULT,
    _save_completed: usize,
    _get_cur_file: usize,
}

struct Com(*mut c_void);

impl Com {
    unsafe fn vtbl<T>(&self) -> &T {
        &**(self.0 as *const *const T)
    }
}

impl Drop for Com {
    fn drop(&mut self) {
        if !self.0.is_null() {
            unsafe { (self.vtbl::<IUnknownVtbl>().release)(self.0) };
        }
    }
}

/// COM apartment for the current thread (balanced CoUninitialize on drop).
struct Apartment(bool);

impl Apartment {
    fn enter() -> Self {
        let hr = unsafe { CoInitializeEx(null(), (COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE) as u32) };
        Apartment(hr >= 0)
    }
}

impl Drop for Apartment {
    fn drop(&mut self) {
        if self.0 {
            unsafe { CoUninitialize() };
        }
    }
}

struct Link {
    link: Com,
    file: Com,
}

impl Link {
    fn new() -> Option<Self> {
        unsafe {
            let mut raw = null_mut();
            if CoCreateInstance(&ShellLink, null_mut(), CLSCTX_INPROC_SERVER, &IID_ISHELLLINKW, &mut raw) < 0 || raw.is_null() {
                return None;
            }
            let link = Com(raw);
            let mut file = null_mut();
            if (link.vtbl::<IUnknownVtbl>().query_interface)(link.0, &IID_IPERSISTFILE, &mut file) < 0 || file.is_null() {
                return None;
            }
            Some(Link { link, file: Com(file) })
        }
    }

    fn load(path: &Path) -> Option<Self> {
        let link = Self::new()?;
        let path = wide(path.as_os_str());
        let hr = unsafe { (link.file.vtbl::<IPersistFileVtbl>().load)(link.file.0, path.as_ptr(), STGM_READ) };
        (hr >= 0).then_some(link)
    }

    fn target(&self) -> Option<PathBuf> {
        let mut buffer = vec![0u16; 2048];
        let hr = unsafe {
            (self.link.vtbl::<IShellLinkWVtbl>().get_path)(self.link.0, buffer.as_mut_ptr(), buffer.len() as i32, null_mut(), 0)
        };
        let text = from_wide(&buffer);
        (hr >= 0 && !text.is_empty()).then(|| PathBuf::from(text))
    }

    fn icon_location(&self) -> Option<(String, i32)> {
        let mut buffer = vec![0u16; 2048];
        let mut index = 0i32;
        let hr = unsafe {
            (self.link.vtbl::<IShellLinkWVtbl>().get_icon_location)(self.link.0, buffer.as_mut_ptr(), buffer.len() as i32, &mut index)
        };
        (hr >= 0).then(|| (from_wide(&buffer), index))
    }

    fn set_icon_location(&self, icon: &Path, index: i32) -> bool {
        let icon = wide(icon.as_os_str());
        unsafe { (self.link.vtbl::<IShellLinkWVtbl>().set_icon_location)(self.link.0, icon.as_ptr(), index) >= 0 }
    }

    #[cfg(test)]
    fn set_target(&self, target: &Path) -> bool {
        let target = wide(target.as_os_str());
        unsafe { (self.link.vtbl::<IShellLinkWVtbl>().set_path)(self.link.0, target.as_ptr()) >= 0 }
    }

    fn save(&self, path: &Path) -> bool {
        let path = wide(path.as_os_str());
        unsafe { (self.file.vtbl::<IPersistFileVtbl>().save)(self.file.0, path.as_ptr(), 1) >= 0 }
    }
}

// ── Tray: DPI-exact entry of a multi-size ICO ─────────────────────────────

/// The ICO entry drawn for the current small-icon size (16 px at 100 %, 20 at
/// 125 %, 24 at 150 %, 32 at 200 %). `Image::from_bytes` alone always decodes
/// the largest entry, which Windows then shrinks into a blurry tray icon.
/// The ICO entry for the window (taskbar / Alt-Tab) icon size.
pub fn big_icon(ico: &[u8]) -> tauri::Result<Image<'static>> {
    let want = unsafe { GetSystemMetrics(SM_CXICON) }.clamp(16, 256) as u32;
    match ico_entry(ico, want) {
        Some(single) => Image::from_bytes(&single).or_else(|_| Image::from_bytes(ico)),
        None => Image::from_bytes(ico),
    }
}

pub fn small_icon(ico: &[u8]) -> tauri::Result<Image<'static>> {
    let want = unsafe { GetSystemMetrics(SM_CXSMICON) }.clamp(16, 256) as u32;
    match ico_entry(ico, want) {
        Some(single) => Image::from_bytes(&single).or_else(|_| Image::from_bytes(ico)),
        None => Image::from_bytes(ico),
    }
}

/// The smallest entry at least `want` px (else the largest), as a one-entry ICO.
fn ico_entry(ico: &[u8], want: u32) -> Option<Vec<u8>> {
    if ico.len() < 6 || ico[0..4] != [0, 0, 1, 0] {
        return None;
    }
    let count = usize::from(u16::from_le_bytes([ico[4], ico[5]]));
    let entries: Vec<(u32, usize)> = (0..count)
        .filter_map(|i| {
            let e = ico.get(6 + i * 16..6 + i * 16 + 16)?;
            Some((if e[0] == 0 { 256 } else { u32::from(e[0]) }, 6 + i * 16))
        })
        .collect();
    let (_, at) = entries
        .iter()
        .filter(|(size, _)| *size >= want)
        .min_by_key(|(size, _)| *size)
        .or_else(|| entries.iter().max_by_key(|(size, _)| *size))?;
    let e = &ico[*at..*at + 16];
    let len = u32::from_le_bytes([e[8], e[9], e[10], e[11]]) as usize;
    let offset = u32::from_le_bytes([e[12], e[13], e[14], e[15]]) as usize;
    let data = ico.get(offset..offset.checked_add(len)?)?;
    let mut single = Vec::with_capacity(22 + len);
    single.extend_from_slice(&[0, 0, 1, 0, 1, 0]);
    single.extend_from_slice(&e[0..8]);
    single.extend_from_slice(&(len as u32).to_le_bytes());
    single.extend_from_slice(&22u32.to_le_bytes());
    single.extend_from_slice(data);
    Some(single)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ico_sizes(ico: &[u8]) -> Vec<u32> {
        let count = usize::from(u16::from_le_bytes([ico[4], ico[5]]));
        (0..count).map(|i| match ico[6 + i * 16] { 0 => 256, s => u32::from(s) }).collect()
    }

    #[test]
    fn phases_map_like_the_tray_dot() {
        for phase in ["idle", "starting", "recording", "processing"] {
            assert_eq!(Live::from_phase(phase), Live::Ready);
        }
        for phase in ["disconnected", "error", ""] {
            assert_eq!(Live::from_phase(phase), Live::Paused);
        }
    }

    #[test]
    fn shortcut_icons_cover_every_shell_size() {
        for ico in [READY_ICO, PAUSED_ICO, IDLE_ICO] {
            assert_eq!(ico_sizes(ico), vec![16, 20, 24, 32, 40, 48, 64, 128, 256]);
        }
        let names: Vec<_> = [Live::Ready, Live::Paused, Live::Exited]
            .iter()
            .map(|l| {
                let (name, bytes) = l.icon();
                icon_file_name(name, bytes)
            })
            .collect();
        assert!(names[0].starts_with("dotvoice-ready-") && names[0].ends_with(".ico"));
        assert_ne!(names[0], names[1]);
    }

    #[test]
    fn tray_gets_the_entry_for_its_size() {
        let tray = include_bytes!("../icons/status-online.ico");
        for (want, got) in [(16, 16), (20, 20), (24, 24), (30, 32), (32, 32), (48, 48), (96, 64)] {
            let single = ico_entry(tray, want).unwrap();
            assert_eq!(ico_sizes(&single), vec![got], "want {want}");
            assert!(Image::from_bytes(&single).is_ok());
        }
    }

    #[test]
    fn expands_environment_icon_paths() {
        std::env::set_var("DOTVOICE_TEST_ROOT", r"C:\Root");
        assert_eq!(expand_env(r"%DOTVOICE_TEST_ROOT%\a.ico"), r"C:\Root\a.ico");
        assert_eq!(expand_env(r"C:\100%\x"), r"C:\100%\x");
    }

    /// Creates real .lnk files in a scratch folder (never the user's own) and
    /// drives the worker through ready → paused → off → ready → exit.
    #[test]
    fn rewrites_only_dotvoice_shortcuts() {
        let root = std::env::var_os("DOTVOICE_SHORTCUT_TEST_DIR").map(PathBuf::from).unwrap_or_else(std::env::temp_dir);
        let base = root.join(format!("live-shortcut-test-{}", std::process::id()));
        let desktop = base.join("Desktop");
        let icons = base.join("shell-icons");
        let app = base.join("App");
        for dir in [&desktop, &app] {
            std::fs::create_dir_all(dir).unwrap();
        }
        let exe = app.join("DotVoice.exe");
        let other = app.join("Other.exe");
        std::fs::write(&exe, b"MZ").unwrap();
        std::fs::write(&other, b"MZ").unwrap();
        // A stale icon from an "older build" must be cleaned up.
        std::fs::create_dir_all(&icons).unwrap();
        std::fs::write(icons.join("dotvoice-ready-00000000.ico"), b"old").unwrap();

        let _apartment = Apartment::enter();
        let make = |name: &str, target: &Path, icon: Option<(&Path, i32)>| {
            let link = Link::new().unwrap();
            assert!(link.set_target(target));
            if let Some((path, index)) = icon {
                assert!(link.set_icon_location(path, index));
            }
            let path = desktop.join(name);
            assert!(link.save(&path));
            path
        };
        let installed = make("点声 DotVoice.lnk", &exe, Some((&exe, 0)));
        let pinned = make("DotVoice pinned.lnk", &exe, None);
        let foreign = make("Other.lnk", &other, Some((&other, 0)));
        let custom_icon = Path::new(r"C:\Windows\System32\shell32.dll");
        let custom = make("DotVoice custom.lnk", &exe, Some((custom_icon, 4)));
        let icon_of = |lnk: &Path| Link::load(lnk).unwrap().icon_location().unwrap();
        let name_of = |lnk: &Path| Path::new(&expand_env(&icon_of(lnk).0)).file_name().unwrap().to_string_lossy().to_string();

        let mut worker = Worker::new(exe.clone(), Some(icons.clone()), vec![(desktop.clone(), 0, true)]);
        worker.settle(None, Some(Live::Ready));
        assert_eq!(worker.applied, None, "nothing happens before prefs are known");
        worker.settle(Some(false), Some(Live::Ready));
        assert_eq!(icon_of(&pinned).0, "", "the off pass leaves an icon-less pin alone");
        assert_eq!(path_key(Path::new(&icon_of(&installed).0)), path_key(&exe));

        worker.settle(Some(true), Some(Live::Ready));
        assert!(name_of(&installed).starts_with("dotvoice-ready-"), "{:?}", icon_of(&installed));
        assert!(name_of(&pinned).starts_with("dotvoice-ready-"));
        assert_eq!(path_key(Path::new(&icon_of(&foreign).0)), path_key(&other), "foreign shortcut untouched");
        assert_eq!(icon_of(&custom), (custom_icon.to_string_lossy().to_string(), 4), "user icon untouched");
        assert!(!icons.join("dotvoice-ready-00000000.ico").exists(), "stale icon removed");
        assert_eq!(std::fs::read_dir(&icons).unwrap().count(), 3);

        worker.settle(Some(true), Some(Live::Paused));
        assert!(name_of(&installed).starts_with("dotvoice-paused-"));

        worker.settle(Some(false), Some(Live::Paused));
        assert_eq!(path_key(Path::new(&expand_env(&icon_of(&installed).0))), path_key(&exe), "off restores the exe icon");
        worker.finish();
        assert_eq!(path_key(Path::new(&expand_env(&icon_of(&installed).0))), path_key(&exe), "exit while off changes nothing");

        worker.settle(Some(true), Some(Live::Ready));
        assert!(name_of(&pinned).starts_with("dotvoice-ready-"));
        worker.finish();
        assert!(name_of(&installed).starts_with("dotvoice-idle-"));
        assert!(name_of(&pinned).starts_with("dotvoice-idle-"));
        // A taskbar pin an older build gave a live dot goes back to the plain mark and stays there.
        let taskbar = base.join("TaskBar");
        std::fs::create_dir_all(&taskbar).unwrap();
        let live_icon = icons.join(icon_file_name(Live::Ready.icon().0, Live::Ready.icon().1));
        let pin = taskbar.join("点声 DotVoice.lnk");
        let link = Link::new().unwrap();
        assert!(link.set_target(&exe) && link.set_icon_location(&live_icon, 0) && link.save(&pin));
        let mut worker = Worker::new(exe.clone(), Some(icons.clone()), vec![(desktop.clone(), 0, true), (taskbar.clone(), 0, false)]);
        worker.settle(Some(true), Some(Live::Ready));
        assert_eq!(path_key(Path::new(&expand_env(&icon_of(&pin).0))), path_key(&exe), "pin shows the plain mark");
        assert!(name_of(&installed).starts_with("dotvoice-ready-"), "desktop keeps the live dot");
        worker.finish();
        assert_eq!(path_key(Path::new(&expand_env(&icon_of(&pin).0))), path_key(&exe), "pin stays plain on exit");
        println!("scratch shortcuts left for inspection in {}", desktop.display());
    }
}
