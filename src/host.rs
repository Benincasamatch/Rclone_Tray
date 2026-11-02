//! A small resident message window owns the controller; foreground objects are disposable.
use std::ffi::c_void;
use std::mem::{size_of, zeroed};
use std::ptr;
use std::sync::Arc;
use std::thread::JoinHandle;

use sha2::{Digest, Sha256};
use windows_sys::w;
use windows_sys::Win32::Foundation::{
    CloseHandle, GetLastError, ERROR_ALREADY_EXISTS, HANDLE, HWND, LPARAM, LRESULT, WPARAM,
};
use windows_sys::Win32::System::Com::{CoInitializeEx, CoUninitialize, COINIT_APARTMENTTHREADED};
use windows_sys::Win32::System::LibraryLoader::GetModuleHandleW;
use windows_sys::Win32::System::Threading::CreateMutexW;
use windows_sys::Win32::UI::HiDpi::{
    SetProcessDpiAwarenessContext, DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2,
};
use windows_sys::Win32::UI::Shell::{
    Shell_NotifyIconW, NIF_ICON, NIF_INFO, NIF_MESSAGE, NIF_SHOWTIP, NIF_TIP, NIIF_ERROR,
    NIIF_INFO, NIM_ADD,
    NIM_DELETE, NIM_MODIFY, NIM_SETVERSION, NINF_KEY, NIN_SELECT, NOTIFYICONDATAW,
    NOTIFYICON_VERSION_4,
};
use windows_sys::Win32::UI::WindowsAndMessaging::*;

use crate::backend::Backend;
use crate::contracts::{Command, Page, RunState, Snapshot};
use crate::update::UpdateState;
use crate::ui::{self, Frontend};

pub const WM_BACKEND_EVENT: u32 = WM_APP + 1;
const WM_TRAY_EVENT: u32 = WM_APP + 2;
pub const WM_FRONTEND_CLOSED: u32 = WM_APP + 3;
const WM_OPEN_FRONTEND: u32 = WM_APP + 4;
const HOST_CLASS: *const u16 = w!("rclone_tray.NativeTrayHost");
const TRAY_ID: u32 = 1;
// shellapi.h defines this as a macro; windows metadata exposes its operands.
const NIN_KEYSELECT: u32 = NIN_SELECT | NINF_KEY;

#[derive(Default)]
pub struct Launch {
    pub autostart: bool,
    pub show: bool,
}

struct OwnedHandle(HANDLE);
impl Drop for OwnedHandle {
    fn drop(&mut self) {
        if !self.0.is_null() {
            unsafe {
                CloseHandle(self.0);
            }
        }
    }
}
struct OwnedIcon(HICON);
impl Drop for OwnedIcon {
    fn drop(&mut self) {
        if !self.0.is_null() {
            unsafe {
                DestroyIcon(self.0);
            }
        }
    }
}

#[derive(PartialEq, Eq)]
struct TraySignature {
    gui: RunState,
    ready: usize,
    total: usize,
    failed: bool,
    closing: bool,
}

struct Host {
    hwnd: HWND,
    icon: OwnedIcon,
    small_icon: OwnedIcon,
    backend: Option<Backend>,
    worker: Option<JoinHandle<()>>,
    frontend: Option<Arc<Frontend>>,
    taskbar_created: u32,
    tray_present: bool,
    tray_signature: Option<TraySignature>,
    notified_update: Option<String>,
}

/// No window callback holds a mutable Host reference across a nested Win32 message loop.
pub fn run(launch: Launch) -> Result<(), String> {
    unsafe {
        SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
    }
    let namespace = instance_namespace()?;
    let title = utf16(&format!("rclone_tray.tray.{namespace}"));
    let mutex_name = utf16(&if namespace.is_empty() {
        "Local\\rclone_tray_single_instance".into()
    } else {
        format!("Local\\rclone_tray_single_instance_{namespace}")
    });
    let mutex = unsafe { CreateMutexW(ptr::null(), 0, mutex_name.as_ptr()) };
    if mutex.is_null() {
        return Err(system_error("创建单实例锁"));
    }
    let existing = unsafe { GetLastError() } == ERROR_ALREADY_EXISTS;
    let _mutex = OwnedHandle(mutex);
    if existing {
        if launch.show || !launch.autostart {
            let hwnd = unsafe { FindWindowW(HOST_CLASS, title.as_ptr()) };
            if hwnd.is_null() || unsafe { PostMessageW(hwnd, WM_OPEN_FRONTEND, 0, 0) } == 0 {
                return Err("应用已在运行，但无法唤起窗口；请从系统托盘打开。".into());
            }
        }
        return Ok(());
    }
    let com_result = unsafe { CoInitializeEx(ptr::null(), COINIT_APARTMENTTHREADED as u32) };
    if com_result < 0 {
        return Err(format!(
            "初始化 Windows 桌面组件失败（HRESULT {com_result:#x}）。"
        ));
    }
    let result = unsafe { run_host(launch, &title) };
    unsafe { CoUninitialize() };
    result
}

unsafe fn run_host(launch: Launch, title: &[u16]) -> Result<(), String> {
    let instance = GetModuleHandleW(ptr::null());
    if instance.is_null() {
        return Err(system_error("读取应用模块"));
    }
    let mut host = Box::new(Host {
        hwnd: ptr::null_mut(),
        icon: load_icon(32)?,
        small_icon: load_icon(16)?,
        backend: None,
        worker: None,
        frontend: None,
        taskbar_created: RegisterWindowMessageW(w!("TaskbarCreated")),
        tray_present: false,
        tray_signature: None,
        notified_update: None,
    });
    if host.taskbar_created == 0 {
        return Err(system_error("注册托盘恢复消息"));
    }
    let class = WNDCLASSW {
        lpfnWndProc: Some(window_proc),
        hInstance: instance,
        hIcon: host.icon.0,
        hCursor: LoadCursorW(ptr::null_mut(), IDC_ARROW),
        lpszClassName: HOST_CLASS,
        ..zeroed()
    };
    if RegisterClassW(&class) == 0 {
        return Err(system_error("注册托盘宿主窗口"));
    }
    let pointer = ptr::from_mut(&mut *host);
    let hwnd = CreateWindowExW(
        0,
        HOST_CLASS,
        title.as_ptr(),
        0,
        0,
        0,
        0,
        0,
        ptr::null_mut(),
        ptr::null_mut(),
        instance,
        pointer.cast::<c_void>(),
    );
    if hwnd.is_null() {
        UnregisterClassW(HOST_CLASS, instance);
        return Err(system_error("创建托盘宿主窗口"));
    }
    SendMessageW(hwnd, WM_SETICON, ICON_BIG as WPARAM, host.icon.0 as LPARAM);
    SendMessageW(
        hwnd,
        WM_SETICON,
        ICON_SMALL as WPARAM,
        host.small_icon.0 as LPARAM,
    );
    if let Err(error) = add_tray(pointer) {
        DestroyWindow(hwnd);
        UnregisterClassW(HOST_CLASS, instance);
        return Err(error);
    }
    let destination = hwnd as usize;
    let wake = Arc::new(move || unsafe {
        PostMessageW(destination as HWND, WM_BACKEND_EVENT, 0, 0);
    });
    match Backend::start(wake, launch.autostart) {
        Ok((backend, worker)) => {
            host.backend = Some(backend);
            host.worker = Some(worker);
        }
        Err(error) => {
            remove_tray(pointer);
            DestroyWindow(hwnd);
            UnregisterClassW(HOST_CLASS, instance);
            return Err(error);
        }
    }
    let snapshot = host.backend.as_ref().unwrap().snapshot();
    update_tray(pointer, &snapshot);
    if launch.show || !launch.autostart || !snapshot.config_error.is_empty() {
        show_frontend(pointer, Page::Mounts, None);
    }
    let mut message: MSG = zeroed();
    loop {
        let result = GetMessageW(&mut message, ptr::null_mut(), 0, 0);
        if result == 0 {
            break;
        }
        if result == -1 {
            show_error(hwnd, &system_error("读取 Windows 消息"));
            request_exit(pointer);
            continue;
        }
        if !ui::translate_message(&message) {
            TranslateMessage(&message);
            DispatchMessageW(&message);
        }
    }
    host.frontend.take();
    remove_tray(pointer);
    if IsWindow(hwnd) != 0 {
        DestroyWindow(hwnd);
    }
    UnregisterClassW(HOST_CLASS, instance);
    if let Some(worker) = host.worker.take() {
        worker
            .join()
            .map_err(|_| "后台操作线程异常结束；请核对仍在运行的 rclone 进程。".to_string())?;
    }
    Ok(())
}

unsafe extern "system" fn window_proc(
    hwnd: HWND,
    message: u32,
    wparam: WPARAM,
    lparam: LPARAM,
) -> LRESULT {
    if message == WM_NCCREATE {
        let creation = &*(lparam as *const CREATESTRUCTW);
        let pointer = creation.lpCreateParams as *mut Host;
        (*pointer).hwnd = hwnd;
        SetWindowLongPtrW(hwnd, GWLP_USERDATA, pointer as isize);
        return DefWindowProcW(hwnd, message, wparam, lparam);
    }
    let pointer = GetWindowLongPtrW(hwnd, GWLP_USERDATA) as *mut Host;
    if pointer.is_null() {
        return DefWindowProcW(hwnd, message, wparam, lparam);
    }
    if message == (*pointer).taskbar_created {
        (*pointer).tray_signature = None;
        if let Err(error) = add_tray(pointer) {
            (*pointer).tray_present = false;
            show_frontend(pointer, Page::Mounts, None);
            show_error(hwnd, &error);
        } else if let Some(backend) = (*pointer).backend.clone() {
            update_tray(pointer, &backend.snapshot());
        }
        return 0;
    }
    match message {
        WM_BACKEND_EVENT => {
            backend_event(pointer);
            0
        }
        WM_OPEN_FRONTEND => {
            show_frontend(pointer, Page::Mounts, None);
            0
        }
        WM_FRONTEND_CLOSED => {
            let closed = (*pointer)
                .frontend
                .as_ref()
                .is_some_and(|frontend| frontend.hwnd() as usize == wparam || !frontend.is_open());
            if closed {
                (*pointer).frontend.take();
            }
            if !(*pointer).tray_present {
                show_frontend(pointer, Page::Mounts, None);
            }
            0
        }
        WM_TRAY_EVENT => {
            let event = lparam as u32 & 0xffff;
            match event {
                NIN_SELECT | NIN_KEYSELECT | WM_LBUTTONDBLCLK => {
                    show_frontend(pointer, Page::Mounts, None);
                }
                WM_CONTEXTMENU | WM_RBUTTONUP => {
                    tray_menu(pointer);
                }
                _ => {}
            }
            0
        }
        WM_CLOSE => {
            request_exit(pointer);
            0
        }
        WM_DESTROY => {
            PostQuitMessage(0);
            0
        }
        WM_NCDESTROY => {
            SetWindowLongPtrW(hwnd, GWLP_USERDATA, 0);
            DefWindowProcW(hwnd, message, wparam, lparam)
        }
        _ => DefWindowProcW(hwnd, message, wparam, lparam),
    }
}

unsafe fn backend_event(pointer: *mut Host) {
    let Some(backend) = (*pointer).backend.clone() else {
        return;
    };
    let events = backend.drain_events();
    let snapshot = backend.snapshot();
    let complete = events.iter().any(|event| event.shutdown_complete);
    if let Some(frontend) = (*pointer).frontend.clone() {
        if frontend.is_open() {
            for event in &events {
                frontend.handle_event(event, snapshot.clone());
            }
            if frontend.is_open() {
                frontend.refresh(snapshot.clone());
            }
        }
    } else if let Some(event) = events.iter().rev().find(|event| !event.ok) {
        show_balloon(pointer, "rclone_tray · 操作未完成", &event.message, NIIF_ERROR);
    }
    if snapshot.update.state == UpdateState::Available
        && (*pointer).tray_present
        && (*pointer).notified_update.as_deref() != Some(snapshot.update.latest.as_str())
    {
        let message = format!(
            "发现新版本 {}（当前 {}）。\n在“关于与诊断”可一键打开下载链接。",
            snapshot.update.latest, snapshot.update.current
        );
        show_balloon(pointer, "rclone_tray · 发现新版本", &message, NIIF_INFO);
        (*pointer).notified_update = Some(snapshot.update.latest.clone());
    }
    update_tray(pointer, &snapshot);
    if complete {
        (*pointer).frontend.take();
        remove_tray(pointer);
        DestroyWindow((*pointer).hwnd);
    }
}

unsafe fn show_frontend(pointer: *mut Host, page: Page, selected: Option<String>) {
    let Some(backend) = (*pointer).backend.clone() else {
        return;
    };
    if let Some(frontend) = (*pointer).frontend.clone() {
        if frontend.is_open() {
            frontend.show(page, selected.as_deref());
            return;
        }
    }
    (*pointer).frontend.take();
    match Frontend::create((*pointer).hwnd, backend, page, selected) {
        Ok(frontend) => {
            (*pointer).frontend = Some(Arc::new(frontend));
        }
        Err(error) => show_error((*pointer).hwnd, &error),
    }
}

unsafe fn request_exit(pointer: *mut Host) {
    let Some(backend) = (*pointer).backend.clone() else {
        return;
    };
    if backend.snapshot().closing {
        return;
    }
    if let Some(frontend) = (*pointer).frontend.clone() {
        if frontend.is_open() {
            frontend.request_exit();
            return;
        }
    }
    let snapshot = backend.snapshot();
    let active = [&snapshot.gui]
        .into_iter()
        .chain(snapshot.mounts.values())
        .filter(|status| {
            status.pid.is_some()
                || matches!(
                    status.state,
                    RunState::Starting | RunState::Ready | RunState::Stopping
                )
        })
        .count();
    let prompt = utf16(&format!("将停止本应用的 {active} 个运行任务并取消延迟启动，然后退出。\n停止失败会保留托盘与进程控制权。"));
    if MessageBoxW(
        (*pointer).hwnd,
        prompt.as_ptr(),
        w!("退出 rclone_tray？"),
        MB_YESNO | MB_ICONQUESTION | MB_DEFBUTTON2 | MB_SETFOREGROUND,
    ) == IDYES
    {
        if let Err(error) = backend.send(Command::Shutdown) {
            show_error((*pointer).hwnd, &error);
        }
    }
}

unsafe fn icon_data(pointer: *mut Host, flags: u32) -> NOTIFYICONDATAW {
    let mut data: NOTIFYICONDATAW = zeroed();
    data.cbSize = size_of::<NOTIFYICONDATAW>() as u32;
    data.hWnd = (*pointer).hwnd;
    data.uID = TRAY_ID;
    data.uFlags = flags;
    data.uCallbackMessage = WM_TRAY_EVENT;
    data.hIcon = (*pointer).icon.0;
    data
}

unsafe fn add_tray(pointer: *mut Host) -> Result<(), String> {
    let mut data = icon_data(pointer, NIF_ICON | NIF_MESSAGE | NIF_TIP | NIF_SHOWTIP);
    fixed_text(&mut data.szTip, "rclone_tray · 原生托盘宿主");
    if Shell_NotifyIconW(NIM_ADD, &data) == 0 {
        return Err("无法添加系统托盘图标；请在正常 Windows 用户桌面会话中启动应用。".into());
    }
    data.Anonymous.uVersion = NOTIFYICON_VERSION_4;
    if Shell_NotifyIconW(NIM_SETVERSION, &data) == 0 {
        Shell_NotifyIconW(NIM_DELETE, &data);
        return Err("Windows 未接受托盘交互版本；未假报托盘启动成功。".into());
    }
    (*pointer).tray_present = true;
    Ok(())
}

unsafe fn remove_tray(pointer: *mut Host) {
    if (*pointer).tray_present {
        let data = icon_data(pointer, 0);
        Shell_NotifyIconW(NIM_DELETE, &data);
        (*pointer).tray_present = false;
    }
}

unsafe fn update_tray(pointer: *mut Host, snapshot: &Snapshot) {
    let signature = TraySignature {
        gui: snapshot.gui.state,
        ready: snapshot
            .mounts
            .values()
            .filter(|status| status.state == RunState::Ready)
            .count(),
        total: snapshot.config.mounts.len(),
        failed: !snapshot.config_error.is_empty()
            || !snapshot.environment.error.is_empty()
            || snapshot.gui.state == RunState::Failed
            || snapshot
                .mounts
                .values()
                .any(|status| status.state == RunState::Failed),
        closing: snapshot.closing,
    };
    if (*pointer).tray_signature.as_ref() == Some(&signature) {
        return;
    }
    let text = format!(
        "rclone_tray · GUI {} · 挂载 {}/{}{}",
        signature.gui.label(),
        signature.ready,
        signature.total,
        if signature.closing {
            " · 正在退出"
        } else if signature.failed {
            " · 请查看诊断"
        } else {
            ""
        }
    );
    let mut data = icon_data(pointer, NIF_TIP | NIF_SHOWTIP);
    fixed_text(&mut data.szTip, &text);
    if Shell_NotifyIconW(NIM_MODIFY, &data) != 0 {
        (*pointer).tray_signature = Some(signature);
    }
}

unsafe fn show_balloon(pointer: *mut Host, title: &str, message: &str, flags: u32) {
    if !(*pointer).tray_present {
        return;
    }
    let mut data = icon_data(pointer, NIF_INFO);
    fixed_text(&mut data.szInfoTitle, title);
    fixed_text(&mut data.szInfo, message);
    data.dwInfoFlags = flags;
    Shell_NotifyIconW(NIM_MODIFY, &data);
}

enum MenuAction {
    Show(Page, Option<String>),
    Command(Command),
    Exit,
}
struct Popup {
    handle: HMENU,
    actions: Vec<MenuAction>,
}
impl Popup {
    unsafe fn new() -> Result<Self, String> {
        let handle = CreatePopupMenu();
        if handle.is_null() {
            Err(system_error("创建托盘菜单"))
        } else {
            Ok(Self {
                handle,
                actions: Vec::new(),
            })
        }
    }
    unsafe fn item(
        &mut self,
        label: &str,
        action: MenuAction,
        enabled: bool,
        checked: bool,
    ) -> Result<(), String> {
        let id = self.actions.len() + 1;
        let label = utf16(label);
        let flags =
            MF_STRING | if enabled { 0 } else { MF_GRAYED } | if checked { MF_CHECKED } else { 0 };
        if AppendMenuW(self.handle, flags, id, label.as_ptr()) == 0 {
            return Err(system_error("添加托盘菜单项"));
        }
        self.actions.push(action);
        Ok(())
    }
    unsafe fn separator(&self) -> Result<(), String> {
        if AppendMenuW(self.handle, MF_SEPARATOR, 0, ptr::null()) == 0 {
            Err(system_error("添加菜单分隔线"))
        } else {
            Ok(())
        }
    }
}
impl Drop for Popup {
    fn drop(&mut self) {
        if !self.handle.is_null() {
            unsafe {
                DestroyMenu(self.handle);
            }
        }
    }
}

unsafe fn build_menu(snapshot: &Snapshot) -> Result<Popup, String> {
    let mut menu = Popup::new()?;
    let enabled = !snapshot.closing;
    menu.item(
        "打开挂载管理",
        MenuAction::Show(Page::Mounts, None),
        true,
        false,
    )?;
    menu.item(
        "rclone GUI",
        MenuAction::Show(Page::Service, None),
        true,
        false,
    )?;
    menu.item("设置", MenuAction::Show(Page::Settings, None), true, false)?;
    menu.item(
        "关于与诊断",
        MenuAction::Show(Page::About, None),
        true,
        false,
    )?;
    menu.separator()?;
    let gui_owned = snapshot.gui.pid.is_some();
    menu.item(
        if gui_owned {
            "停止 GUI"
        } else {
            "启动 GUI"
        },
        MenuAction::Command(if gui_owned {
            Command::StopGui
        } else {
            Command::StartGui
        }),
        enabled && !snapshot.busy.contains("gui"),
        false,
    )?;
    menu.item(
        "打开当前 GUI 网页",
        MenuAction::Command(Command::OpenGui),
        enabled && snapshot.gui.state == RunState::Ready,
        false,
    )?;
    menu.item(
        "启动全部挂载",
        MenuAction::Command(Command::StartAllMounts),
        enabled && !snapshot.config.mounts.is_empty() && !snapshot.busy.contains("mounts"),
        false,
    )?;
    menu.item(
        "停止全部挂载",
        MenuAction::Command(Command::StopAllMounts),
        enabled && !snapshot.config.mounts.is_empty() && !snapshot.busy.contains("mounts"),
        false,
    )?;
    if !snapshot.config.mounts.is_empty() {
        menu.separator()?;
        for mount in &snapshot.config.mounts {
            let status = snapshot.mounts.get(&mount.id);
            let running = status.is_some_and(|status| status.pid.is_some());
            let state = status.map(|status| status.state).unwrap_or_default();
            menu.item(
                &format!(
                    "{} · {} · {}",
                    mount.name,
                    state.label(),
                    if running { "停止" } else { "启动" }
                ),
                MenuAction::Command(if running {
                    Command::StopMount(mount.id.clone())
                } else {
                    Command::StartMount(mount.id.clone())
                }),
                enabled && !snapshot.busy.contains(&mount.id) && !snapshot.busy.contains("mounts"),
                false,
            )?;
        }
    }
    menu.separator()?;
    menu.item(
        "打开 rclone 配置",
        MenuAction::Command(Command::OpenRcloneConfig),
        enabled,
        false,
    )?;
    menu.item(
        "系统登录自启",
        MenuAction::Command(Command::SetStartup(!snapshot.config.startup_enabled)),
        enabled && !snapshot.busy.contains("settings"),
        snapshot.config.startup_enabled,
    )?;
    menu.item(
        "开机自动挂载",
        MenuAction::Command(Command::SetAutoMount(!snapshot.config.auto_mount_on_boot)),
        enabled && !snapshot.busy.contains("settings"),
        snapshot.config.auto_mount_on_boot,
    )?;
    menu.item(
        "GUI 随应用启动",
        MenuAction::Command(Command::SetGuiAutoStart(!snapshot.config.gui_auto_start)),
        enabled && !snapshot.busy.contains("settings"),
        snapshot.config.gui_auto_start,
    )?;
    menu.separator()?;
    menu.item("退出应用…", MenuAction::Exit, enabled, false)?;
    Ok(menu)
}

unsafe fn tray_menu(pointer: *mut Host) {
    let Some(backend) = (*pointer).backend.clone() else {
        return;
    };
    let snapshot = backend.snapshot();
    let mut menu = match build_menu(&snapshot) {
        Ok(menu) => menu,
        Err(error) => {
            show_error((*pointer).hwnd, &error);
            return;
        }
    };
    let hwnd = (*pointer).hwnd;
    let mut point = zeroed();
    GetCursorPos(&mut point);
    SetForegroundWindow(hwnd);
    let selected = TrackPopupMenuEx(
        menu.handle,
        TPM_RETURNCMD | TPM_NONOTIFY | TPM_RIGHTBUTTON,
        point.x,
        point.y,
        hwnd,
        ptr::null(),
    ) as usize;
    PostMessageW(hwnd, WM_NULL, 0, 0);
    if selected == 0 || selected > menu.actions.len() {
        return;
    }
    match menu.actions.swap_remove(selected - 1) {
        MenuAction::Show(page, id) => show_frontend(pointer, page, id),
        MenuAction::Command(command) => {
            if let Err(error) = backend.send(command) {
                show_error(hwnd, &error);
            }
        }
        MenuAction::Exit => request_exit(pointer),
    }
}

fn instance_namespace() -> Result<String, String> {
    let value = match std::env::var_os("RCLONE_TRAY_DATA_DIR").filter(|value| !value.is_empty()) {
        Some(value) => {
            let path = std::path::PathBuf::from(value);
            let path = if path.is_absolute() {
                path
            } else {
                std::env::current_dir()
                    .map_err(|error| format!("无法确定配置目录：{error}"))?
                    .join(path)
            };
            path.to_string_lossy().replace('/', "\\").to_lowercase()
        }
        None => return Ok(String::new()),
    };
    let digest = Sha256::digest(value.as_bytes());
    let mut result = String::with_capacity(24);
    use std::fmt::Write;
    for byte in &digest[..12] {
        let _ = write!(result, "{byte:02x}");
    }
    Ok(result)
}

fn utf16(value: &str) -> Vec<u16> {
    value.encode_utf16().chain(Some(0)).collect()
}
fn fixed_text<const N: usize>(buffer: &mut [u16; N], value: &str) {
    let mut length = 0;
    for character in value.chars() {
        let mut encoded = [0; 2];
        let units = character.encode_utf16(&mut encoded);
        if length + units.len() >= N {
            break;
        }
        buffer[length..length + units.len()].copy_from_slice(units);
        length += units.len();
    }
    if length < N {
        buffer[length] = 0;
    }
}
fn system_error(action: &str) -> String {
    format!("{action}失败（Windows 错误 {}）。", unsafe {
        GetLastError()
    })
}
pub fn show_error(owner: HWND, error: &str) {
    let error = utf16(error);
    unsafe {
        MessageBoxW(
            owner,
            error.as_ptr(),
            w!("rclone_tray"),
            MB_OK | MB_ICONERROR | MB_SETFOREGROUND,
        );
    }
}

fn load_icon(size: i32) -> Result<OwnedIcon, String> {
    let bytes = include_bytes!("../assets/icon.ico");
    if bytes.len() < 6 || bytes[..4] != [0, 0, 1, 0] {
        return Err("内置图标资源格式无效。".into());
    }
    let count = u16::from_le_bytes([bytes[4], bytes[5]]) as usize;
    let mut selected = None;
    let mut best_rank = usize::MAX;
    for index in 0..count {
        let offset = 6 + index * 16;
        let Some(entry) = bytes.get(offset..offset + 16) else {
            return Err("内置图标目录不完整。".into());
        };
        let width = if entry[0] == 0 {
            256
        } else {
            entry[0] as usize
        };
        let rank = if width >= size as usize {
            width - size as usize
        } else {
            1024 + size as usize - width
        };
        if rank < best_rank {
            let length = u32::from_le_bytes(entry[8..12].try_into().unwrap()) as usize;
            let start = u32::from_le_bytes(entry[12..16].try_into().unwrap()) as usize;
            selected = Some((start, length));
            best_rank = rank;
        }
    }
    let (start, length) = selected.ok_or_else(|| "内置图标没有图像。".to_string())?;
    let end = start
        .checked_add(length)
        .ok_or_else(|| "内置图标尺寸无效。".to_string())?;
    let image = bytes
        .get(start..end)
        .ok_or_else(|| "内置图标图像不完整。".to_string())?;
    let icon = unsafe {
        CreateIconFromResourceEx(
            image.as_ptr(),
            length as u32,
            1,
            0x00030000,
            size,
            size,
            LR_DEFAULTCOLOR,
        )
    };
    if icon.is_null() {
        Err(system_error("创建应用图标"))
    } else {
        Ok(OwnedIcon(icon))
    }
}
