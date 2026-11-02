//! Disposable Unicode Win32 foreground. Only commands cross the backend boundary.
//! All references into window state are short-lived; no mutable borrow crosses a
//! SendMessage, modal dialog, or DestroyWindow call.
use crate::backend::Backend;
use crate::config::{AppConfig, MountConfig};
use crate::contracts::{Command, Event, Page, RunState, Snapshot};
use crate::update::UpdateState;
use std::cell::{Cell, RefCell};
use std::collections::BTreeMap;
use std::path::PathBuf;
use std::ptr::{null, null_mut};
use std::rc::Rc;
use std::sync::{Arc, LazyLock};
use windows_sys::Win32::Foundation::*;
use windows_sys::Win32::Graphics::Dwm::DwmSetWindowAttribute;
use windows_sys::Win32::Graphics::Gdi::*;
use windows_sys::Win32::Storage::FileSystem::GetLogicalDrives;
use windows_sys::Win32::System::DataExchange::*;
use windows_sys::Win32::System::LibraryLoader::GetModuleHandleW;
use windows_sys::Win32::System::Memory::*;
use windows_sys::Win32::System::Registry::*;
use windows_sys::Win32::UI::Controls::Dialogs::*;
use windows_sys::Win32::UI::Controls::*;
use windows_sys::Win32::UI::HiDpi::GetDpiForWindow;
use windows_sys::Win32::UI::Input::KeyboardAndMouse::{
    EnableWindow, GetFocus, GetKeyState, IsWindowEnabled, SetFocus, TrackMouseEvent,
    TRACKMOUSEEVENT, TME_LEAVE,
};
use windows_sys::Win32::UI::Shell::*;
use windows_sys::Win32::UI::WindowsAndMessaging::*;

#[path = "ui_style.rs"]
mod style;

pub const WINDOW_CLASS: &str = "RcloneTray.Frontend";
pub const EDITOR_CLASS: &str = "RcloneTray.MountEditor";
const WM_EDITOR_CLOSED: u32 = WM_APP + 71;
const WM_SHORTCUT: u32 = WM_APP + 72;

/// Stable GetDlgItem IDs; page controls are direct children of WINDOW_CLASS.
pub mod ids {
    pub const NAV_MOUNTS: u16 = 101;
    pub const NAV_SERVICE: u16 = 102;
    pub const NAV_SETTINGS: u16 = 103;
    pub const NAV_ABOUT: u16 = 104;
    pub const RELOAD: u16 = 110;
    pub const CLOSE: u16 = 111;
    pub const EXIT: u16 = 112;
    pub const MESSAGE: u16 = 113;
    pub const FOOTER: u16 = 114;
    pub const TOGGLE_NAV: u16 = 115;
    pub const SEARCH: u16 = 201;
    pub const FILTER: u16 = 202;
    pub const CLEAR_FILTER: u16 = 203;
    pub const MOUNT_LIST: u16 = 204;
    pub const MOUNT_DETAIL: u16 = 205;
    pub const ADD: u16 = 210;
    pub const EDIT: u16 = 211;
    pub const DELETE: u16 = 212;
    pub const START: u16 = 213;
    pub const STOP: u16 = 214;
    pub const OPEN: u16 = 215;
    pub const LOG: u16 = 216;
    pub const START_ALL: u16 = 217;
    pub const STOP_ALL: u16 = 218;
    pub const REFRESH: u16 = 219;
    pub const SUMMARY: u16 = 220;
    pub const GUI_STATUS: u16 = 300;
    pub const GUI_START: u16 = 301;
    pub const GUI_STOP: u16 = 302;
    pub const GUI_OPEN: u16 = 303;
    pub const GUI_LOG: u16 = 304;
    pub const GUI_AUTO: u16 = 310;
    pub const GUI_NO_AUTH: u16 = 311;
    pub const GUI_USER: u16 = 312;
    pub const GUI_PASS: u16 = 313;
    pub const SERVICE_SAVE: u16 = 320;
    pub const SERVICE_CANCEL: u16 = 321;
    pub const SERVICE_REBASE: u16 = 322;
    pub const SERVICE_NOTE: u16 = 323;
    pub const EXE: u16 = 401;
    pub const CONF: u16 = 402;
    pub const BROWSE_EXE: u16 = 403;
    pub const BROWSE_CONF: u16 = 404;
    pub const AUTO_DISCOVER: u16 = 405;
    pub const ENVIRONMENT: u16 = 406;
    pub const DETECT: u16 = 407;
    pub const OPEN_CONF: u16 = 408;
    pub const STARTUP: u16 = 410;
    pub const AUTO_MOUNT: u16 = 411;
    pub const DELAY: u16 = 412;
    pub const THEME: u16 = 413;
    pub const REDUCE_MOTION: u16 = 414;
    pub const SETTINGS_SAVE: u16 = 420;
    pub const SETTINGS_CANCEL: u16 = 421;
    pub const SETTINGS_REBASE: u16 = 422;
    pub const SETTINGS_NOTE: u16 = 423;
    pub const DIAGNOSTICS: u16 = 501;
    pub const COPY: u16 = 502;
    pub const APP_CONFIG: u16 = 503;
    pub const LOG_DIR: u16 = 504;
    pub const ABOUT_CONF: u16 = 505;
    pub const CONF_DIR: u16 = 506;
    pub const HELP: u16 = 507;
    pub const WINFSP_HELP: u16 = 508;
    pub const ABOUT_REFRESH: u16 = 509;
    pub const ABOUT_EXIT: u16 = 510;
    pub const CHECK_UPDATE: u16 = 511;
    pub const OPEN_UPDATE: u16 = 512;
    pub const UPDATE_STATUS: u16 = 513;
    pub const NAME: u16 = 601;
    pub const REMOTE: u16 = 602;
    pub const POINT: u16 = 603;
    pub const VOLNAME: u16 = 604;
    pub const VFS: u16 = 605;
    pub const NETWORK: u16 = 606;
    pub const READ_ONLY: u16 = 607;
    pub const MOUNT_AUTO: u16 = 608;
    pub const BROWSE_POINT: u16 = 609;
    pub const EDITOR_CONF: u16 = 610;
    pub const EDITOR_SAVE: u16 = 620;
    pub const EDITOR_CANCEL: u16 = 621;
    pub const EDITOR_REBASE: u16 = 622;
    pub const EDITOR_NOTE: u16 = 623;
    pub const EDITOR_RELOAD: u16 = 624;
}
use ids::*;

#[derive(Clone, PartialEq, Eq)]
enum Value {
    Text(String),
    Check(bool),
}
impl Value {
    fn text(&self) -> &str {
        match self {
            Self::Text(s) => s,
            Self::Check(_) => "",
        }
    }
    fn checked(&self) -> bool {
        matches!(self, Self::Check(true))
    }
}
type Values = BTreeMap<u16, Value>;
#[derive(Clone)]
struct Draft {
    values: Values,
    baseline: Values,
    revision: u64,
    pending: bool,
    request_id: Option<u64>,
    error: String,
}
impl Draft {
    fn new(values: Values, revision: u64) -> Self {
        Self {
            baseline: values.clone(),
            values,
            revision,
            pending: false,
            request_id: None,
            error: String::new(),
        }
    }
    fn dirty(&self) -> bool {
        self.values != self.baseline
    }
}
struct Model {
    snapshot: Arc<Snapshot>,
    page: Page,
    selected: String,
    visible_ids: Vec<String>,
    list_rows: Vec<String>,
    service: Draft,
    settings: Draft,
    editor: Option<(String, Draft)>,
    reload_request: Option<u64>,
    message: String,
}
#[derive(Clone, Copy)]
struct Control {
    id: u16,
    hwnd: HWND,
    page: Option<Page>,
    rect: [i32; 4],
    grow_x: bool,
    grow_y: bool,
}
struct View {
    host: HWND,
    owner: HWND,
    backend: Backend,
    model: RefCell<Model>,
    controls: RefCell<Vec<Control>>,
    child: Cell<HWND>,
    font: Cell<HFONT>,
    title_font: Cell<HFONT>,
    small_font: Cell<HFONT>,
    icon_font: Cell<HFONT>,
    mark_icon: Cell<HICON>,
    big_icon: Cell<HICON>,
    small_icon: Cell<HICON>,
    panel_brush: Cell<HBRUSH>,
    input_brush: Cell<HBRUSH>,
    sidebar_brush: Cell<HBRUSH>,
    collapsed: Cell<bool>,
    compact: Cell<bool>,
    hot: Cell<HWND>,
    brush: Cell<HBRUSH>,
    dark: Cell<bool>,
    motion: Cell<Option<bool>>,
    dpi: Cell<u32>,
    loading: Cell<bool>,
    rendering: Cell<bool>,
    modal: Cell<bool>,
    scroll_x: Cell<i32>,
    scroll_y: Cell<i32>,
    lifecycle: Rc<Cell<u8>>,
}
impl Drop for View {
    fn drop(&mut self) {
        unsafe {
            let font = self.font.replace(null_mut());
            for object in [
                self.title_font.replace(null_mut()),
                self.small_font.replace(null_mut()),
                self.icon_font.replace(null_mut()),
                self.panel_brush.replace(null_mut()),
                self.input_brush.replace(null_mut()),
                self.sidebar_brush.replace(null_mut()),
            ] {
                if !object.is_null() {
                    DeleteObject(object);
                }
            }
            for icon in [
                self.mark_icon.replace(null_mut()),
                self.big_icon.replace(null_mut()),
                self.small_icon.replace(null_mut()),
            ] {
                if !icon.is_null() {
                    DestroyIcon(icon);
                }
            }
            if !font.is_null() {
                DeleteObject(font);
            }
            let brush = self.brush.replace(null_mut());
            if !brush.is_null() {
                DeleteObject(brush);
            }
        }
    }
}

/// Lightweight, UI-thread-only HWND owner. Backend never retains this object.
pub struct Frontend {
    hwnd: HWND,
}
impl Frontend {
    pub fn create(
        host: HWND,
        backend: Backend,
        page: Page,
        selected_id: Option<String>,
    ) -> Result<Self, String> {
        let hwnd = create_view(
            host,
            null_mut(),
            backend,
            page,
            selected_id.unwrap_or_default(),
            None,
        )?;
        unsafe {
            ShowWindow(hwnd, SW_SHOWNORMAL);
            SetForegroundWindow(hwnd);
        }
        Ok(Self { hwnd })
    }
    pub fn hwnd(&self) -> HWND {
        self.hwnd
    }
    pub fn is_open(&self) -> bool {
        unsafe { !view_ptr(self.hwnd).is_null() }
    }
    pub fn show(&self, page: Page, mount_id: Option<&str>) -> bool {
        navigate(self.hwnd, page, mount_id)
    }
    pub fn refresh(&self, snapshot: Arc<Snapshot>) {
        refresh(self.hwnd, snapshot);
    }
    pub fn handle_event(&self, event: &Event, snapshot: Arc<Snapshot>) {
        handle_event(self.hwnd, event, snapshot);
    }
    pub fn request_close(&self) -> bool {
        close_view(self.hwnd)
    }
    pub fn request_exit(&self) -> bool {
        exit_app(self.hwnd)
    }
}
impl Drop for Frontend {
    fn drop(&mut self) {
        unsafe {
            if self.is_open() {
                DestroyWindow(self.hwnd);
            }
        }
    }
}

fn wide(s: &str) -> Vec<u16> {
    s.encode_utf16().chain(std::iter::once(0)).collect()
}
unsafe fn view_ptr(hwnd: HWND) -> *const View {
    if hwnd.is_null() || IsWindow(hwnd) == 0 {
        return null();
    }
    // Host, common dialogs and unrelated HWNDs may have their own userdata.
    // Never interpret those pointers as our boxed view.
    let mut class = [0u16; 64];
    let len = GetClassNameW(hwnd, class.as_mut_ptr(), class.len() as i32).max(0) as usize;
    let class = &class[..len];
    if !class.iter().copied().eq(WINDOW_CLASS.encode_utf16())
        && !class.iter().copied().eq(EDITOR_CLASS.encode_utf16())
    {
        return null();
    }
    GetWindowLongPtrW(hwnd, GWLP_USERDATA) as *const View
}
fn with_view<R>(hwnd: HWND, f: impl FnOnce(&View) -> R) -> Option<R> {
    unsafe {
        let pointer = view_ptr(hwnd);
        if pointer.is_null() {
            return None;
        }
        // A nested native message may destroy the HWND; retain Rust state until
        // this callback returns, without keeping any closed window alive.
        Rc::increment_strong_count(pointer);
        let view = Rc::from_raw(pointer);
        Some(f(&view))
    }
}
fn handle(hwnd: HWND, id: u16) -> HWND {
    with_view(hwnd, |v| {
        v.controls
            .borrow()
            .iter()
            .find(|c| c.id == id)
            .map_or(null_mut(), |c| c.hwnd)
    })
    .unwrap_or(null_mut())
}
fn text(hwnd: HWND) -> String {
    if hwnd.is_null() {
        return String::new();
    }
    unsafe {
        let len = GetWindowTextLengthW(hwnd).max(0) as usize;
        let mut buf = vec![0u16; len + 1];
        let n = GetWindowTextW(hwnd, buf.as_mut_ptr(), buf.len() as i32).max(0) as usize;
        String::from_utf16_lossy(&buf[..n])
    }
}
fn set_text(hwnd: HWND, s: &str) {
    if !hwnd.is_null() && text(hwnd) != s {
        unsafe {
            SetWindowTextW(hwnd, wide(s).as_ptr());
        }
    }
}
fn enable(hwnd: HWND, enabled: bool) {
    if !hwnd.is_null() {
        unsafe {
            EnableWindow(hwnd, enabled as i32);
        }
    }
}
fn register_classes() -> Result<(), String> {
    static CLASSES: LazyLock<Result<(), String>> = LazyLock::new(|| unsafe {
        for name in [WINDOW_CLASS, EDITOR_CLASS] {
            let class = wide(name);
            let wc = WNDCLASSW {
                style: CS_DBLCLKS,
                lpfnWndProc: Some(window_proc),
                hInstance: GetModuleHandleW(null()),
                hCursor: LoadCursorW(null_mut(), IDC_ARROW),
                hbrBackground: null_mut(),
                lpszClassName: class.as_ptr(),
                ..std::mem::zeroed()
            };
            if RegisterClassW(&wc) == 0 && GetLastError() != ERROR_CLASS_ALREADY_EXISTS {
                return Err(format!("注册前端窗口失败：{}", GetLastError()));
            }
        }
        Ok(())
    });
    CLASSES.clone()
}
fn create_view(
    host: HWND,
    owner: HWND,
    backend: Backend,
    page: Page,
    selected: String,
    mount: Option<Option<MountConfig>>,
) -> Result<HWND, String> {
    register_classes()?;
    let snapshot = backend.snapshot();
    let lifecycle = Rc::new(Cell::new(0));
    let editor = mount.is_some();
    let mut model = Model {
        page,
        selected,
        visible_ids: Vec::new(),
        list_rows: Vec::new(),
        service: Draft::new(
            if editor {
                Values::new()
            } else {
                service_values(&snapshot.config)
            },
            snapshot.revision,
        ),
        settings: Draft::new(
            if editor {
                Values::new()
            } else {
                settings_values(&snapshot.config)
            },
            snapshot.revision,
        ),
        editor: None,
        reload_request: None,
        message: snapshot.message.clone(),
        snapshot,
    };
    if let Some(mount) = mount {
        let is_new = mount.is_none();
        let mount = match mount {
            Some(m) => m,
            None => MountConfig::new(
                String::from("新挂载"),
                String::from("remote:"),
                String::from("Z:"),
            )?,
        };
        let mut values = mount_values(&mount);
        if is_new {
            for id in [NAME, REMOTE, POINT] {
                values.insert(id, Value::Text(String::new()));
            }
        }
        model.editor = Some((mount.id, Draft::new(values, model.snapshot.revision)));
    }
    let view = Rc::new(View {
        host,
        owner,
        backend,
        model: RefCell::new(model),
        controls: RefCell::new(Vec::new()),
        child: Cell::new(null_mut()),
        font: Cell::new(null_mut()),
        title_font: Cell::new(null_mut()),
        small_font: Cell::new(null_mut()),
        icon_font: Cell::new(null_mut()),
        mark_icon: Cell::new(null_mut()),
        big_icon: Cell::new(null_mut()),
        small_icon: Cell::new(null_mut()),
        panel_brush: Cell::new(null_mut()),
        input_brush: Cell::new(null_mut()),
        sidebar_brush: Cell::new(null_mut()),
        collapsed: Cell::new(false),
        compact: Cell::new(false),
        hot: Cell::new(null_mut()),
        brush: Cell::new(null_mut()),
        dark: Cell::new(false),
        motion: Cell::new(None),
        dpi: Cell::new(96),
        loading: Cell::new(false),
        rendering: Cell::new(false),
        modal: Cell::new(false),
        lifecycle: lifecycle.clone(),
        scroll_x: Cell::new(0),
        scroll_y: Cell::new(0),
    });
    let ptr = Rc::into_raw(view);
    let title = if editor {
        "挂载配置".to_string()
    } else {
        format!("rclone_tray · {}", page.label())
    };
    let hwnd = unsafe {
        CreateWindowExW(
            WS_EX_CONTROLPARENT,
            wide(if editor { EDITOR_CLASS } else { WINDOW_CLASS }).as_ptr(),
            wide(&title).as_ptr(),
            WS_OVERLAPPEDWINDOW | WS_CLIPCHILDREN | WS_VSCROLL | WS_HSCROLL,
            CW_USEDEFAULT,
            CW_USEDEFAULT,
            if editor { 700 } else { 1180 },
            if editor { 780 } else { 840 },
            owner,
            null_mut(),
            GetModuleHandleW(null()),
            ptr.cast(),
        )
    };
    if hwnd.is_null() {
        if lifecycle.get() == 0 {
            unsafe {
                drop(Rc::from_raw(ptr));
            }
        }
        return Err(format!("创建前端窗口失败：{}", unsafe {
            GetLastError()
        }));
    }
    if let Err(error) = build_controls(hwnd, editor) {
        unsafe {
            DestroyWindow(hwnd);
        }
        return Err(error);
    }
    with_view(hwnd, |v| {
        v.dpi.set(unsafe { GetDpiForWindow(hwnd) }.max(96))
    });
    let dpi = with_view(hwnd, |v| v.dpi.get()).unwrap_or(96) as i32;
    let mut work = unsafe { std::mem::zeroed::<RECT>() };
    unsafe {
        if SystemParametersInfoW(SPI_GETWORKAREA, 0, (&mut work as *mut RECT).cast(), 0) != 0 {
            let width =
                ((if editor { 700 } else { 1180 }) * dpi / 96).min(work.right - work.left - 24);
            let height =
                ((if editor { 780 } else { 840 }) * dpi / 96).min(work.bottom - work.top - 24);
            SetWindowPos(
                hwnd,
                null_mut(),
                work.left + (work.right - work.left - width) / 2,
                work.top + (work.bottom - work.top - height) / 2,
                width,
                height,
                SWP_NOZORDER | SWP_NOACTIVATE,
            );
        }
    }
    apply_font(hwnd);
    refresh(hwnd, with_view(hwnd, |v| v.backend.snapshot()).unwrap());
    layout(hwnd);
    Ok(hwnd)
}

unsafe extern "system" fn window_proc(hwnd: HWND, msg: u32, w: WPARAM, l: LPARAM) -> LRESULT {
    if msg == WM_NCCREATE {
        let create = &*(l as *const CREATESTRUCTW);
        let ptr = create.lpCreateParams as *mut View;
        SetWindowLongPtrW(hwnd, GWLP_USERDATA, ptr as isize);
        (*ptr).lifecycle.set(1);
        return DefWindowProcW(hwnd, msg, w, l);
    }
    if msg == WM_NCDESTROY {
        let ptr = GetWindowLongPtrW(hwnd, GWLP_USERDATA) as *mut View;
        SetWindowLongPtrW(hwnd, GWLP_USERDATA, 0);
        if !ptr.is_null() {
            let view = Rc::from_raw(ptr);
            view.lifecycle.set(2);
            let host = view.host;
            let owner = view.owner;
            let child = view.child.replace(null_mut());
            if !child.is_null() && IsWindow(child) != 0 {
                DestroyWindow(child);
            }
            drop(view);
            if owner.is_null() {
                PostMessageW(host, crate::host::WM_FRONTEND_CLOSED, hwnd as usize, 0);
            } else {
                PostMessageW(owner, WM_EDITOR_CLOSED, hwnd as usize, 0);
            }
        }
        return DefWindowProcW(hwnd, msg, w, l);
    }
    if view_ptr(hwnd).is_null() {
        return DefWindowProcW(hwnd, msg, w, l);
    }
    match msg {
        WM_CLOSE => {
            close_view(hwnd);
            0
        }
        WM_SIZE => {
            layout(hwnd);
            0
        }
        WM_VSCROLL => {
            scroll(hwnd, SB_VERT, (w & 0xffff) as i32);
            0
        }
        WM_HSCROLL => {
            scroll(hwnd, SB_HORZ, (w & 0xffff) as i32);
            0
        }
        WM_MOUSEWHEEL => {
            let steps = (w >> 16) as u16 as i16 as i32 / 120;
            for _ in 0..steps.abs() {
                scroll(
                    hwnd,
                    SB_VERT,
                    if steps > 0 { SB_LINEUP } else { SB_LINEDOWN },
                );
            }
            0
        }
        WM_GETMINMAXINFO => {
            let editor = with_view(hwnd, |v| !v.owner.is_null()).unwrap_or(false);
            let dpi = with_view(hwnd, |v| v.dpi.get()).unwrap_or(96) as i32;
            let info = &mut *(l as *mut MINMAXINFO);
            info.ptMinTrackSize.x = (if editor { 460 } else { 720 }) * dpi / 96;
            info.ptMinTrackSize.y = 480 * dpi / 96;
            0
        }
        WM_DPICHANGED => {
            with_view(hwnd, |v| v.dpi.set((w as u32 & 0xffff).max(96)));
            let r = &*(l as *const RECT);
            SetWindowPos(
                hwnd,
                null_mut(),
                r.left,
                r.top,
                r.right - r.left,
                r.bottom - r.top,
                SWP_NOZORDER | SWP_NOACTIVATE,
            );
            apply_font(hwnd);
            layout(hwnd);
            0
        }
        WM_COMMAND => {
            command(
                hwnd,
                (w & 0xffff) as u16,
                ((w >> 16) & 0xffff) as u16,
                l as HWND,
            );
            0
        }
        WM_SHORTCUT => {
            shortcut(hwnd, w as u32);
            0
        }
        WM_EDITOR_CLOSED => {
            with_view(hwnd, |v| {
                if v.child.get() == w as HWND {
                    v.child.set(null_mut());
                }
            });
            0
        }
        WM_SETTINGCHANGE | WM_THEMECHANGED => {
            apply_theme(hwnd);
            0
        }
        WM_PAINT => {
            let mut paint = std::mem::zeroed::<PAINTSTRUCT>();
            let dc = BeginPaint(hwnd, &mut paint);
            paint_background(hwnd, dc);
            EndPaint(hwnd, &paint);
            0
        }
        WM_ERASEBKGND => { paint_background(hwnd, w as HDC); 1 }
        WM_MEASUREITEM => {
            let item = &mut *(l as *mut MEASUREITEMSTRUCT);
            let dpi = with_view(hwnd, |v| v.dpi.get()).unwrap_or(96);
            item.itemHeight = (if item.CtlID == MOUNT_LIST as u32 { 76 } else { 30 }) * dpi / 96;
            1
        }
        WM_DRAWITEM => { paint_item(hwnd, &*(l as *const DRAWITEMSTRUCT)); 1 }
        WM_CTLCOLORSTATIC | WM_CTLCOLOREDIT | WM_CTLCOLORLISTBOX | WM_CTLCOLORBTN => {
            let id = GetDlgCtrlID(l as HWND) as u16;
            with_view(hwnd, |v| {
                let p = style::palette(v.dark.get());
                let panel = matches!(id, MESSAGE | MOUNT_DETAIL | GUI_STATUS | ENVIRONMENT | DIAGNOSTICS | EDITOR_NOTE | MOUNT_LIST);
                let color = if panel { p.panel } else if msg == WM_CTLCOLOREDIT || msg == WM_CTLCOLORLISTBOX { p.input } else { p.bg };
                SetTextColor(w as HDC, if IsWindowEnabled(l as HWND) == 0 { p.disabled } else if panel { p.secondary } else { p.text });
                SetBkColor(w as HDC, color);
                if panel { v.panel_brush.get() as isize } else if color == p.input { v.input_brush.get() as isize } else { v.brush.get() as isize }
            }).unwrap_or(0)
        }
        _ => DefWindowProcW(hwnd, msg, w, l),
    }
}

fn with_utf16_text<R>(value: &[u16], f: impl FnOnce(&str) -> R) -> R {
    let mut utf8 = [0u8; 8192];
    let mut end = 0;
    for value in char::decode_utf16(value.iter().copied()) {
        let value = value.unwrap_or(char::REPLACEMENT_CHARACTER);
        end += value.encode_utf8(&mut utf8[end..]).len();
    }
    f(std::str::from_utf8(&utf8[..end]).unwrap_or(""))
}

fn with_control_text<R>(hwnd: HWND, f: impl FnOnce(&str) -> R) -> R {
    let mut utf16 = [0u16; 2048];
    let length = unsafe { GetWindowTextW(hwnd, utf16.as_mut_ptr(), utf16.len() as i32) }.max(0) as usize;
    with_utf16_text(&utf16[..length], f)
}

unsafe fn paint_background(hwnd: HWND, dc: HDC) {
    let mut rect = std::mem::zeroed::<RECT>();
    GetClientRect(hwnd, &mut rect);
    with_view(hwnd, |v| {
        let dpi = v.dpi.get();
        style::background(dc, rect, dpi, if v.compact.get() { 64 } else { 192 },
            !v.owner.is_null(), style::palette(v.dark.get()));
        if v.compact.get() || !v.owner.is_null() { return; }
        let mark = v.mark_icon.get();
        if mark.is_null() { return; }
        let scale = dpi as i32;
        let size = 24 * scale / 96;
        DrawIconEx(dc, 24 * scale / 96, 32 * scale / 96, mark, size, size, 0, null_mut(), DI_NORMAL);
    });
}

unsafe fn paint_item(hwnd: HWND, item: &DRAWITEMSTRUCT) {
    with_view(hwnd, |v| {
        let p = style::palette(v.dark.get());
        if item.CtlID == MOUNT_LIST as u32 {
            let m = v.model.borrow();
            if let Some(id) = m.visible_ids.get(item.itemID as usize) {
                if let Some(mount) = m.snapshot.config.mounts.iter().find(|mount| &mount.id == id) {
                    let state = m.snapshot.mounts.get(id).map_or(RunState::Stopped, |run| run.state);
                    let mut row = *item;
                    if v.hot.get() == item.hwndItem {
                        let mut cursor = std::mem::zeroed::<POINT>();
                        GetCursorPos(&mut cursor);
                        ScreenToClient(item.hwndItem, &mut cursor);
                        let hit = SendMessageW(item.hwndItem, LB_ITEMFROMPOINT, 0, (cursor.x as u16 as isize) | ((cursor.y as u16 as isize) << 16));
                        if hit >> 16 == 0 && (hit & 0xffff) as u32 == item.itemID { row.itemState |= ODS_HOTLIGHT; }
                    }
                    style::row(&row, p, v.dpi.get(), v.font.get(), v.icon_font.get(), &mount.name, &mount.remote, &mount.mount_point, state);
                }
            }
        } else if item.CtlType == ODT_COMBOBOX {
            let selected = item.itemState & ODS_SELECTED != 0 && item.itemState & ODS_COMBOBOXEDIT == 0;
            let color = if selected { p.selected } else { p.input };
            SetDCBrushColor(item.hDC, color);
            FillRect(item.hDC, &item.rcItem, GetStockObject(DC_BRUSH));
            if item.itemID != u32::MAX {
                let length = SendMessageW(item.hwndItem, CB_GETLBTEXTLEN, item.itemID as usize, 0);
                let mut value = [0u16; 2048];
                if length >= 0 && (length as usize) < value.len() {
                    SendMessageW(item.hwndItem, CB_GETLBTEXT, item.itemID as usize, value.as_mut_ptr() as isize);
                    let mut rect = item.rcItem;
                    let padding = (8 * v.dpi.get() / 96) as i32;
                    rect.left += padding; rect.right -= padding;
                    with_utf16_text(&value[..length as usize], |caption| {
                        style::text(item.hDC, rect, caption, v.font.get(), if item.itemState & ODS_DISABLED != 0 { p.disabled } else if selected { p.accent } else { p.text },
                            DT_SINGLELINE | DT_VCENTER | DT_END_ELLIPSIS | DT_NOPREFIX);
                    });
                }
            }
        } else if item.CtlType == ODT_BUTTON {
            let page = v.model.borrow().page;
            let active = matches!((item.CtlID as u16, page), (NAV_MOUNTS, Page::Mounts) | (NAV_SERVICE, Page::Service) | (NAV_SETTINGS, Page::Settings) | (NAV_ABOUT, Page::About));
            with_control_text(item.hwndItem, |caption| {
                style::button(item, p, v.dpi.get(), v.font.get(), v.icon_font.get(), caption, active, v.hot.get() == item.hwndItem,
                    if is_check(item.CtlID as u16) { Some(SendMessageW(item.hwndItem, BM_GETCHECK, 0, 0) == BST_CHECKED as isize) } else { None });
            });
        } else {
            let id = item.CtlID as u16;
            let sidebar = matches!(id, 900 | 922 | 923);
            FillRect(item.hDC, &item.rcItem, if sidebar { v.sidebar_brush.get() } else { v.brush.get() });
            let title = matches!(id, 920 | 922);
            let small = matches!(id, 900 | 902 | 921 | 923 | FOOTER | SERVICE_NOTE | SETTINGS_NOTE);
            let color = if title { p.text } else if matches!(id, 903) { p.warning } else if small { p.muted } else { p.secondary };
            with_control_text(item.hwndItem, |caption| {
                style::text(item.hDC, item.rcItem, caption, if title { v.title_font.get() } else if small { v.small_font.get() } else { v.font.get() },
                    color, DT_LEFT | DT_NOPREFIX | if id == FOOTER { DT_SINGLELINE | DT_VCENTER | DT_END_ELLIPSIS } else { DT_WORDBREAK });
            });
        }
    });
}

unsafe extern "system" fn control_proc(child: HWND, msg: u32, w: WPARAM, l: LPARAM, _: usize, _: usize) -> LRESULT {
    let parent = GetAncestor(child, GA_ROOT);
    let id = GetDlgCtrlID(child) as u16;
    if msg == WM_NCDESTROY {
        with_view(parent, |v| { if v.hot.get() == child { v.hot.set(null_mut()); } });
        RemoveWindowSubclass(child, Some(control_proc), 1);
        return DefSubclassProc(child, msg, w, l);
    }
    if msg == WM_MOUSEMOVE {
        let previous = with_view(parent, |v| v.hot.replace(child)).unwrap_or(null_mut());
        if previous != child {
            if !previous.is_null() { InvalidateRect(previous, null(), 0); }
            InvalidateRect(child, null(), 0);
            let mut tracking = TRACKMOUSEEVENT { cbSize: std::mem::size_of::<TRACKMOUSEEVENT>() as u32, dwFlags: TME_LEAVE, hwndTrack: child, dwHoverTime: 0 };
            TrackMouseEvent(&mut tracking);
        }
    } else if msg == WM_MOUSELEAVE {
        with_view(parent, |v| { if v.hot.get() == child { v.hot.set(null_mut()); } });
        InvalidateRect(child, null(), 0);
    }
    if msg == WM_PAINT && is_check(id) {
        let mut paint = std::mem::zeroed::<PAINTSTRUCT>();
        let dc = BeginPaint(child, &mut paint);
        let mut rect = std::mem::zeroed::<RECT>();
        GetClientRect(child, &mut rect);
        let state = if IsWindowEnabled(child) == 0 { ODS_DISABLED } else { 0 }
            | if GetFocus() == child { ODS_FOCUS } else { 0 }
            | if SendMessageW(child, BM_GETSTATE, 0, 0) & BST_PUSHED as isize != 0 { ODS_SELECTED } else { 0 };
        paint_item(parent, &DRAWITEMSTRUCT { CtlType: ODT_BUTTON, CtlID: id as u32, hwndItem: child, hDC: dc, rcItem: rect, itemState: state, ..std::mem::zeroed() });
        EndPaint(child, &paint);
        return 0;
    }
    let result = DefSubclassProc(child, msg, w, l);
    if matches!(msg, WM_SIZE | WM_SETFONT | WM_SETTEXT) && GetWindowLongW(child, GWL_STYLE) as u32 & ES_MULTILINE as u32 != 0 && !is_check(id) {
        let mut class = [0u16; 16];
        GetClassNameW(child, class.as_mut_ptr(), class.len() as i32);
        if class[0] == b'E' as u16 {
            let mut rect = std::mem::zeroed::<RECT>();
            GetClientRect(child, &mut rect);
            let padding = (10 * GetDpiForWindow(child).max(96) / 96) as i32;
            rect.left += padding; rect.top += padding; rect.right -= padding; rect.bottom -= padding;
            SendMessageW(child, EM_SETRECTNP, 0, (&rect as *const RECT) as isize);
        }
    }
    if matches!(msg, WM_SETFOCUS | WM_KILLFOCUS | WM_ENABLE | BM_SETCHECK | BM_SETSTATE) { InvalidateRect(child, null(), 0); }
    if msg == WM_PAINT && !is_check(id) {
        let mut class = [0u16; 16];
        GetClassNameW(child, class.as_mut_ptr(), class.len() as i32);
        if class[0] == b'E' as u16 || class[0] == b'C' as u16 {
            let dc = GetDC(child);
            let mut rect = std::mem::zeroed::<RECT>(); GetClientRect(child, &mut rect);
            with_view(parent, |v| {
                let p = style::palette(v.dark.get());
                if id == SEARCH && GetWindowTextLengthW(child) == 0 && GetFocus() != child {
                    let mut cue = rect;
                    let padding = (8 * v.dpi.get() / 96) as i32;
                    cue.left += padding; cue.right -= padding;
                    style::text(dc, cue, "搜索名称、remote 或位置 · Ctrl+F", v.font.get(), p.muted,
                        DT_SINGLELINE | DT_VCENTER | DT_END_ELLIPSIS | DT_NOPREFIX);
                }
                if class[0] == b'C' as u16 {
                    let saved = SaveDC(dc);
                    let arrow = (26 * v.dpi.get() / 96) as i32;
                    let face = RECT { left: rect.right - arrow, top: 0, ..rect };
                    FillRect(dc, &face, v.input_brush.get());
                    let edge = (2 * v.dpi.get() / 96).max(1) as i32;
                    for strip in [
                        RECT { bottom: edge, ..rect }, RECT { top: rect.bottom - edge, ..rect },
                        RECT { right: edge, ..rect }, RECT { left: rect.right - edge, ..rect },
                    ] { FillRect(dc, &strip, v.input_brush.get()); }
                    SelectObject(dc, GetStockObject(DC_PEN));
                    SetDCPenColor(dc, if IsWindowEnabled(child) == 0 { p.disabled } else { p.secondary });
                    let cx = rect.right - arrow / 2;
                    let cy = rect.bottom / 2;
                    let d = (4 * v.dpi.get() / 96) as i32;
                    let points = [POINT { x: cx - d, y: cy - d / 2 }, POINT { x: cx, y: cy + d / 2 }, POINT { x: cx + d, y: cy - d / 2 }];
                    Polyline(dc, points.as_ptr(), 3);
                    RestoreDC(dc, saved);
                }
                style::border(dc, rect, v.dpi.get(), if GetFocus() == child || IsChild(child, GetFocus()) != 0 { p.accent } else if v.hot.get() == child { p.muted } else { p.border });
            });
            ReleaseDC(child, dc);
        }
    }
    result
}

fn add_control(
    hwnd: HWND,
    id: u16,
    page: Option<Page>,
    class: &str,
    caption: &str,
    style: u32,
    rect: [i32; 4],
    grow_x: bool,
    grow_y: bool,
) -> Result<HWND, String> {
    let child = unsafe {
        CreateWindowExW(
            0,
            wide(class).as_ptr(),
            wide(caption).as_ptr(),
            WS_CHILD | WS_VISIBLE | WS_CLIPSIBLINGS | style,
            rect[0],
            rect[1],
            rect[2],
            rect[3],
            hwnd,
            id as usize as HMENU,
            GetModuleHandleW(null()),
            null(),
        )
    };
    if child.is_null() {
        return Err(format!("创建控件 {id} 失败：{}", unsafe {
            GetLastError()
        }));
    }
    if class == "BUTTON" || class == "EDIT" || class == "COMBOBOX" || class == "LISTBOX" {
        unsafe { SetWindowSubclass(child, Some(control_proc), 1, 0); }
    }
    with_view(hwnd, |v| {
        v.controls.borrow_mut().push(Control {
            id,
            hwnd: child,
            page,
            rect,
            grow_x,
            grow_y,
        })
    });
    Ok(child)
}
fn label(
    hwnd: HWND,
    id: u16,
    page: Option<Page>,
    s: &str,
    rect: [i32; 4],
    grow: bool,
) -> Result<(), String> {
    add_control(
        hwnd,
        id,
        page,
        "STATIC",
        s,
        windows_sys::Win32::System::SystemServices::SS_OWNERDRAW,
        rect,
        grow,
        false,
    )
    .map(|_| ())
}
fn button(hwnd: HWND, id: u16, page: Option<Page>, s: &str, rect: [i32; 4]) -> Result<(), String> {
    add_control(
        hwnd,
        id,
        page,
        "BUTTON",
        s,
        WS_TABSTOP | BS_OWNERDRAW as u32,
        rect,
        false,
        false,
    )
    .map(|_| ())
}
fn edit(
    hwnd: HWND,
    id: u16,
    page: Option<Page>,
    rect: [i32; 4],
    extra: u32,
    grow: bool,
) -> Result<(), String> {
    let h = add_control(
        hwnd,
        id,
        page,
        "EDIT",
        "",
        WS_TABSTOP | ES_AUTOHSCROLL as u32 | extra,
        rect,
        grow,
        false,
    )?;
    unsafe {
        SendMessageW(h, EM_SETMARGINS, (EC_LEFTMARGIN | EC_RIGHTMARGIN) as usize, 8 | (8 << 16));
        SendMessageW(h, EM_SETLIMITTEXT, 32767, 0);
    }
    Ok(())
}
fn check(hwnd: HWND, id: u16, page: Option<Page>, s: &str, rect: [i32; 4]) -> Result<(), String> {
    add_control(
        hwnd,
        id,
        page,
        "BUTTON",
        s,
        WS_TABSTOP | BS_AUTOCHECKBOX as u32,
        rect,
        true,
        false,
    )
    .map(|_| ())
}
fn combo(
    hwnd: HWND,
    id: u16,
    page: Option<Page>,
    rect: [i32; 4],
    items: &[&str],
    editable: bool,
) -> Result<(), String> {
    let h = add_control(
        hwnd,
        id,
        page,
        "COMBOBOX",
        "",
        WS_TABSTOP
            | WS_VSCROLL
            | (if editable {
                CBS_DROPDOWN
            } else {
                CBS_DROPDOWNLIST | CBS_OWNERDRAWFIXED | CBS_HASSTRINGS
            }) as u32
            | CBS_AUTOHSCROLL as u32,
        rect,
        false,
        false,
    )?;
    for s in items {
        unsafe {
            SendMessageW(h, CB_ADDSTRING, 0, wide(s).as_ptr() as isize);
        }
    }
    unsafe {
        SendMessageW(h, CB_SETCURSEL, 0, 0);
    }
    Ok(())
}
fn readonly(
    hwnd: HWND,
    id: u16,
    page: Option<Page>,
    rect: [i32; 4],
    grow_x: bool,
    grow_y: bool,
) -> Result<(), String> {
    add_control(
        hwnd,
        id,
        page,
        "EDIT",
        "",
        WS_TABSTOP
            | WS_VSCROLL
            | ES_MULTILINE as u32
            | ES_AUTOVSCROLL as u32
            | ES_READONLY as u32,
        rect,
        grow_x,
        grow_y,
    )
    .map(|_| ())
}

fn build_controls(hwnd: HWND, editor: bool) -> Result<(), String> {
    if editor { return build_editor(hwnd); }
    label(hwnd, 922, None, "rclone", [64, 28, 104, 32], false)?;
    label(hwnd, 923, None, "TRAY / WORKSPACE", [24, 64, 148, 20], false)?;
    button(hwnd, TOGGLE_NAV, None, "折叠侧栏", [16, 104, 160, 32])?;
    for (id, s, y) in [
        (NAV_MOUNTS, "挂载管理", 156),
        (NAV_SERVICE, "rclone GUI", 204),
        (NAV_SETTINGS, "设置", 252),
        (NAV_ABOUT, "关于与诊断", 300),
    ] { button(hwnd, id, None, s, [16, y, 160, 40])?; }
    button(hwnd, EXIT, None, "退出应用…", [16, 660, 160, 36])?;
    label(hwnd, 900, None, "关闭窗口，任务继续。\r\n从系统托盘随时返回。", [24, 708, 144, 44], false)?;
    label(hwnd, 920, None, "挂载管理", [216, 24, 500, 32], true)?;
    label(hwnd, 921, None, "连接远端，让文件触手可及。", [216, 64, 500, 24], true)?;
    button(hwnd, RELOAD, None, "重新载入配置", [778, 28, 156, 32])?;
    button(hwnd, CLOSE, None, "关闭至托盘", [946, 28, 132, 32])?;
    readonly(hwnd, MESSAGE, None, [216, 116, 870, 48], true, false)?;
    label(hwnd, FOOTER, None, "", [216, 772, 870, 24], true)?;
    build_mounts(hwnd)?;
    build_service(hwnd)?;
    build_settings(hwnd)?;
    build_about(hwnd)?;
    Ok(())
}
fn build_mounts(hwnd: HWND) -> Result<(), String> {
    let p = Some(Page::Mounts);
    edit(hwnd, SEARCH, p, [216, 184, 440, 32], 0, true)?;
    unsafe {
        SendMessageW(handle(hwnd, SEARCH), EM_SETCUEBANNER, 0,
            wide("搜索名称、remote 或位置  ·  Ctrl+F").as_ptr() as isize);
    }
    combo(hwnd, FILTER, p, [676, 184, 168, 230],
        &["全部状态", "已停止", "启动中", "已就绪", "停止中", "失败"], false)?;
    button(hwnd, CLEAR_FILTER, p, "清除筛选", [856, 184, 110, 32])?;
    for (id, s, x, w) in [
        (ADD, "添加挂载", 216, 124),
        (START_ALL, "启动全部", 352, 120),
        (STOP_ALL, "停止全部", 484, 120),
        (REFRESH, "刷新环境", 616, 120),
    ] { button(hwnd, id, p, s, [x, 232, w, 32])?; }
    label(hwnd, SUMMARY, p, "", [216, 280, 870, 44], true)?;
    add_control(hwnd, MOUNT_LIST, p, "LISTBOX", "",
        WS_TABSTOP | WS_VSCROLL | LBS_NOTIFY as u32 | LBS_NOINTEGRALHEIGHT as u32
            | LBS_OWNERDRAWFIXED as u32 | LBS_HASSTRINGS as u32,
        [216, 336, 500, 300], true, true)?;
    readonly(hwnd, MOUNT_DETAIL, p, [736, 336, 350, 300], false, true)?;
    for (id, s, x, w) in [
        (START, "启动", 216, 100),
        (STOP, "停止", 328, 100),
        (OPEN, "打开位置", 440, 120),
        (LOG, "查看日志", 572, 120),
        (EDIT, "编辑", 704, 100),
        (DELETE, "删除配置", 816, 130),
    ] { button(hwnd, id, p, s, [x, 656, w, 32])?; }
    label(hwnd, 902, p,
        "新增与编辑只保存配置，不会自动启动；删除不影响远端文件。\r\n运行中的连接参数请先停止再修改。双击编辑 · Delete 删除。",
        [216, 704, 870, 44], true)?;
    Ok(())
}
fn build_service(hwnd: HWND) -> Result<(), String> {
    let p = Some(Page::Service);
    readonly(hwnd, GUI_STATUS, p, [184, 142, 870, 112], true, false)?;
    for (id, s, x) in [
        (GUI_START, "启动 GUI", 184),
        (GUI_STOP, "停止 GUI", 324),
        (GUI_OPEN, "打开网页", 464),
        (GUI_LOG, "查看日志", 604),
    ] {
        button(hwnd, id, p, s, [x, 266, 130, 34])?;
    }
    check(
        hwnd,
        GUI_AUTO,
        p,
        "随应用启动 GUI（每次应用启动生效，与开机挂载无关）",
        [184, 320, 850, 30],
    )?;
    check(
        hwnd,
        GUI_NO_AUTH,
        p,
        "免认证（仅限完全可信本机）",
        [184, 360, 850, 30],
    )?;
    label(hwnd,903,p,"警告：免认证开放本机 5572 RC API，任何本机进程都能调用，\r\n等同本机命令执行权限。默认需要用户名与密码。",[184,398,850,56],true)?;
    label(hwnd, 904, p, "用户名", [184, 468, 120, 25], false)?;
    edit(hwnd, GUI_USER, p, [310, 462, 570, 32], 0, true)?;
    label(hwnd, 905, p, "密码", [184, 514, 120, 25], false)?;
    edit(
        hwnd,
        GUI_PASS,
        p,
        [310, 508, 570, 32],
        ES_PASSWORD as u32,
        true,
    )?;
    label(hwnd,906,p,"密码通过网页认证，不放入 URL。保存不会自动启动或重启。\r\n启停使用已保存配置；取消输入不会撤销运行操作。",[184,556,870,58],true)?;
    form_footer(
        hwnd,
        p,
        SERVICE_SAVE,
        SERVICE_CANCEL,
        SERVICE_REBASE,
        SERVICE_NOTE,
    )
}
fn form_footer(
    hwnd: HWND,
    p: Option<Page>,
    save: u16,
    cancel: u16,
    rebase: u16,
    note: u16,
) -> Result<(), String> {
    label(hwnd, note, p, "", [184, 608, 870, 48], true)?;
    button(hwnd, save, p, "保存更改", [184, 668, 144, 34])?;
    button(hwnd, cancel, p, "取消 / 放弃输入", [340, 668, 156, 34])?;
    button(hwnd, rebase, p, "保留输入，更新版本", [508, 668, 194, 34])?;
    Ok(())
}
fn build_settings(hwnd: HWND) -> Result<(), String> {
    let p = Some(Page::Settings);
    label(
        hwnd,
        907,
        p,
        "rclone 可执行文件（留空自动发现；明确无效路径不会回退）",
        [184, 142, 830, 26],
        true,
    )?;
    edit(hwnd, EXE, p, [184, 170, 650, 30], 0, true)?;
    button(hwnd, BROWSE_EXE, p, "浏览…", [850, 170, 100, 30])?;
    label(
        hwnd,
        908,
        p,
        "rclone.conf（留空使用 RCLONE_CONFIG 或标准位置）",
        [184, 210, 830, 26],
        true,
    )?;
    edit(hwnd, CONF, p, [184, 238, 650, 30], 0, true)?;
    button(hwnd, BROWSE_CONF, p, "浏览…", [850, 238, 100, 30])?;
    for (id, s, x, w) in [
        (DETECT, "重新检测", 184, 130),
        (AUTO_DISCOVER, "自动发现", 324, 130),
        (OPEN_CONF, "打开 rclone 配置", 464, 180),
    ] {
        button(hwnd, id, p, s, [x, 282, w, 32])?;
    }
    readonly(hwnd, ENVIRONMENT, p, [184, 326, 870, 96], true, false)?;
    check(
        hwnd,
        STARTUP,
        p,
        "Windows 登录时启动托盘（保存后更新当前用户启动项）",
        [184, 436, 850, 28],
    )?;
    check(
        hwnd,
        AUTO_MOUNT,
        p,
        "开机启动后自动挂载（仅 --autostart，且逐项允许时生效）",
        [184, 472, 850, 28],
    )?;
    label(
        hwnd,
        909,
        p,
        "挂载延迟（秒，0–86400）",
        [184, 516, 270, 26],
        false,
    )?;
    edit(hwnd, DELAY, p, [458, 510, 150, 30], ES_NUMBER as u32, false)?;
    label(hwnd, 910, p, "主题", [184, 562, 90, 26], false)?;
    combo(
        hwnd,
        THEME,
        p,
        [280, 554, 178, 200],
        &["跟随系统", "浅色", "深色"],
        false,
    )?;
    check(
        hwnd,
        REDUCE_MOTION,
        p,
        "减少动态与材质效果",
        [480, 558, 540, 30],
    )?;
    form_footer(
        hwnd,
        p,
        SETTINGS_SAVE,
        SETTINGS_CANCEL,
        SETTINGS_REBASE,
        SETTINGS_NOTE,
    )
}
fn build_about(hwnd: HWND) -> Result<(), String> {
    let p = Some(Page::About);
    for (id, s, x, w) in [
        (APP_CONFIG, "应用配置", 184, 130),
        (ABOUT_CONF, "rclone 配置", 324, 140),
        (LOG_DIR, "日志目录", 474, 130),
        (ABOUT_REFRESH, "刷新诊断", 614, 130),
    ] {
        button(hwnd, id, p, s, [x, 142, w, 32])?;
    }
    for (id, s, x, w) in [
        (HELP, "rclone 官方文档", 184, 170),
        (WINFSP_HELP, "WinFsp 安装与文档", 364, 200),
        (CONF_DIR, "rclone 配置目录", 574, 180),
    ] {
        button(hwnd, id, p, s, [x, 186, w, 32])?;
    }
    readonly(hwnd, UPDATE_STATUS, p, [184, 238, 870, 104], true, false)?;
    button(hwnd, CHECK_UPDATE, p, "检查更新", [184, 350, 130, 32])?;
    button(hwnd, OPEN_UPDATE, p, "打开下载页面", [324, 350, 170, 32])?;
    readonly(hwnd, DIAGNOSTICS, p, [184, 390, 870, 240], true, true)?;
    button(hwnd, COPY, p, "复制诊断信息", [184, 650, 170, 34])?;
    button(
        hwnd,
        ABOUT_EXIT,
        p,
        "退出应用并停止本应用任务",
        [364, 650, 300, 34],
    )?;
    Ok(())
}
fn build_editor(hwnd: HWND) -> Result<(), String> {
    label(
        hwnd,
        911,
        None,
        "挂载配置 · 保存不会启动；自动挂载还需启用全局策略。",
        [20, 16, 600, 46],
        true,
    )?;
    for (label_id, id, s, y) in [
        (912, NAME, "名称", 74),
        (913, REMOTE, "Remote · remote:path", 142),
        (914, POINT, "挂载位置 · 明确盘符或空目录", 210),
        (915, VOLNAME, "卷标（可选）", 330),
    ] {
        label(hwnd, label_id, None, s, [20, y, 590, 24], true)?;
        if id == REMOTE || id == POINT {
            combo(hwnd, id, None, [20, y + 28, 570, 220], &[], true)?;
        } else {
            edit(hwnd, id, None, [20, y + 28, 570, 30], 0, true)?;
        }
    }
    button(hwnd, BROWSE_POINT, None, "选择空目录…", [20, 286, 180, 32])?;
    button(
        hwnd,
        EDITOR_CONF,
        None,
        "打开 rclone 配置",
        [210, 286, 190, 32],
    )?;
    label(hwnd, 916, None, "VFS 缓存模式", [20, 404, 160, 25], false)?;
    combo(
        hwnd,
        VFS,
        None,
        [200, 400, 180, 200],
        &["off", "minimal", "writes", "full"],
        false,
    )?;
    check(
        hwnd,
        NETWORK,
        None,
        "网络驱动器模式（仅明确盘符）",
        [20, 448, 590, 28],
    )?;
    check(
        hwnd,
        READ_ONLY,
        None,
        "只读 · 禁止写入远端",
        [20, 484, 590, 28],
    )?;
    check(
        hwnd,
        MOUNT_AUTO,
        None,
        "开机自动挂载此项",
        [20, 520, 590, 28],
    )?;
    readonly(hwnd, EDITOR_NOTE, None, [20, 562, 600, 66], true, false)?;
    button(hwnd, EDITOR_SAVE, None, "保存挂载", [20, 644, 124, 34])?;
    button(hwnd, EDITOR_CANCEL, None, "取消", [154, 644, 100, 34])?;
    button(
        hwnd,
        EDITOR_REBASE,
        None,
        "保留输入，更新版本",
        [264, 644, 194, 34],
    )?;
    button(
        hwnd,
        EDITOR_RELOAD,
        None,
        "重新载入配置",
        [468, 644, 150, 34],
    )?;
    let drives = unsafe { GetLogicalDrives() };
    for letter in b'C'..=b'Z' {
        if drives & (1 << (letter - b'A')) == 0 {
            unsafe {
                SendMessageW(
                    handle(hwnd, POINT),
                    CB_ADDSTRING,
                    0,
                    wide(&format!("{}:", letter as char)).as_ptr() as isize,
                );
            }
        }
    }
    Ok(())
}

fn layout(hwnd: HWND) {
    let (dpi, page, editor, collapsed) = match with_view(hwnd, |v| {
        (v.dpi.get(), v.model.borrow().page, !v.owner.is_null(), v.collapsed.get())
    }) { Some(x) => x, None => return };
    let mut r = unsafe { std::mem::zeroed::<RECT>() };
    unsafe { GetClientRect(hwnd, &mut r); }
    let scale = |n: i32| n * dpi as i32 / 96;
    let client_w = r.right * 96 / dpi as i32;
    let client_h = r.bottom * 96 / dpi as i32;
    let compact = !editor && (collapsed || client_w < 1000);
    let sidebar = if editor { 0 } else if compact { 64 } else { 192 };
    let width = client_w.max(if editor { 650 } else if compact { 680 } else { 1000 });
    let left = sidebar + 24;
    let body_w = width - left - 24;
    let narrow = !editor && body_w < 760;
    let height = client_h.max(if editor { 700 } else if narrow {
        match page { Page::Mounts => 1140, Page::Service => 900, Page::Settings => 940, Page::About => 940 }
    } else { 784 });
    let (sx, sy) = with_view(hwnd, |v| {
        v.compact.set(compact);
        let sx = v.scroll_x.get().clamp(0, (scale(width) - r.right).max(0));
        let sy = v.scroll_y.get().clamp(0, (scale(height) - r.bottom).max(0));
        v.scroll_x.set(sx);
        v.scroll_y.set(sy);
        (sx, sy)
    }).unwrap();
    for (bar, range, extent, pos) in [
        (SB_HORZ, scale(width), r.right, sx), (SB_VERT, scale(height), r.bottom, sy),
    ] {
        let info = SCROLLINFO {
            cbSize: std::mem::size_of::<SCROLLINFO>() as u32,
            fMask: SIF_RANGE | SIF_PAGE | SIF_POS,
            nMin: 0, nMax: range - 1, nPage: extent.max(0) as u32, nPos: pos, nTrackPos: 0,
        };
        unsafe { SetScrollInfo(hwnd, bar, &info, 1); }
    }
    with_view(hwnd, |v| {
        for c in v.controls.borrow().iter() {
            let [mut x, mut y, mut w, mut h] = c.rect;
            let mut fixed = false;
            let mut visible = c.page.is_none() || c.page == Some(page);
            if editor {
                if c.grow_x { w += width - 650; }
                if c.grow_y { h += height - 700; }
            } else if matches!(c.id, NAV_MOUNTS | NAV_SERVICE | NAV_SETTINGS | NAV_ABOUT | TOGGLE_NAV | EXIT | 900 | 922 | 923) {
                fixed = true;
                if compact {
                    x = 8; w = 48;
                    if matches!(c.id, 900 | 922 | 923) { visible = false; }
                }
                if c.id == EXIT { y = client_h - if compact { 60 } else { 128 }; }
                if c.id == 900 { y = client_h - 80; }
            } else {
                // Legacy form coordinates share the same content origin; mount
                // controls and the frame already use the new 216px origin.
                let origin = if c.page.is_some() && c.page != Some(Page::Mounts) { 184 } else { 216 };
                x = left + x - origin;
                if c.grow_x { w = (body_w - (c.rect[0] - origin) - (870 - c.rect[2])).max(80); }
                if c.page.is_some() && c.page != Some(Page::Mounts) { y += 42; }
                if c.grow_y { h += height - 784; }
                match c.id {
                    920 | 921 => w = if narrow { body_w } else { body_w - 328 },
                    RELOAD => { x = width - 326; y = if narrow { 96 } else { 28 }; }
                    CLOSE => { x = width - 158; y = if narrow { 96 } else { 28 }; }
                    MESSAGE => { w = body_w; if narrow { y = 144; } }
                    FOOTER => { x = left; y = client_h - 32; w = body_w; h = 32; fixed = true; }
                    FILTER => { x = if narrow { left } else { width - 444 }; if narrow { y = 252; } }
                    CLEAR_FILTER => { x = if narrow { left + 180 } else { width - 264 }; if narrow { y = 252; } }
                    SEARCH => { w = if narrow { body_w } else { body_w - 430 }; if narrow { y = 208; } }
                    ADD | START_ALL | STOP_ALL | REFRESH => {
                        if narrow {
                            let i = match c.id { ADD => 0, START_ALL => 1, STOP_ALL => 2, _ => 3 };
                            let cell = (body_w - 24) / 3;
                            x = left + (i % 3) * (cell + 12); y = 296 + (i / 3) * 44; w = cell;
                        }
                    }
                    SUMMARY => { w = body_w; if narrow { y = 392; } }
                    MOUNT_LIST => {
                        w = if narrow { body_w } else { body_w - 370 };
                        if narrow { y = 448; h = 228; }
                    }
                    MOUNT_DETAIL => {
                        x = if narrow { left } else { width - 374 };
                        if narrow { y = 692; w = body_w; h = 244; }
                    }
                    START | STOP | OPEN | LOG | EDIT | DELETE => {
                        if narrow {
                            let i = match c.id { START => 0, STOP => 1, OPEN => 2, LOG => 3, EDIT => 4, _ => 5 };
                            let cell = (body_w - 24) / 3;
                            x = left + (i % 3) * (cell + 12); y = 952 + (i / 3) * 44; w = cell;
                        } else { y += height - 784; }
                    }
                    902 => { w = body_w; y = if narrow { 1048 } else { y + height - 784 }; }
                    BROWSE_EXE | BROWSE_CONF => x = width - 124,
                    EXE | CONF => w = body_w - 116,
                    GUI_USER | GUI_PASS => w = body_w - 126,
                    GUI_AUTO | GUI_NO_AUTH | STARTUP | AUTO_MOUNT => w = body_w,
                    GUI_START | GUI_STOP | GUI_OPEN | GUI_LOG => {
                        if narrow {
                            let i = match c.id { GUI_START => 0, GUI_STOP => 1, GUI_OPEN => 2, _ => 3 };
                            let cell = (body_w - 36) / 4;
                            x = left + i * (cell + 12); w = cell;
                        }
                    }
                    REDUCE_MOTION => { if narrow { x = left; y += 44; w = body_w; } }
                    SERVICE_NOTE | SETTINGS_NOTE => { if narrow { y += 48; } }
                    SERVICE_SAVE | SERVICE_CANCEL | SERVICE_REBASE | SETTINGS_SAVE | SETTINGS_CANCEL | SETTINGS_REBASE => {
                        if narrow {
                            let i = match c.id { SERVICE_SAVE | SETTINGS_SAVE => 0, SERVICE_CANCEL | SETTINGS_CANCEL => 1, _ => 2 };
                            let cell = (body_w - 24) / 3;
                            x = left + i * (cell + 12); w = cell; y += 48;
                        }
                    }
                    COPY | ABOUT_EXIT => { y += height - 784; if narrow && c.id == ABOUT_EXIT { x = left + 184; w = body_w - 184; } }
                    APP_CONFIG | ABOUT_CONF | LOG_DIR | ABOUT_REFRESH | HELP | WINFSP_HELP | CONF_DIR | DETECT | AUTO_DISCOVER | OPEN_CONF => {
                        if narrow {
                            let (i, row) = match c.id {
                                APP_CONFIG => (0, 0), ABOUT_CONF => (1, 0), LOG_DIR => (2, 0), ABOUT_REFRESH => (3, 0),
                                HELP => (0, 1), WINFSP_HELP => (1, 1), CONF_DIR => (2, 1),
                                DETECT => (0, 2), AUTO_DISCOVER => (1, 2), _ => (2, 2),
                            };
                            let columns = if row == 0 { 4 } else { 3 };
                            let cell = (body_w - (columns - 1) * 12) / columns;
                            x = left + i * (cell + 12); w = cell;
                        }
                    }
                    _ => {}
                }
                if narrow && c.page.is_some() && !matches!(c.id,
                    SEARCH | FILTER | CLEAR_FILTER | ADD | START_ALL | STOP_ALL | REFRESH | SUMMARY | MOUNT_LIST | MOUNT_DETAIL | START | STOP | OPEN | LOG | EDIT | DELETE | 902) {
                    y += 48;
                }
            }
            unsafe {
                MoveWindow(c.hwnd, scale(x) - if fixed { 0 } else { sx },
                    scale(y) - if fixed { 0 } else { sy }, scale(w), scale(h), 1);
                ShowWindow(c.hwnd, if visible { SW_SHOW } else { SW_HIDE });
            }
        }
    });
    if !editor {
        // Keep the opaque status strip above every scrollable child surface.
        unsafe { SetWindowPos(handle(hwnd, FOOTER), HWND_TOP, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE); }
    }
    unsafe { RedrawWindow(hwnd, null(), null_mut(), RDW_INVALIDATE | RDW_ERASE | RDW_ALLCHILDREN); }
}
fn scroll(hwnd: HWND, bar: i32, action: i32) {
    let mut info = unsafe { std::mem::zeroed::<SCROLLINFO>() };
    info.cbSize = std::mem::size_of::<SCROLLINFO>() as u32;
    info.fMask = SIF_ALL;
    unsafe {
        GetScrollInfo(hwnd, bar, &mut info);
    }
    let line = with_view(hwnd, |v| 32 * v.dpi.get() as i32 / 96).unwrap_or(32);
    let pos = match action {
        SB_LINEUP => info.nPos - line,
        SB_LINEDOWN => info.nPos + line,
        SB_PAGEUP => info.nPos - info.nPage as i32,
        SB_PAGEDOWN => info.nPos + info.nPage as i32,
        SB_THUMBPOSITION | SB_THUMBTRACK => info.nTrackPos,
        SB_TOP => info.nMin,
        SB_BOTTOM => info.nMax,
        _ => return,
    }
    .clamp(0, (info.nMax + 1 - info.nPage as i32).max(0));
    with_view(hwnd, |v| {
        if bar == SB_VERT {
            v.scroll_y.set(pos)
        } else {
            v.scroll_x.set(pos)
        }
    });
    layout(hwnd);
    unsafe {
        InvalidateRect(hwnd, null(), 1);
    }
}
fn ensure_visible(hwnd: HWND, child: HWND) {
    if child.is_null() || unsafe { IsChild(hwnd, child) } == 0 {
        return;
    }
    let mut rect = unsafe { std::mem::zeroed::<RECT>() };
    let mut client = unsafe { std::mem::zeroed::<RECT>() };
    unsafe {
        GetWindowRect(child, &mut rect);
        GetClientRect(hwnd, &mut client);
        MapWindowPoints(
            null_mut(),
            hwnd,
            (&mut rect as *mut RECT).cast::<POINT>(),
            2,
        );
    }
    let dx = if rect.left < 0 {
        rect.left
    } else if rect.right > client.right {
        rect.right - client.right
    } else {
        0
    };
    let dy = if rect.top < 0 {
        rect.top
    } else if rect.bottom > client.bottom {
        rect.bottom - client.bottom
    } else {
        0
    };
    if dx != 0 || dy != 0 {
        with_view(hwnd, |v| {
            v.scroll_x.set((v.scroll_x.get() + dx).max(0));
            v.scroll_y.set((v.scroll_y.get() + dy).max(0));
        });
        layout(hwnd);
    }
}
/// The ready-made system icon font: "Segoe Fluent Icons" on Windows 11, with
/// "Segoe MDL2 Assets" as the Windows 10 fallback. Both carry the same
/// codepoints, so `style::glyph` needs a single mapping.
unsafe fn icon_font(dpi: u32) -> HFONT {
    let height = -(20 * dpi as i32 / 96);
    for face in ["Segoe Fluent Icons", "Segoe MDL2 Assets"] {
        let name = wide(face);
        let font = CreateFontW(
            height, 0, 0, 0, FW_NORMAL as i32, 0, 0, 0,
            DEFAULT_CHARSET as u32, OUT_DEFAULT_PRECIS as u32, CLIP_DEFAULT_PRECIS as u32,
            CLEARTYPE_QUALITY as u32, DEFAULT_PITCH as u32 | FF_DONTCARE as u32,
            name.as_ptr(),
        );
        if font.is_null() { continue; }
        let dc = GetDC(null_mut());
        if dc.is_null() {
            DeleteObject(font);
            continue;
        }
        let previous = SelectObject(dc, font);
        let mut actual = [0u16; 64];
        let length = GetTextFaceW(dc, actual.len() as i32, actual.as_mut_ptr());
        SelectObject(dc, previous);
        ReleaseDC(null_mut(), dc);
        let selected = length > 1
            && String::from_utf16_lossy(&actual[..length as usize - 1]).eq_ignore_ascii_case(face);
        if selected { return font; }
        DeleteObject(font);
    }
    null_mut()
}

/// Sidebar brand mark created from the ICO bytes; the caller owns the handle.
unsafe fn mark_icon(dpi: u32) -> HICON {
    let size = 24 * dpi as i32 / 96;
    crate::host::icon_from_ico(include_bytes!("../assets/mark.ico"), size).unwrap_or(null_mut())
}

fn apply_font(hwnd: HWND) {
    let dpi = with_view(hwnd, |v| v.dpi.get()).unwrap_or(96);
    let make_font = |size: i32, weight: i32| unsafe {
        CreateFontW(-(size * dpi as i32 / 96), 0, 0, 0, weight, 0, 0, 0,
            DEFAULT_CHARSET as u32, OUT_DEFAULT_PRECIS as u32, CLIP_DEFAULT_PRECIS as u32,
            CLEARTYPE_QUALITY as u32, DEFAULT_PITCH as u32 | FF_DONTCARE as u32,
            wide("Microsoft YaHei UI").as_ptr())
    };
    let font = make_font(14, FW_NORMAL as i32);
    let title = make_font(24, FW_SEMIBOLD as i32);
    let small = make_font(12, FW_NORMAL as i32);
    if font.is_null() || title.is_null() || small.is_null() {
        for object in [font, title, small] { if !object.is_null() { unsafe { DeleteObject(object); } } }
        return;
    }
    let (icon, mark) = unsafe { (icon_font(dpi), mark_icon(dpi)) };
    let old = with_view(hwnd, |v| [v.font.replace(font), v.title_font.replace(title), v.small_font.replace(small)]).unwrap();
    // A failed fallback never replaces a working object; the old handle stays.
    let old_icon = with_view(hwnd, |v| if icon.is_null() { null_mut() } else { v.icon_font.replace(icon) }).unwrap();
    let old_mark = with_view(hwnd, |v| if mark.is_null() { null_mut() } else { v.mark_icon.replace(mark) }).unwrap();
    let (big, small) = (
        crate::host::icon_from_ico(include_bytes!("../assets/icon.ico"), 32 * dpi as i32 / 96).unwrap_or(null_mut()),
        crate::host::icon_from_ico(include_bytes!("../assets/icon.ico"), 16 * dpi as i32 / 96).unwrap_or(null_mut()),
    );
    let previous = with_view(hwnd, |v| (
        if big.is_null() { null_mut() } else { v.big_icon.replace(big) },
        if small.is_null() { null_mut() } else { v.small_icon.replace(small) },
    )).unwrap();
    unsafe {
        if !old_icon.is_null() { DeleteObject(old_icon); }
        if !old_mark.is_null() { DestroyIcon(old_mark); }
        // DPI-exact frames keep the title bar and taskbar crisp; without this the
        // shell scales the embedded EXE icon.
        if !big.is_null() {
            SendMessageW(hwnd, WM_SETICON, ICON_BIG as WPARAM, big as LPARAM);
            if !previous.0.is_null() { DestroyIcon(previous.0); }
        }
        if !small.is_null() {
            SendMessageW(hwnd, WM_SETICON, ICON_SMALL as WPARAM, small as LPARAM);
            if !previous.1.is_null() { DestroyIcon(previous.1); }
        }
    }
    with_view(hwnd, |v| {
        for c in v.controls.borrow().iter() {
            unsafe { SendMessageW(c.hwnd, WM_SETFONT, font as usize, 1); }
        }
    });
    if !handle(hwnd, MOUNT_LIST).is_null() {
        unsafe { SendMessageW(handle(hwnd, MOUNT_LIST), LB_SETITEMHEIGHT, 0, (76 * dpi / 96) as isize); }
    }
    for object in old { if !object.is_null() { unsafe { DeleteObject(object); } } }
}
fn system_dark() -> bool {
    unsafe {
        let mut value = 1u32;
        let mut size = 4u32;
        RegGetValueW(
            HKEY_CURRENT_USER,
            wide("Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize").as_ptr(),
            wide("AppsUseLightTheme").as_ptr(),
            RRF_RT_REG_DWORD,
            null_mut(),
            (&mut value as *mut u32).cast(),
            &mut size,
        ) == ERROR_SUCCESS
            && value == 0
    }
}
fn apply_theme(hwnd: HWND) {
    let (theme, reduce_motion, child) = match with_view(hwnd, |v| {
        let m = v.model.borrow();
        let theme = if v.owner.is_null() {
            m.settings.values[&THEME].text().to_owned()
        } else {
            m.snapshot.config.theme.clone()
        };
        let reduce_motion = if v.owner.is_null() {
            m.settings.values[&REDUCE_MOTION].checked()
        } else {
            m.snapshot.config.reduce_motion
        };
        (theme, reduce_motion, v.child.get())
    }) {
        Some(x) => x,
        None => return,
    };
    if with_view(hwnd, |v| v.motion.get() != Some(reduce_motion)).unwrap_or(false) {
        let disabled = reduce_motion as i32;
        let result = unsafe {
            DwmSetWindowAttribute(
                hwnd,
                windows_sys::Win32::Graphics::Dwm::DWMWA_TRANSITIONS_FORCEDISABLED as u32,
                (&disabled as *const i32).cast(),
                std::mem::size_of::<i32>() as u32,
            )
        };
        with_view(hwnd, |v| v.motion.set(Some(reduce_motion)));
        // Native title-bar material is optional; content stays opaque/readable
        // on older Windows, remote sessions and reduced-effects systems.
        let backdrop = if reduce_motion { 1i32 } else { 2i32 };
        unsafe { DwmSetWindowAttribute(hwnd, 38, (&backdrop as *const i32).cast(), 4); }
        if result < 0 {
            feedback(
                hwnd,
                format!("当前桌面不支持原生窗口转场控制（HRESULT {result:#x}）。"),
            );
        }
    }
    let dark = match theme.as_str() {
        "dark" => true,
        "light" => false,
        _ => system_dark(),
    };
    let needs =
        with_view(hwnd, |v| v.brush.get().is_null() || v.dark.get() != dark).unwrap_or(false);
    if needs {
        let p = style::palette(dark);
        with_view(hwnd, |v| {
            v.dark.set(dark);
            for (slot, color) in [(&v.brush, p.bg), (&v.panel_brush, p.panel), (&v.input_brush, p.input), (&v.sidebar_brush, p.sidebar)] {
                let brush = unsafe { CreateSolidBrush(color) };
                if !brush.is_null() {
                    let old = slot.replace(brush);
                    if !old.is_null() { unsafe { DeleteObject(old); } }
                }
            }
            let native_theme = wide(if dark { "DarkMode_Explorer" } else { "Explorer" });
            let empty = [0u16];
            for c in v.controls.borrow().iter() {
                unsafe { SetWindowTheme(c.hwnd, if is_check(c.id) { empty.as_ptr() } else { native_theme.as_ptr() }, if is_check(c.id) { empty.as_ptr() } else { null() }); }
            }
        });
        let flag = dark as i32;
        let corner = 2i32;
        unsafe {
            DwmSetWindowAttribute(hwnd, 20, (&flag as *const i32).cast(), 4);
            DwmSetWindowAttribute(hwnd, 33, (&corner as *const i32).cast(), 4);
        }
        unsafe {
            RedrawWindow(
                hwnd,
                null(),
                null_mut(),
                RDW_INVALIDATE | RDW_ERASE | RDW_ALLCHILDREN,
            );
        }
    }
    if !child.is_null() {
        apply_theme(child);
    }
}
fn is_check(id: u16) -> bool {
    matches!(
        id,
        GUI_AUTO
            | GUI_NO_AUTH
            | STARTUP
            | AUTO_MOUNT
            | REDUCE_MOTION
            | NETWORK
            | READ_ONLY
            | MOUNT_AUTO
    )
}

fn service_values(c: &AppConfig) -> Values {
    BTreeMap::from([
        (GUI_USER, Value::Text(c.gui_user.clone())),
        (GUI_PASS, Value::Text(c.gui_pass.clone())),
        (GUI_NO_AUTH, Value::Check(c.gui_no_auth)),
        (GUI_AUTO, Value::Check(c.gui_auto_start)),
    ])
}
fn settings_values(c: &AppConfig) -> Values {
    BTreeMap::from([
        (EXE, Value::Text(c.rclone_path.clone())),
        (CONF, Value::Text(c.rclone_conf.clone())),
        (STARTUP, Value::Check(c.startup_enabled)),
        (AUTO_MOUNT, Value::Check(c.auto_mount_on_boot)),
        (DELAY, Value::Text(c.mount_delay_seconds.to_string())),
        (THEME, Value::Text(c.theme.clone())),
        (REDUCE_MOTION, Value::Check(c.reduce_motion)),
    ])
}
fn mount_values(c: &MountConfig) -> Values {
    BTreeMap::from([
        (NAME, Value::Text(c.name.clone())),
        (REMOTE, Value::Text(c.remote.clone())),
        (POINT, Value::Text(c.mount_point.clone())),
        (VOLNAME, Value::Text(c.volname.clone())),
        (VFS, Value::Text(c.vfs_cache_mode.clone())),
        (NETWORK, Value::Check(c.network_mode)),
        (READ_ONLY, Value::Check(c.read_only)),
        (MOUNT_AUTO, Value::Check(c.auto_mount)),
    ])
}
fn draft_values(hwnd: HWND) -> Values {
    with_view(hwnd, |v| {
        let m = v.model.borrow();
        if let Some((_, d)) = &m.editor {
            d.values.clone()
        } else {
            let mut values = m.settings.values.clone();
            values.extend(m.service.values.clone());
            values
        }
    })
    .unwrap_or_default()
}
fn sync_inputs(hwnd: HWND) {
    let values = draft_values(hwnd);
    with_view(hwnd, |v| v.loading.set(true));
    for (id, value) in values {
        let h = handle(hwnd, id);
        match value {
            Value::Check(b) => unsafe {
                SendMessageW(
                    h,
                    BM_SETCHECK,
                    if b { BST_CHECKED } else { BST_UNCHECKED } as usize,
                    0,
                );
            },
            Value::Text(s) => {
                if id == THEME {
                    let index = match s.as_str() {
                        "light" => 1,
                        "dark" => 2,
                        _ => 0,
                    };
                    unsafe {
                        SendMessageW(h, CB_SETCURSEL, index, 0);
                    }
                } else if id == VFS {
                    let index = match s.as_str() {
                        "off" => 0,
                        "minimal" => 1,
                        "full" => 3,
                        _ => 2,
                    };
                    unsafe {
                        SendMessageW(h, CB_SETCURSEL, index, 0);
                    }
                } else {
                    set_text(h, &s);
                }
            }
        }
    }
    with_view(hwnd, |v| v.loading.set(false));
}
fn capture(hwnd: HWND, id: u16) -> bool {
    if with_view(hwnd, |v| v.loading.get()).unwrap_or(true) {
        return false;
    }
    let previous = with_view(hwnd, |v| {
        let m = v.model.borrow();
        if let Some((_, d)) = &m.editor {
            d.values.get(&id).cloned()
        } else {
            m.settings
                .values
                .get(&id)
                .or_else(|| m.service.values.get(&id))
                .cloned()
        }
    })
    .flatten();
    let previous = match previous {
        Some(p) => p,
        None => return false,
    };
    let h = handle(hwnd, id);
    let value = match previous {
        Value::Check(_) => {
            Value::Check(unsafe { SendMessageW(h, BM_GETCHECK, 0, 0) } == BST_CHECKED as isize)
        }
        Value::Text(_) => Value::Text(if id == THEME {
            match unsafe { SendMessageW(h, CB_GETCURSEL, 0, 0) } {
                1 => "light",
                2 => "dark",
                _ => "system",
            }
            .into()
        } else if id == VFS {
            match unsafe { SendMessageW(h, CB_GETCURSEL, 0, 0) } {
                0 => "off",
                1 => "minimal",
                3 => "full",
                _ => "writes",
            }
            .into()
        } else {
            text(h)
        }),
    };
    with_view(hwnd, |v| {
        let mut m = v.model.borrow_mut();
        let d = if m.editor.is_some() {
            &mut m.editor.as_mut().unwrap().1
        } else if m.settings.values.contains_key(&id) {
            &mut m.settings
        } else {
            &mut m.service
        };
        if !d.pending {
            d.values.insert(id, value);
        }
    });
    true
}
fn capture_combo_selection(hwnd: HWND, id: u16) {
    if !matches!(id, REMOTE | POINT) {
        return;
    }
    let h = handle(hwnd, id);
    let index = unsafe { SendMessageW(h, CB_GETCURSEL, 0, 0) };
    if index < 0 {
        return;
    }
    let len = unsafe { SendMessageW(h, CB_GETLBTEXTLEN, index as usize, 0) };
    if len < 0 {
        return;
    }
    let mut buf = vec![0u16; len as usize + 1];
    unsafe {
        SendMessageW(h, CB_GETLBTEXT, index as usize, buf.as_mut_ptr() as isize);
    }
    with_view(hwnd, |v| v.loading.set(true));
    set_text(h, &String::from_utf16_lossy(&buf[..len as usize]));
    with_view(hwnd, |v| v.loading.set(false));
}
fn render_note(d: &Draft, s: &Snapshot) -> String {
    let mut note = if d.pending {
        "正在保存，请等待结果。".into()
    } else if !d.error.is_empty() {
        d.error.clone()
    } else if d.dirty() {
        "有未保存输入；仅保存此页。".into()
    } else {
        "输入与已保存设置一致。".into()
    };
    if !s.config_error.is_empty() {
        note.push_str(" 配置读取失败：修复文件后重新载入，输入保留。");
    }
    if !d.pending && d.revision != s.revision {
        note.push_str(" 配置版本已变化；可保留输入并更新版本后再保存。");
    }
    note
}
fn refresh(hwnd: HWND, snapshot: Arc<Snapshot>) {
    if with_view(hwnd, |v| v.rendering.replace(true)).unwrap_or(true) {
        return;
    }
    with_view(hwnd, |v| {
        let mut m = v.model.borrow_mut();
        if m.editor.is_none() {
            if !m.service.dirty() && !m.service.pending {
                let values = service_values(&snapshot.config);
                if m.service.values != values {
                    m.service.values = values.clone();
                    m.service.baseline = values;
                }
                m.service.revision = snapshot.revision;
            }
            if !m.settings.dirty() && !m.settings.pending {
                let values = settings_values(&snapshot.config);
                if m.settings.values != values {
                    m.settings.values = values.clone();
                    m.settings.baseline = values;
                }
                m.settings.revision = snapshot.revision;
            }
        }
        m.snapshot = snapshot.clone();
    });
    sync_inputs(hwnd);
    apply_theme(hwnd);
    let editor = with_view(hwnd, |v| !v.owner.is_null()).unwrap_or(false);
    if editor {
        render_editor(hwnd);
    } else {
        render_root(hwnd);
    }
    with_view(hwnd, |v| v.rendering.set(false));
}
fn render_root(hwnd: HWND) {
    let (snapshot, service, settings, message, child) = with_view(hwnd, |v| {
        let m = v.model.borrow();
        (
            m.snapshot.clone(),
            m.service.clone(),
            m.settings.clone(),
            m.message.clone(),
            v.child.get(),
        )
    })
    .unwrap();
    let s = &*snapshot;
    let page = with_view(hwnd, |v| v.model.borrow().page).unwrap_or(Page::Mounts);
    let (title, subtitle) = match page {
        Page::Mounts => ("挂载管理", "连接远端，让文件触手可及。"),
        Page::Service => ("rclone GUI", "本机服务与认证 · 使用已保存的配置启动。"),
        Page::Settings => ("设置", "环境、启动策略与外观 · 更改后记得保存。"),
        Page::About => ("关于与诊断", "版本、更新与运行环境 · 所有诊断来自实际状态。"),
    };
    set_text(handle(hwnd, 920), title);
    set_text(handle(hwnd, 921), subtitle);
    set_text(
        handle(hwnd, MESSAGE),
        &if s.config_error.is_empty() {
            message
        } else {
            format!("配置读取失败：{}\r\n{}", s.config_error, message)
        },
    );
    set_text(
        handle(hwnd, FOOTER),
        &format!(
            "{} · GUI {} · {}",
            s.environment.version,
            s.gui.state.label(),
            if s.closing {
                "正在退出"
            } else if s.busy.is_empty() {
                "空闲"
            } else {
                "操作进行中"
            }
        ),
    );
    set_text(
        handle(hwnd, GUI_STATUS),
        &format!(
            "服务：{}{}\r\n{}\r\n已保存：{}，{}。草稿不影响运行操作。",
            s.gui.state.label(),
            s.gui.pid.map(|p| format!(" · PID {p}")).unwrap_or_default(),
            s.gui.message,
            if s.config.gui_no_auth {
                "免认证"
            } else {
                "需要认证（凭据不显示）"
            },
            if s.config.gui_auto_start {
                "随应用启动"
            } else {
                "手动启动"
            }
        ),
    );
    set_text(handle(hwnd, ENVIRONMENT), &environment_text(s));
    set_text(handle(hwnd, UPDATE_STATUS), &update_text(s));
    set_text(handle(hwnd, DIAGNOSTICS), &diagnostics(s));
    set_text(handle(hwnd, SERVICE_NOTE), &render_note(&service, s));
    set_text(handle(hwnd, SETTINGS_NOTE), &render_note(&settings, s));
    let busy = s.closing
        || s.busy.contains("settings")
        || service.pending
        || settings.pending
        || with_view(hwnd, |v| v.model.borrow().reload_request.is_some()).unwrap_or(false);
    let blocked = busy || !s.config_error.is_empty();
    for (d, save, cancel, rebase) in [
        (&service, SERVICE_SAVE, SERVICE_CANCEL, SERVICE_REBASE),
        (&settings, SETTINGS_SAVE, SETTINGS_CANCEL, SETTINGS_REBASE),
    ] {
        enable(handle(hwnd, save), d.dirty() && !blocked);
        enable(handle(hwnd, cancel), !busy);
        enable(
            handle(hwnd, rebase),
            d.dirty() && d.revision != s.revision && !blocked,
        );
    }
    for id in settings.values.keys() {
        enable(handle(hwnd, *id), !settings.pending && !s.closing);
    }
    for id in [BROWSE_EXE, BROWSE_CONF, AUTO_DISCOVER] {
        enable(handle(hwnd, id), !settings.pending && !s.closing);
    }
    for id in service.values.keys() {
        enable(handle(hwnd, *id), !service.pending && !s.closing);
    }
    for id in [GUI_USER, GUI_PASS] {
        enable(
            handle(hwnd, id),
            !service.pending && !s.closing && !service.values[&GUI_NO_AUTH].checked(),
        );
    }
    let gui_busy = s.closing || s.busy.contains("gui");
    enable(
        handle(hwnd, GUI_START),
        !gui_busy
            && s.gui.pid.is_none()
            && matches!(s.gui.state, RunState::Stopped | RunState::Failed),
    );
    enable(
        handle(hwnd, GUI_STOP),
        !gui_busy && (s.gui.pid.is_some() || s.gui.state != RunState::Stopped),
    );
    enable(
        handle(hwnd, GUI_OPEN),
        !gui_busy && s.gui.state == RunState::Ready,
    );
    enable(
        handle(hwnd, GUI_LOG),
        s.gui.log_path.is_some() && !s.closing,
    );
    enable(handle(hwnd, RELOAD), !busy);
    for id in [REFRESH, DETECT, ABOUT_REFRESH] {
        enable(
            handle(hwnd, id),
            !s.closing && !s.busy.contains("environment"),
        );
    }
    enable(
        handle(hwnd, CHECK_UPDATE),
        !s.closing && !s.busy.contains("update"),
    );
    enable(
        handle(hwnd, OPEN_UPDATE),
        !s.closing && !s.update.download_url.is_empty(),
    );
    render_mounts(hwnd);
    if !child.is_null() {
        refresh(child, snapshot);
    }
}
fn size_text(bytes: u64) -> String {
    if bytes >= 1024 * 1024 {
        format!("{:.1} MB", bytes as f64 / (1024.0 * 1024.0))
    } else if bytes >= 1024 {
        format!("{:.0} KB", bytes as f64 / 1024.0)
    } else {
        format!("{bytes} B")
    }
}
fn update_text(s: &Snapshot) -> String {
    let u = &s.update;
    let checking = u.state == UpdateState::Checking || s.busy.contains("update");
    let status = if checking {
        "检查中…".to_string()
    } else {
        match u.state {
            UpdateState::Idle => "尚未检查。".into(),
            UpdateState::UpToDate => {
                if u.message.is_empty() {
                    format!("已是最新版本（{}）。", u.latest)
                } else {
                    u.message.clone()
                }
            }
            UpdateState::Available => format!("发现新版本 {}。", u.latest),
            UpdateState::Failed => u.message.clone(),
            UpdateState::Checking => "检查中…".into(),
        }
    };
    let mut text = format!("当前：{}\r\n状态：{}", crate::version::summary(), status);
    if !checking && !u.checked_at.is_empty() {
        text.push_str(&format!(" · 检查于 {}", u.checked_at));
    }
    if !checking && u.state == UpdateState::Available {
        if !u.published_at.is_empty() {
            text.push_str(&format!("\r\n发布：{}", u.published_at));
        }
        if !u.asset_name.is_empty() {
            text.push_str(&format!(
                " · 资产 {}（{}）",
                u.asset_name,
                size_text(u.asset_bytes)
            ));
        }
        if !u.notes.is_empty() {
            let notes = u.notes.replace(['\r', '\n'], " ");
            let notes: String = notes.chars().take(240).collect();
            text.push_str(&format!("\r\n更新说明：{notes}"));
        }
    }
    text
}
fn environment_text(s: &Snapshot) -> String {
    format!(
        "rclone：{}\r\n程序：{}\r\nWinFsp：{} · Remote：{} 个{}",
        if s.environment.version.is_empty() {
            "未检测到有效版本"
        } else {
            &s.environment.version
        },
        s.environment
            .exe
            .as_ref()
            .map(|p| p.display().to_string())
            .unwrap_or_else(|| "未发现".into()),
        if s.environment.winfsp {
            "已安装"
        } else {
            "未检测到（挂载依赖）"
        },
        s.environment.remotes.len(),
        if s.environment.error.is_empty() {
            String::new()
        } else {
            format!("\r\n{}", s.environment.error)
        }
    )
}
fn diagnostics(s: &Snapshot) -> String {
    let data = crate::config::app_data_dir();
    // Deliberately do not format AppConfig/credentials or raw backend errors.
    // Diagnostics are an allowlist of facts, not a dump of process arguments.
    let mut text=format!("rclone_tray {} · 原生 Rust / Win32\r\n\r\nrclone 版本：{}\r\nrclone 程序：{}\r\nWinFsp：{}\r\nRemote：{} 个\r\n\r\n应用配置：{}\r\nrclone 配置：{}\r\n日志目录：{}\r\n配置版本：{}\r\n挂载配置：{} 个\r\nWindows 登录自启：{}\r\n登录自动挂载：{} · 延迟 {} 秒\r\nGUI：{}（{}）\r\n配置读取：{}\r\n\r\n诊断不显示或复制用户名、密码、进程参数或原始错误；日志由运行层脱敏。\r\n配置损坏不会被覆盖：修复文件后重新载入。\r\n关闭窗口销毁前端，托盘与本应用任务继续运行；退出会停止本应用任务。",
        crate::version::summary(),s.environment.version,s.environment.exe.as_ref().map(|p|p.display().to_string()).unwrap_or_else(||"自动发现 / 未发现".into()),
        if s.environment.winfsp{"已安装"}else{"未检测到"},s.environment.remotes.len(),data.join("config.json").display(),
        s.environment.conf.as_ref().map(|p|p.display().to_string()).unwrap_or_else(||"未发现".into()),data.join("logs").display(),s.revision,s.config.mounts.len(),
        if s.config.startup_enabled{"启用"}else{"关闭"},if s.config.auto_mount_on_boot{"启用"}else{"关闭"},s.config.mount_delay_seconds,s.gui.state.label(),
        if s.config.gui_no_auth{"免认证"}else{"认证，凭据不显示"},if s.config_error.is_empty(){"正常"}else{"失败（详细原因见状态栏）"});
    for credential in [&s.config.gui_pass, &s.config.gui_user] {
        if !credential.is_empty() {
            text = text.replace(credential, "[已隐藏]");
        }
    }
    text
}
fn render_mounts(hwnd: HWND) {
    let query = text(handle(hwnd, SEARCH)).to_lowercase();
    let filter = unsafe { SendMessageW(handle(hwnd, FILTER), CB_GETCURSEL, 0, 0) };
    let s = with_view(hwnd, |v| v.model.borrow().snapshot.clone()).unwrap();
    let mut ids = Vec::new();
    let mut rows = Vec::new();
    for mount in &s.config.mounts {
        let state = s
            .mounts
            .get(&mount.id)
            .map_or(RunState::Stopped, |x| x.state);
        let index = match state {
            RunState::Stopped => 1,
            RunState::Starting => 2,
            RunState::Ready => 3,
            RunState::Stopping => 4,
            RunState::Failed => 5,
        };
        if (filter <= 0 || filter == index)
            && (query.is_empty()
                || format!("{} {} {}", mount.name, mount.remote, mount.mount_point)
                    .to_lowercase()
                    .contains(&query))
        {
            ids.push(mount.id.clone());
            rows.push(format!(
                "{}  |  {}  |  {}  |  {}",
                mount.name,
                state.label(),
                mount.remote,
                mount.mount_point
            ));
        }
    }
    let (changed, selected) = with_view(hwnd, |v| {
        let mut m = v.model.borrow_mut();
        if !s.config.mounts.iter().any(|x| x.id == m.selected) {
            m.selected.clear();
        }
        if m.selected.is_empty() {
            if let Some(id) = ids.first() {
                m.selected = id.clone();
            }
        }
        let changed = m.list_rows != rows || m.visible_ids != ids;
        if changed {
            m.list_rows.clone_from(&rows);
            m.visible_ids.clone_from(&ids);
        }
        (changed, m.selected.clone())
    })
    .unwrap();
    let list = handle(hwnd, MOUNT_LIST);
    if changed {
        let top = unsafe { SendMessageW(list, LB_GETTOPINDEX, 0, 0) };
        unsafe {
            SendMessageW(list, WM_SETREDRAW, 0, 0);
            SendMessageW(list, LB_RESETCONTENT, 0, 0);
        }
        // Owner-drawn rows elide long paths; no horizontal text extent is needed.
        for row in &rows {
            unsafe {
                SendMessageW(list, LB_ADDSTRING, 0, wide(row).as_ptr() as isize);
            }
        }
        unsafe {
            SendMessageW(list, LB_SETTOPINDEX, top.max(0) as usize, 0);
            SendMessageW(list, WM_SETREDRAW, 1, 0);
            InvalidateRect(list, null(), 1);
        }
    }
    let selection = ids
        .iter()
        .position(|id| id == &selected)
        .map(|i| i as isize)
        .unwrap_or(-1);
    unsafe {
        SendMessageW(list, LB_SETCURSEL, selection as usize, 0);
    }
    let ready = s
        .mounts
        .values()
        .filter(|x| x.state == RunState::Ready)
        .count();
    set_text(
        handle(hwnd, SUMMARY),
        &format!(
            "{} 个挂载 · {} 个就绪 · {} 个 remote · 显示 {} 个{}",
            s.config.mounts.len(),
            ready,
            s.environment.remotes.len(),
            ids.len(),
            if s.config.mounts.is_empty() {
                "\r\n还没有挂载；先配置真实 remote 和 WinFsp。"
            } else if ids.is_empty() {
                "\r\n没有匹配项；清除筛选即可显示，原配置保留。"
            } else {
                ""
            }
        ),
    );
    let mount = s.config.mounts.iter().find(|x| x.id == selected);
    let status = s.mounts.get(&selected);
    let state = status.map_or(RunState::Stopped, |x| x.state);
    let detail = if let Some(m) = mount {
        format!("{}{}\r\n\r\nRemote\r\n{}\r\n\r\n挂载位置\r\n{}\r\n\r\n{}{}\r\nVFS {} · {} · {}\r\n自动挂载：{}\r\n卷标：{}\r\n\r\n{}",m.name,if !ids.contains(&selected){"（被筛选隐藏，仍保留选择）"}else{""},m.remote,m.mount_point,state.label(),status.and_then(|x|x.pid).map(|p|format!(" · PID {p}")).unwrap_or_default(),m.vfs_cache_mode,if m.network_mode{"网络模式"}else{"本地模式"},if m.read_only{"只读"}else{"可读写"},if m.auto_mount{"允许"}else{"关闭"},m.volname,status.map(|x|x.message.as_str()).unwrap_or(""))
    } else {
        "选择挂载查看详情。动作只作用于所选稳定挂载 ID。".into()
    };
    set_text(handle(hwnd, MOUNT_DETAIL), &detail);
    let busy = s.closing || s.busy.contains("mounts");
    let locked = busy || s.busy.contains(&selected);
    let live = status.is_some_and(|x| {
        x.pid.is_some()
            || matches!(
                x.state,
                RunState::Starting | RunState::Ready | RunState::Stopping
            )
    });
    enable(
        handle(hwnd, ADD),
        !s.closing && !s.busy.contains("settings") && s.config_error.is_empty(),
    );
    for id in [START_ALL, STOP_ALL] {
        enable(handle(hwnd, id), !busy && !s.config.mounts.is_empty());
    }
    for (id, enabled) in [
        (
            START,
            mount.is_some() && !live && matches!(state, RunState::Stopped | RunState::Failed),
        ),
        (
            STOP,
            mount.is_some() && (live || state != RunState::Stopped),
        ),
        (OPEN, mount.is_some() && state == RunState::Ready),
        (LOG, status.is_some_and(|x| x.log_path.is_some())),
        (
            EDIT,
            mount.is_some() && s.config_error.is_empty() && !s.busy.contains("settings"),
        ),
        (
            DELETE,
            mount.is_some() && s.config_error.is_empty() && !s.busy.contains("settings"),
        ),
    ] {
        enable(handle(hwnd, id), enabled && !locked);
    }
}
fn render_editor(hwnd: HWND) {
    let (s, id, d) = with_view(hwnd, |v| {
        let m = v.model.borrow();
        let (id, d) = m.editor.as_ref().unwrap();
        (m.snapshot.clone(), id.clone(), d.clone())
    })
    .unwrap();
    let busy = d.pending
        || s.closing
        || s.busy.contains(&id)
        || s.busy.contains("mounts")
        || s.busy.contains("settings")
        || with_view(hwnd, |v| v.model.borrow().reload_request.is_some()).unwrap_or(false);
    for id in d.values.keys() {
        enable(handle(hwnd, *id), !d.pending && !s.closing);
    }
    enable(handle(hwnd, BROWSE_POINT), !d.pending && !s.closing);
    enable(
        handle(hwnd, EDITOR_SAVE),
        !busy && s.config_error.is_empty(),
    );
    enable(handle(hwnd, EDITOR_CANCEL), !d.pending);
    enable(
        handle(hwnd, EDITOR_REBASE),
        !busy && s.config_error.is_empty() && d.revision != s.revision,
    );
    enable(
        handle(hwnd, EDITOR_RELOAD),
        !d.pending
            && !s.closing
            && !s.busy.contains("settings")
            && !with_view(hwnd, |v| v.model.borrow().reload_request.is_some()).unwrap_or(false),
    );
    set_text(
        handle(hwnd, EDITOR_NOTE),
        &format!(
            "{}\r\n检测到 {} 个 remote，可手动输入子路径；此处不创建远端。",
            render_note(&d, &s),
            s.environment.remotes.len()
        ),
    );
    // Editable remote suggestions never replace the user's text or selection.
    let remote = handle(hwnd, REMOTE);
    let count = unsafe { SendMessageW(remote, CB_GETCOUNT, 0, 0) };
    let known = with_view(hwnd, |v| v.model.borrow().list_rows.clone()).unwrap_or_default();
    if count != s.environment.remotes.len() as isize || known != s.environment.remotes {
        let typed = text(remote);
        with_view(hwnd, |v| v.loading.set(true));
        unsafe {
            SendMessageW(remote, CB_RESETCONTENT, 0, 0);
        }
        for name in &s.environment.remotes {
            unsafe {
                SendMessageW(remote, CB_ADDSTRING, 0, wide(name).as_ptr() as isize);
            }
        }
        set_text(remote, &typed);
        with_view(hwnd, |v| {
            v.loading.set(false);
            v.model.borrow_mut().list_rows = s.environment.remotes.clone();
        });
    }
}

fn feedback(hwnd: HWND, message: impl Into<String>) {
    let message = message.into();
    let editor = with_view(hwnd, |v| {
        let mut m = v.model.borrow_mut();
        if let Some((_, d)) = &mut m.editor {
            d.error = message.clone();
            true
        } else {
            m.message = message.clone();
            false
        }
    })
    .unwrap_or(false);
    if editor {
        render_editor(hwnd);
    } else {
        set_text(handle(hwnd, MESSAGE), &message);
    }
}
fn submit(hwnd: HWND, command: Command) -> Option<u64> {
    let backend = with_view(hwnd, |v| v.backend.clone())?;
    match backend.send(command) {
        Ok(id) => Some(id),
        Err(e) => {
            feedback(hwnd, e);
            None
        }
    }
}
fn queue(hwnd: HWND, command: Command) -> bool {
    submit(hwnd, command).is_some()
}
fn current_snapshot(hwnd: HWND) -> Option<Arc<Snapshot>> {
    with_view(hwnd, |v| v.backend.snapshot())
}
fn confirm(hwnd: HWND, title: &str, message: &str) -> bool {
    if with_view(hwnd, |v| v.modal.replace(true)).unwrap_or(true) {
        return false;
    }
    let answer = unsafe {
        MessageBoxW(
            hwnd,
            wide(message).as_ptr(),
            wide(title).as_ptr(),
            MB_YESNO | MB_ICONWARNING | MB_DEFBUTTON2,
        )
    } == IDYES;
    with_view(hwnd, |v| v.modal.set(false));
    answer && with_view(hwnd, |_| ()).is_some()
}
fn guard(hwnd: HWND, all: bool) -> bool {
    let (pending, dirty, editor) = match with_view(hwnd, |v| {
        let m = v.model.borrow();
        if let Some((_, d)) = &m.editor {
            (d.pending, d.dirty(), true)
        } else {
            (
                m.settings.pending || m.service.pending,
                (all || m.page == Page::Settings) && m.settings.dirty()
                    || (all || m.page == Page::Service) && m.service.dirty(),
                false,
            )
        }
    }) {
        Some(x) => x,
        None => return true,
    };
    if pending {
        feedback(hwnd, "正在保存，请等待结果后离开；输入保留。");
        return false;
    }
    if !dirty {
        return true;
    }
    if !confirm(
        hwnd,
        "放弃未保存输入？",
        "当前有未保存输入。确认放弃并继续？\n已经执行的启停操作不会被撤销。",
    ) {
        return false;
    }
    let s = match current_snapshot(hwnd) {
        Some(s) => s,
        None => return false,
    };
    with_view(hwnd, |v| {
        let mut m = v.model.borrow_mut();
        if !editor {
            if all || m.page == Page::Settings {
                m.settings = Draft::new(settings_values(&s.config), s.revision);
            }
            if all || m.page == Page::Service {
                m.service = Draft::new(service_values(&s.config), s.revision);
            }
        }
    });
    if !editor {
        sync_inputs(hwnd);
        apply_theme(hwnd);
    }
    true
}
fn close_view(hwnd: HWND) -> bool {
    if with_view(hwnd, |v| v.modal.get()).unwrap_or(false) {
        return false;
    }
    let child = with_view(hwnd, |v| v.child.get()).unwrap_or(null_mut());
    if !child.is_null() && !close_view(child) {
        unsafe {
            SetForegroundWindow(child);
        }
        return false;
    }
    if !guard(hwnd, true) {
        return false;
    }
    unsafe {
        if !view_ptr(hwnd).is_null() {
            DestroyWindow(hwnd);
        }
    }
    true
}
fn navigate(hwnd: HWND, page: Page, mount: Option<&str>) -> bool {
    if with_view(hwnd, |v| v.modal.get()).unwrap_or(true) {
        return false;
    }
    let (old, child) = with_view(hwnd, |v| (v.model.borrow().page, v.child.get())).unwrap();
    if !child.is_null() && !close_view(child) {
        unsafe {
            SetForegroundWindow(child);
        }
        return false;
    }
    if old != page && !guard(hwnd, false) {
        return false;
    }
    let s = match current_snapshot(hwnd) {
        Some(s) => s,
        None => return false,
    };
    with_view(hwnd, |v| {
        let mut m = v.model.borrow_mut();
        m.page = page;
        if let Some(id) = mount {
            m.selected = id.into();
        }
        v.scroll_x.set(0);
        v.scroll_y.set(0);
    });
    if mount.is_some() {
        clear_filter(hwnd);
    }
    refresh(hwnd, s);
    layout(hwnd);
    set_text(hwnd, &format!("rclone_tray · {}", page.label()));
    unsafe {
        ShowWindow(hwnd, SW_RESTORE);
        SetForegroundWindow(hwnd);
    }
    true
}
fn exit_app(hwnd: HWND) -> bool {
    if with_view(hwnd, |v| v.modal.get()).unwrap_or(true) {
        return false;
    }
    let child = with_view(hwnd, |v| v.child.get()).unwrap_or(null_mut());
    if !child.is_null() && !close_view(child) {
        return false;
    }
    if !guard(hwnd, true) {
        return false;
    }
    let s = match current_snapshot(hwnd) {
        Some(s) => s,
        None => return false,
    };
    if s.closing {
        feedback(hwnd, "正在停止本应用任务，请等待退出结果。");
        return false;
    }
    let active = std::iter::once(&s.gui)
        .chain(s.mounts.values())
        .filter(|x| {
            x.pid.is_some()
                || matches!(
                    x.state,
                    RunState::Starting | RunState::Ready | RunState::Stopping
                )
        })
        .count();
    if !confirm(hwnd,"退出 rclone_tray？",&format!("将停止本应用持有的 {active} 个运行任务并取消延迟启动，然后退出。\n停止失败时应用保留以便重试；不接管其他应用的进程。")){return false;}
    if queue(hwnd, Command::Shutdown) {
        feedback(hwnd, "正在停止本应用持有的任务…");
        true
    } else {
        false
    }
}
fn clear_filter(hwnd: HWND) {
    set_text(handle(hwnd, SEARCH), "");
    unsafe {
        SendMessageW(handle(hwnd, FILTER), CB_SETCURSEL, 0, 0);
    }
    render_mounts(hwnd);
}
fn open_editor(hwnd: HWND, existing: bool) {
    let (child, backend, selected) = match with_view(hwnd, |v| {
        (
            v.child.get(),
            v.backend.clone(),
            v.model.borrow().selected.clone(),
        )
    }) {
        Some(x) => x,
        None => return,
    };
    if !child.is_null() && unsafe { IsWindow(child) } != 0 {
        unsafe {
            SetForegroundWindow(child);
        }
        return;
    }
    let s = backend.snapshot();
    if s.closing || !s.config_error.is_empty() || s.busy.contains("settings") {
        feedback(hwnd, "请先处理配置错误或等待设置保存完成。");
        return;
    }
    let mount = if existing {
        match s.config.mounts.iter().find(|x| x.id == selected) {
            Some(m) => Some(m.clone()),
            None => {
                feedback(hwnd, "请选择一个挂载。");
                return;
            }
        }
    } else {
        None
    };
    match create_view(
        null_mut(),
        hwnd,
        backend,
        Page::Mounts,
        String::new(),
        Some(mount),
    ) {
        Ok(child) => {
            with_view(hwnd, |v| v.child.set(child));
            unsafe {
                ShowWindow(child, SW_SHOWNORMAL);
                SetForegroundWindow(child);
                SetFocus(handle(child, NAME));
            }
        }
        Err(error) => feedback(hwnd, error),
    }
}
fn delete_mount(hwnd: HWND) {
    let selected = with_view(hwnd, |v| v.model.borrow().selected.clone()).unwrap_or_default();
    let s = match current_snapshot(hwnd) {
        Some(s) => s,
        None => return,
    };
    let mount = match s.config.mounts.iter().find(|x| x.id == selected) {
        Some(m) => m,
        None => return,
    };
    if s.mounts.get(&selected).is_some_and(|x| {
        x.pid.is_some()
            || matches!(
                x.state,
                RunState::Starting | RunState::Ready | RunState::Stopping
            )
    }) {
        feedback(hwnd, "请先停止所选挂载，确认停止后再删除配置。");
        return;
    }
    if confirm(
        hwnd,
        "删除挂载配置？",
        &format!(
            "删除“{}”的挂载配置？\n不会删除 remote 或任何远端文件。",
            mount.name
        ),
    ) {
        queue(
            hwnd,
            Command::DeleteMount {
                id: selected,
                revision: s.revision,
            },
        );
    }
}
fn reload(hwnd: HWND) {
    let (pending, dirty, child) = with_view(hwnd, |v| {
        let m = v.model.borrow();
        (
            m.reload_request.is_some()
                || m.service.pending
                || m.settings.pending
                || m.editor.as_ref().is_some_and(|(_, d)| d.pending),
            m.service.dirty() || m.settings.dirty() || m.editor.is_some(),
            v.child.get(),
        )
    })
    .unwrap();
    let child_pending = with_view(child, |v| {
        let m = v.model.borrow();
        m.reload_request.is_some() || m.editor.as_ref().is_some_and(|(_, d)| d.pending)
    })
    .unwrap_or(false);
    if pending || child_pending {
        feedback(hwnd, "保存或重新载入正在执行，请等待结果。");
        return;
    }
    if (dirty || !child.is_null())
        && !confirm(
            hwnd,
            "重新载入配置",
            "重新读取配置并保留所有输入？\n草稿保留原版本；成功后可显式更新保存版本。",
        )
    {
        return;
    }
    if let Some(id) = submit(hwnd, Command::ReloadConfig) {
        with_view(hwnd, |v| v.model.borrow_mut().reload_request = Some(id));
        with_view(child, |v| v.model.borrow_mut().reload_request = Some(id));
    }
}
fn save_form(hwnd: HWND, page: Page) {
    let s = match current_snapshot(hwnd) {
        Some(s) => s,
        None => return,
    };
    let d = with_view(hwnd, |v| {
        let m = v.model.borrow();
        if page == Page::Service {
            m.service.clone()
        } else {
            m.settings.clone()
        }
    })
    .unwrap();
    let pending = with_view(hwnd, |v| {
        let m = v.model.borrow();
        m.service.pending || m.settings.pending
    })
    .unwrap_or(true);
    if pending || s.closing || s.busy.contains("settings") || !s.config_error.is_empty() {
        feedback(hwnd, "当前不能保存；输入保留，请等待或修复配置后重新载入。");
        return;
    }
    let mut cfg = s.config.clone();
    let values = &d.values;
    let error = if page == Page::Service {
        cfg.gui_user = values[&GUI_USER].text().into();
        cfg.gui_pass = values[&GUI_PASS].text().into();
        cfg.gui_no_auth = values[&GUI_NO_AUTH].checked();
        cfg.gui_auto_start = values[&GUI_AUTO].checked();
        if !cfg.gui_no_auth && (cfg.gui_user.is_empty() || cfg.gui_pass.is_empty()) {
            Some("启用认证时用户名与密码都不能为空；输入保留。".into())
        } else {
            None
        }
    } else {
        cfg.rclone_path = values[&EXE].text().into();
        cfg.rclone_conf = values[&CONF].text().into();
        cfg.startup_enabled = values[&STARTUP].checked();
        cfg.auto_mount_on_boot = values[&AUTO_MOUNT].checked();
        cfg.theme = values[&THEME].text().into();
        cfg.reduce_motion = values[&REDUCE_MOTION].checked();
        match values[&DELAY].text().trim().parse::<u32>() {
            Ok(delay) if delay <= 86400 => {
                cfg.mount_delay_seconds = delay;
                None
            }
            _ => Some("挂载延迟必须是 0–86400 的整数；输入保留。".into()),
        }
    };
    let error = error.or_else(|| cfg.validate().err());
    if let Some(error) = error {
        with_view(hwnd, |v| {
            let mut m = v.model.borrow_mut();
            let d = if page == Page::Service {
                &mut m.service
            } else {
                &mut m.settings
            };
            d.error = error;
        });
        render_root(hwnd);
        return;
    }
    // No mutable view borrow crosses submission; backend wake only posts to host.
    with_view(hwnd, |v| {
        let mut m = v.model.borrow_mut();
        let d = if page == Page::Service {
            &mut m.service
        } else {
            &mut m.settings
        };
        d.pending = true;
        d.request_id = None;
        d.error.clear();
    });
    let request = submit(
        hwnd,
        Command::SaveSettings {
            draft: cfg,
            revision: d.revision,
        },
    );
    with_view(hwnd, |v| {
        let mut m = v.model.borrow_mut();
        let d = if page == Page::Service {
            &mut m.service
        } else {
            &mut m.settings
        };
        d.request_id = request;
        if request.is_none() {
            d.pending = false;
            d.error = "保存未提交；输入保留。".into();
        }
    });
    render_root(hwnd);
}
fn save_editor(hwnd: HWND) {
    let s = match current_snapshot(hwnd) {
        Some(s) => s,
        None => return,
    };
    let (id, d) = with_view(hwnd, |v| v.model.borrow().editor.as_ref().unwrap().clone()).unwrap();
    if d.pending
        || s.closing
        || !s.config_error.is_empty()
        || s.busy.contains(&id)
        || s.busy.contains("settings")
        || s.busy.contains("mounts")
    {
        feedback(hwnd, "当前不能保存；输入保留，请等待或修复配置。");
        return;
    }
    let v = &d.values;
    let mount = MountConfig {
        id,
        name: v[&NAME].text().trim().into(),
        remote: v[&REMOTE].text().trim().into(),
        mount_point: v[&POINT].text().trim().into(),
        volname: v[&VOLNAME].text().into(),
        vfs_cache_mode: v[&VFS].text().into(),
        network_mode: v[&NETWORK].checked(),
        read_only: v[&READ_ONLY].checked(),
        auto_mount: v[&MOUNT_AUTO].checked(),
    };
    if let Err(error) = mount.validate() {
        feedback(hwnd, error);
        return;
    }
    with_view(hwnd, |view| {
        let mut m = view.model.borrow_mut();
        let d = &mut m.editor.as_mut().unwrap().1;
        d.pending = true;
        d.request_id = None;
        d.error.clear();
    });
    let request = submit(
        hwnd,
        Command::SaveMount {
            draft: mount,
            revision: d.revision,
        },
    );
    with_view(hwnd, |view| {
        let mut m = view.model.borrow_mut();
        let d = &mut m.editor.as_mut().unwrap().1;
        d.request_id = request;
        if request.is_none() {
            d.pending = false;
            if d.error.is_empty() {
                d.error = "保存未提交；输入保留，请重试。".into();
            }
        }
    });
    render_editor(hwnd);
}
fn rebase(hwnd: HWND, page: Option<Page>) {
    let s = match current_snapshot(hwnd) {
        Some(s) => s,
        None => return,
    };
    if !s.config_error.is_empty() || s.closing {
        return;
    }
    let pending = with_view(hwnd, |v| {
        let m = v.model.borrow();
        if let Some((_, d)) = &m.editor {
            d.pending
        } else {
            m.service.pending || m.settings.pending
        }
    })
    .unwrap_or(true);
    if pending {
        return;
    }
    if !confirm(
        hwnd,
        "更新保存版本",
        "保留输入并使用最新配置版本？\n下一次保存仅应用本页 / 此挂载字段，其他设置采用最新值。",
    ) {
        return;
    }
    let s = match current_snapshot(hwnd) {
        Some(s) => s,
        None => return,
    };
    with_view(hwnd, |v| {
        let mut m = v.model.borrow_mut();
        let d = if m.editor.is_some() {
            &mut m.editor.as_mut().unwrap().1
        } else if page == Some(Page::Service) {
            &mut m.service
        } else {
            &mut m.settings
        };
        d.revision = s.revision;
        d.error = "已更新版本；输入保留，请再次保存。".into();
    });
    refresh(hwnd, s);
}
fn discard(hwnd: HWND, page: Page) {
    let pending = with_view(hwnd, |v| {
        let m = v.model.borrow();
        m.service.pending || m.settings.pending
    })
    .unwrap_or(true);
    if pending {
        feedback(hwnd, "正在保存，请等待结果。");
        return;
    }
    let dirty = with_view(hwnd, |v| {
        let m = v.model.borrow();
        if page == Page::Service {
            m.service.dirty()
        } else {
            m.settings.dirty()
        }
    })
    .unwrap_or(false);
    if dirty
        && !confirm(
            hwnd,
            "放弃本页输入？",
            "放弃此页未保存输入？已执行的运行操作不会撤销。",
        )
    {
        return;
    }
    let s = match current_snapshot(hwnd) {
        Some(s) => s,
        None => return,
    };
    with_view(hwnd, |v| {
        let mut m = v.model.borrow_mut();
        if page == Page::Service {
            m.service = Draft::new(service_values(&s.config), s.revision);
        } else {
            m.settings = Draft::new(settings_values(&s.config), s.revision);
        }
    });
    refresh(hwnd, s);
}
fn handle_event(hwnd: HWND, event: &Event, snapshot: Arc<Snapshot>) {
    if with_view(hwnd, |_| ()).is_none() {
        return;
    }
    with_view(hwnd, |v| v.model.borrow_mut().snapshot = snapshot.clone());
    let child = with_view(hwnd, |v| v.child.get()).unwrap_or(null_mut());
    if !child.is_null() {
        let matching = with_view(child, |v| {
            v.model.borrow().editor.as_ref().is_some_and(|(id, d)| {
                d.pending
                    && d.request_id.is_some()
                    && d.request_id == event.request_id
                    && id == &event.key
                    && event.action == "save_mount"
            })
        })
        .unwrap_or(false);
        if matching && event.ok {
            with_view(hwnd, |v| v.model.borrow_mut().selected = event.key.clone());
            clear_filter(hwnd);
        }
        handle_event(child, event, snapshot.clone());
    }
    let close_editor = with_view(hwnd, |v| {
        let mut m = v.model.borrow_mut();
        let reload_matches = event.action == "reload_config"
            && m.reload_request.is_some()
            && m.reload_request == event.request_id;
        if reload_matches {
            m.reload_request = None;
        }
        if let Some((id, d)) = &mut m.editor {
            if d.pending
                && d.request_id.is_some()
                && d.request_id == event.request_id
                && event.action == "save_mount"
                && event.key == *id
            {
                d.pending = false;
                d.request_id = None;
                if event.ok {
                    return true;
                }
                d.error = event.message.clone();
            } else if reload_matches {
                d.error = if event.ok {
                    "配置已重新载入；输入保留，请更新保存版本。".into()
                } else {
                    event.message.clone()
                };
            }
        } else if event.action == "save_settings" && event.key == "settings" {
            for page in [Page::Service, Page::Settings] {
                let d = if page == Page::Service {
                    &mut m.service
                } else {
                    &mut m.settings
                };
                if d.pending && d.request_id.is_some() && d.request_id == event.request_id {
                    d.pending = false;
                    d.request_id = None;
                    if event.ok {
                        *d = Draft::new(
                            if page == Page::Service {
                                service_values(&snapshot.config)
                            } else {
                                settings_values(&snapshot.config)
                            },
                            snapshot.revision,
                        );
                    } else {
                        d.error = event.message.clone();
                    }
                }
            }
        }
        m.message = format!(
            "{}：{}",
            if event.ok { "完成" } else { "失败" },
            event.message
        );
        false
    })
    .unwrap_or(false);
    if close_editor {
        unsafe {
            DestroyWindow(hwnd);
        }
        return;
    }
    refresh(hwnd, snapshot);
}

fn choose_file(hwnd: HWND, id: u16, exe: bool) {
    if with_view(hwnd, |v| v.modal.replace(true)).unwrap_or(true) {
        return;
    }
    let mut buffer = vec![0u16; 32768];
    let existing = text(handle(hwnd, id));
    for (to, from) in buffer.iter_mut().zip(existing.encode_utf16()) {
        *to = from;
    }
    let filter = wide(if exe {
        "可执行文件\0*.exe\0所有文件\0*.*\0"
    } else {
        "rclone 配置\0*.conf\0所有文件\0*.*\0"
    });
    let title = wide(if exe {
        "选择 rclone 可执行文件"
    } else {
        "选择 rclone 配置"
    });
    let mut ofn = unsafe { std::mem::zeroed::<OPENFILENAMEW>() };
    ofn.lStructSize = std::mem::size_of::<OPENFILENAMEW>() as u32;
    ofn.hwndOwner = hwnd;
    ofn.lpstrFile = buffer.as_mut_ptr();
    ofn.nMaxFile = buffer.len() as u32;
    ofn.lpstrFilter = filter.as_ptr();
    ofn.lpstrTitle = title.as_ptr();
    ofn.Flags = OFN_FILEMUSTEXIST | OFN_PATHMUSTEXIST | OFN_NOCHANGEDIR | OFN_EXPLORER;
    let accepted = unsafe { GetOpenFileNameW(&mut ofn) } != 0;
    let error = if accepted {
        0
    } else {
        unsafe { CommDlgExtendedError() }
    };
    with_view(hwnd, |v| v.modal.set(false));
    if with_view(hwnd, |_| ()).is_none() {
        return;
    }
    if accepted {
        let len = buffer.iter().position(|x| *x == 0).unwrap_or(buffer.len());
        set_text(handle(hwnd, id), &String::from_utf16_lossy(&buffer[..len]));
        capture(hwnd, id);
    } else if error != 0 {
        feedback(hwnd, format!("文件选择失败：{error}"));
    }
    if let Some(s) = current_snapshot(hwnd) {
        refresh(hwnd, s);
    }
}
fn choose_directory(hwnd: HWND) {
    if with_view(hwnd, |v| v.modal.replace(true)).unwrap_or(true) {
        return;
    }
    // Shell folder picker returns a filesystem PIDL. CoTaskMemFree is paired
    // with the shell allocation; no persistent COM interface or UI thread.
    let title = wide("选择空的挂载目录（保存 / 启动时校验）");
    let mut info = unsafe { std::mem::zeroed::<BROWSEINFOW>() };
    info.hwndOwner = hwnd;
    info.lpszTitle = title.as_ptr();
    info.ulFlags = BIF_RETURNONLYFSDIRS | BIF_EDITBOX | BIF_NEWDIALOGSTYLE;
    let pidl = unsafe { SHBrowseForFolderW(&info) };
    let mut buffer = vec![0u16; 32768];
    let accepted = !pidl.is_null()
        && unsafe { SHGetPathFromIDListEx(pidl, buffer.as_mut_ptr(), buffer.len() as u32, 0) } != 0;
    if !pidl.is_null() {
        unsafe {
            windows_sys::Win32::System::Com::CoTaskMemFree(pidl.cast());
        }
    }
    with_view(hwnd, |v| v.modal.set(false));
    if accepted && with_view(hwnd, |_| ()).is_some() {
        let len = buffer.iter().position(|x| *x == 0).unwrap_or(buffer.len());
        set_text(
            handle(hwnd, POINT),
            &String::from_utf16_lossy(&buffer[..len]),
        );
        capture(hwnd, POINT);
        render_editor(hwnd);
    }
}
fn copy_diagnostics(hwnd: HWND) {
    let s = match current_snapshot(hwnd) {
        Some(s) => s,
        None => return,
    };
    let utf16 = wide(&diagnostics(&s));
    unsafe {
        let memory = GlobalAlloc(GMEM_MOVEABLE, utf16.len() * 2);
        if memory.is_null() {
            feedback(hwnd, "复制失败：无法分配剪贴板内存。");
            return;
        }
        let data = GlobalLock(memory) as *mut u16;
        if data.is_null() {
            GlobalFree(memory);
            feedback(hwnd, "复制失败：无法锁定剪贴板内存。");
            return;
        }
        std::ptr::copy_nonoverlapping(utf16.as_ptr(), data, utf16.len());
        GlobalUnlock(memory);
        if OpenClipboard(hwnd) == 0 {
            GlobalFree(memory);
            feedback(hwnd, "剪贴板正被其他应用占用，请重试。");
            return;
        }
        let ok = EmptyClipboard() != 0 && !SetClipboardData(13, memory).is_null();
        CloseClipboard();
        if !ok {
            GlobalFree(memory);
            feedback(hwnd, "复制诊断失败。");
        } else {
            feedback(hwnd, "诊断已复制（不含认证凭据）。");
        }
    }
}
fn command(hwnd: HWND, id: u16, notification: u16, source: HWND) {
    if with_view(hwnd, |v| v.loading.get() || v.modal.get()).unwrap_or(true) {
        return;
    }
    if notification == EN_SETFOCUS as u16 || notification == CBN_SETFOCUS as u16 {
        ensure_visible(hwnd, source);
        return;
    }
    if notification == CBN_SELCHANGE as u16 {
        capture_combo_selection(hwnd, id);
    }
    if notification == EN_CHANGE as u16
        || notification == CBN_EDITCHANGE as u16
        || notification == CBN_SELCHANGE as u16
        || notification == BN_CLICKED as u16
    {
        if capture(hwnd, id) {
            if let Some(s) = current_snapshot(hwnd) {
                refresh(hwnd, s);
            }
            return;
        }
    }
    if id == SEARCH && notification == EN_CHANGE as u16
        || id == FILTER && notification == CBN_SELCHANGE as u16
    {
        render_mounts(hwnd);
        return;
    }
    if id == MOUNT_LIST {
        if notification == LBN_SELCHANGE as u16 {
            let index = unsafe { SendMessageW(source, LB_GETCURSEL, 0, 0) };
            with_view(hwnd, |v| {
                let mut m = v.model.borrow_mut();
                if index >= 0 {
                    if let Some(id) = m.visible_ids.get(index as usize).cloned() {
                        m.selected = id;
                    }
                }
            });
            render_mounts(hwnd);
        } else if notification == LBN_DBLCLK as u16 {
            open_editor(hwnd, true);
        }
        return;
    }
    if notification != BN_CLICKED as u16 {
        return;
    }
    if !source.is_null() && unsafe { IsWindowEnabled(source) } == 0 {
        return;
    }
    let selected = with_view(hwnd, |v| v.model.borrow().selected.clone()).unwrap_or_default();
    match id {
        TOGGLE_NAV => {
            with_view(hwnd, |v| v.collapsed.set(!v.collapsed.get()));
            layout(hwnd);
        }
        NAV_MOUNTS => {
            navigate(hwnd, Page::Mounts, None);
        }
        NAV_SERVICE => {
            navigate(hwnd, Page::Service, None);
        }
        NAV_SETTINGS => {
            navigate(hwnd, Page::Settings, None);
        }
        NAV_ABOUT => {
            navigate(hwnd, Page::About, None);
        }
        CLOSE | EDITOR_CANCEL => {
            close_view(hwnd);
        }
        EXIT | ABOUT_EXIT => {
            exit_app(hwnd);
        }
        RELOAD | EDITOR_RELOAD => reload(hwnd),
        CLEAR_FILTER => clear_filter(hwnd),
        ADD => open_editor(hwnd, false),
        EDIT => open_editor(hwnd, true),
        DELETE => delete_mount(hwnd),
        START => {
            queue(hwnd, Command::StartMount(selected));
        }
        STOP => {
            queue(hwnd, Command::StopMount(selected));
        }
        OPEN => {
            queue(hwnd, Command::OpenMount(selected));
        }
        LOG => {
            if let Some(s) = current_snapshot(hwnd) {
                if let Some(path) = s.mounts.get(&selected).and_then(|x| x.log_path.clone()) {
                    queue(hwnd, Command::OpenPath(path));
                }
            }
        }
        START_ALL => {
            queue(hwnd, Command::StartAllMounts);
        }
        STOP_ALL => {
            queue(hwnd, Command::StopAllMounts);
        }
        REFRESH | DETECT | ABOUT_REFRESH => {
            queue(hwnd, Command::RefreshEnvironment);
        }
        CHECK_UPDATE => {
            if queue(hwnd, Command::CheckUpdate { quiet: false }) {
                feedback(hwnd, "正在检查 GitHub Releases…");
            }
        }
        OPEN_UPDATE => {
            if let Some(url) = current_snapshot(hwnd)
                .map(|s| s.update.download_url.clone())
                .filter(|url| !url.is_empty())
            {
                queue(hwnd, Command::OpenUrl(url));
            }
        }
        GUI_START => {
            queue(hwnd, Command::StartGui);
        }
        GUI_STOP => {
            queue(hwnd, Command::StopGui);
        }
        GUI_OPEN => {
            queue(hwnd, Command::OpenGui);
        }
        GUI_LOG => {
            if let Some(path) = current_snapshot(hwnd).and_then(|s| s.gui.log_path.clone()) {
                queue(hwnd, Command::OpenPath(path));
            }
        }
        SERVICE_SAVE => save_form(hwnd, Page::Service),
        SETTINGS_SAVE => save_form(hwnd, Page::Settings),
        EDITOR_SAVE => save_editor(hwnd),
        SERVICE_CANCEL => discard(hwnd, Page::Service),
        SETTINGS_CANCEL => discard(hwnd, Page::Settings),
        SERVICE_REBASE => rebase(hwnd, Some(Page::Service)),
        SETTINGS_REBASE => rebase(hwnd, Some(Page::Settings)),
        EDITOR_REBASE => rebase(hwnd, None),
        BROWSE_EXE => choose_file(hwnd, EXE, true),
        BROWSE_CONF => choose_file(hwnd, CONF, false),
        BROWSE_POINT => choose_directory(hwnd),
        AUTO_DISCOVER => {
            set_text(handle(hwnd, EXE), "");
            capture(hwnd, EXE);
            if let Some(s) = current_snapshot(hwnd) {
                refresh(hwnd, s);
            }
        }
        OPEN_CONF | ABOUT_CONF | EDITOR_CONF => {
            queue(hwnd, Command::OpenRcloneConfig);
        }
        APP_CONFIG => {
            queue(
                hwnd,
                Command::OpenPath(crate::config::app_data_dir().join("config.json")),
            );
        }
        LOG_DIR => {
            queue(
                hwnd,
                Command::OpenPath(crate::config::app_data_dir().join("logs")),
            );
        }
        CONF_DIR => {
            if let Some(s) = current_snapshot(hwnd) {
                let dir = s
                    .environment
                    .conf
                    .as_ref()
                    .and_then(|p| p.parent())
                    .map(PathBuf::from)
                    .unwrap_or_else(crate::config::app_data_dir);
                queue(hwnd, Command::OpenPath(dir));
            }
        }
        HELP => {
            queue(hwnd, Command::OpenUrl("https://rclone.org/docs/".into()));
        }
        WINFSP_HELP => {
            queue(hwnd, Command::OpenUrl("https://winfsp.dev/".into()));
        }
        COPY => copy_diagnostics(hwnd),
        _ => {}
    }
}
fn shortcut(hwnd: HWND, key: u32) {
    match key {
        27 => {
            close_view(hwnd);
        }
        0x53 => {
            let page = with_view(hwnd, |v| v.model.borrow().page).unwrap_or(Page::Mounts);
            if with_view(hwnd, |v| !v.owner.is_null()).unwrap_or(false) {
                save_editor(hwnd);
            } else if matches!(page, Page::Settings | Page::Service) {
                save_form(hwnd, page);
            }
        }
        0x46 => {
            if navigate(hwnd, Page::Mounts, None) {
                unsafe {
                    SetFocus(handle(hwnd, SEARCH));
                }
            }
        }
        0x4e => {
            if navigate(hwnd, Page::Mounts, None) {
                open_editor(hwnd, false);
            }
        }
        46 => delete_mount(hwnd),
        _ => {}
    }
}
/// Host must call this before TranslateMessage / DispatchMessage. Native
/// IsDialogMessage provides Tab, Shift+Tab and default-button keyboard access.
pub fn translate_message(msg: &MSG) -> bool {
    unsafe {
        let root = GetAncestor(msg.hwnd, GA_ROOT);
        if view_ptr(root).is_null() {
            return false;
        }
        if with_view(root, |v| v.modal.get()).unwrap_or(true) {
            return false;
        }
        if msg.message == WM_KEYDOWN {
            let ctrl = GetKeyState(0x11) < 0;
            let editor = with_view(root, |v| !v.owner.is_null()).unwrap_or(false);
            let key = msg.wParam as u32;
            let handled = key == 27
                || ctrl && (key == 0x53 || !editor && (key == 0x46 || key == 0x4e))
                || key == 46 && msg.hwnd == handle(root, MOUNT_LIST);
            if handled {
                PostMessageW(root, WM_SHORTCUT, key as usize, 0);
                return true;
            }
        }
        let handled = IsDialogMessageW(root, msg as *const MSG) != 0;
        if !view_ptr(root).is_null() {
            ensure_visible(root, GetFocus());
        }
        handled
    }
}
