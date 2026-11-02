use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::os::windows::fs::MetadataExt;
use std::path::{Component, Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Arc, Mutex, MutexGuard};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use std::os::windows::process::CommandExt;
use unicode_casefold::UnicodeCaseFold;
use windows_sys::Win32::Storage::FileSystem::{GetLogicalDrives, FILE_ATTRIBUTE_REPARSE_POINT};
use windows_sys::Win32::System::Com::{CoInitializeEx, CoUninitialize, COINIT_APARTMENTTHREADED};
use windows_sys::Win32::UI::Shell::ShellExecuteW;
use windows_sys::Win32::UI::WindowsAndMessaging::SW_SHOWNORMAL;

use crate::config::{AppConfig, ConfigStore, Fingerprint, MountConfig};
use crate::contracts::{Command, EnvironmentInfo, Event, RunState, RuntimeStatus, Snapshot};
use crate::{environment, runtime, startup};

type Wake = Arc<dyn Fn() + Send + Sync>;

struct Shared {
    snapshot: Arc<Snapshot>,
    events: VecDeque<Event>,
    next_request_id: u64,
}

struct Request {
    id: u64,
    command: Command,
}

/// Owns no frontend objects. Every clone can outlive a destroyed view safely.
#[derive(Clone)]
pub struct Backend {
    tx: mpsc::Sender<Request>,
    shared: Arc<Mutex<Shared>>,
    cancel: Arc<AtomicBool>,
    wake: Wake,
}

impl Backend {
    pub fn start(wake: Wake, autostart: bool) -> Result<(Self, JoinHandle<()>), String> {
        let store = ConfigStore::new();
        let (mut cfg, fingerprint, config_error) = match store.load() {
            Ok((cfg, fingerprint)) => (cfg, fingerprint, String::new()),
            Err(error) => (AppConfig::default(), None, error),
        };
        let startup_error = match startup::is_enabled() {
            Ok(enabled) => {
                cfg.startup_enabled = enabled;
                String::new()
            }
            Err(error) => error,
        };
        let mounts = cfg
            .mounts
            .iter()
            .map(|mount| (mount.id.clone(), RuntimeStatus::default()))
            .collect();
        let shared = Arc::new(Mutex::new(Shared {
            snapshot: Arc::new(Snapshot {
                config: cfg.clone(),
                revision: 0,
                config_error,
                environment: EnvironmentInfo::default(),
                gui: RuntimeStatus::default(),
                mounts,
                update: crate::update::UpdateInfo::default(),
                busy: BTreeSet::new(),
                closing: false,
                message: "正在检测运行环境。".into(),
            }),
            events: VecDeque::new(),
            next_request_id: 0,
        }));
        let (tx, rx) = mpsc::channel();
        let backend = Self {
            tx,
            shared,
            cancel: Arc::new(AtomicBool::new(false)),
            wake,
        };
        let worker_backend = backend.clone();
        let handle = thread::Builder::new()
            .name("rclone-operation".into())
            .spawn(move || {
                let com_result =
                    unsafe { CoInitializeEx(std::ptr::null(), COINIT_APARTMENTTHREADED as u32) };
                let notify_backend = worker_backend.clone();
                let notify: runtime::Notifier = Arc::new(move |key, status| {
                    notify_backend.update(|snapshot| {
                        if key == "gui" {
                            snapshot.gui = status;
                        } else if let Some(existing) = snapshot.mounts.get_mut(key) {
                            *existing = status;
                        }
                    });
                });
                let pm = runtime::ProcessManager::new(&cfg, worker_backend.cancel.clone(), notify);
                let deadline = if autostart
                    && cfg.auto_mount_on_boot
                    && worker_backend.snapshot().config_error.is_empty()
                {
                    Instant::now().checked_add(Duration::from_secs(cfg.mount_delay_seconds.into()))
                } else {
                    None
                };
                let mut worker = Worker {
                    backend: worker_backend,
                    store,
                    cfg,
                    fingerprint,
                    pm,
                    deadline,
                    startup_error,
                };
                worker.run(rx);
                if com_result >= 0 {
                    unsafe { CoUninitialize() };
                }
            })
            .map_err(|error| format!("无法启动后台操作线程：{error}"))?;
        backend.send(Command::RefreshEnvironment)?;
        let initial = backend.snapshot();
        if initial.config_error.is_empty() && initial.config.gui_auto_start {
            backend.send(Command::StartGui)?;
        }
        backend.send(Command::CheckUpdate { quiet: true })?;
        Ok((backend, handle))
    }

    fn shared(&self) -> MutexGuard<'_, Shared> {
        self.shared
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    pub fn snapshot(&self) -> Arc<Snapshot> {
        self.shared().snapshot.clone()
    }

    fn update(&self, operation: impl FnOnce(&mut Snapshot)) {
        {
            let mut shared = self.shared();
            operation(Arc::make_mut(&mut shared.snapshot));
        }
        (self.wake)();
    }

    pub fn send(&self, command: Command) -> Result<u64, String> {
        let key = command.key();
        let shutdown = matches!(command, Command::Shutdown);
        let request_id;
        {
            let mut shared = self.shared();
            let current = &shared.snapshot;
            if current.closing || current.busy.contains("shutdown") {
                return Err("应用正在退出，未执行新操作。".into());
            }
            if current.busy.contains(&key) {
                return Err("同类操作正在进行，请等待结果。".into());
            }
            request_id = shared
                .next_request_id
                .checked_add(1)
                .ok_or_else(|| "操作序号已耗尽；请重新打开应用。".to_string())?;
            shared.next_request_id = request_id;
            let snapshot = Arc::make_mut(&mut shared.snapshot);
            snapshot.busy.insert(key.clone());
            if shutdown {
                snapshot.closing = true;
                self.cancel.store(true, Ordering::Release);
            }
        }
        if self
            .tx
            .send(Request {
                id: request_id,
                command,
            })
            .is_err()
        {
            self.update(|snapshot| {
                snapshot.busy.remove(&key);
                if shutdown {
                    snapshot.closing = false;
                }
            });
            if shutdown {
                self.cancel.store(false, Ordering::Release);
            }
            return Err("后台操作线程已结束；请退出并重新打开应用。".into());
        }
        (self.wake)();
        Ok(request_id)
    }

    pub fn drain_events(&self) -> Vec<Event> {
        self.shared().events.drain(..).collect()
    }

    fn finish(
        &self,
        key: String,
        action: &str,
        request_id: Option<u64>,
        result: Result<String, String>,
        shutdown_complete: bool,
    ) {
        let (ok, message) = match result {
            Ok(message) => (true, message),
            Err(message) => (false, message),
        };
        {
            let mut shared = self.shared();
            let snapshot = Arc::make_mut(&mut shared.snapshot);
            snapshot.busy.remove(&key);
            snapshot.message = message.clone();
            if action == "shutdown" && !ok {
                snapshot.closing = false;
            }
            shared.events.push_back(Event {
                key,
                action: action.into(),
                request_id,
                ok,
                message,
                shutdown_complete,
            });
        }
        (self.wake)();
    }
}

struct Worker {
    backend: Backend,
    store: ConfigStore,
    cfg: AppConfig,
    fingerprint: Fingerprint,
    pm: runtime::ProcessManager,
    deadline: Option<Instant>,
    startup_error: String,
}

impl Worker {
    fn run(&mut self, rx: mpsc::Receiver<Request>) {
        loop {
            if self
                .deadline
                .is_some_and(|deadline| Instant::now() >= deadline)
            {
                self.deadline = None;
                if self.cfg.auto_mount_on_boot && !self.backend.cancel.load(Ordering::Acquire) {
                    self.auto_mount();
                }
            }
            let polling = self.pm.has_live_children();
            let timeout = match (polling, self.deadline) {
                (true, Some(deadline)) => Some(
                    deadline
                        .saturating_duration_since(Instant::now())
                        .min(Duration::from_millis(500)),
                ),
                (true, None) => Some(Duration::from_millis(500)),
                (false, Some(deadline)) => Some(deadline.saturating_duration_since(Instant::now())),
                (false, None) => None,
            };
            let received = if let Some(timeout) = timeout {
                rx.recv_timeout(timeout)
            } else {
                rx.recv().map_err(|_| mpsc::RecvTimeoutError::Disconnected)
            };
            match received {
                Ok(Request {
                    command,
                    id: request_id,
                }) => {
                    self.pm.poll_exits();
                    let key = command.key();
                    let action = command.action();
                    if self.backend.cancel.load(Ordering::Acquire)
                        && !matches!(command, Command::Shutdown)
                    {
                        self.backend.finish(
                            key,
                            action,
                            Some(request_id),
                            Err("应用正在退出；已取消排队操作。".into()),
                            false,
                        );
                        continue;
                    }
                    let shutdown = matches!(command, Command::Shutdown);
                    let result = self.perform(command);
                    self.publish_statuses();
                    let complete = shutdown && result.is_ok();
                    if shutdown && !complete {
                        self.backend.cancel.store(false, Ordering::Release);
                    }
                    self.backend
                        .finish(key, action, Some(request_id), result, complete);
                    if complete {
                        return;
                    }
                }
                Err(mpsc::RecvTimeoutError::Timeout) => {
                    if self.pm.poll_exits() {
                        self.publish_statuses();
                    }
                }
                Err(mpsc::RecvTimeoutError::Disconnected) => {
                    self.backend.cancel.store(true, Ordering::Release);
                    self.pm.stop_all();
                    return;
                }
            }
        }
    }

    fn auto_mount(&mut self) {
        let snapshot = self.backend.snapshot();
        if !snapshot.config_error.is_empty() || snapshot.busy.contains("mounts") || snapshot.closing
        {
            return;
        }
        self.backend.update(|snapshot| {
            snapshot.busy.insert("mounts".into());
        });
        let result = batch_result(self.pm.start_all_mounts(true));
        self.publish_statuses();
        self.backend
            .finish("mounts".into(), "auto_mount", None, result, false);
    }

    fn publish_statuses(&self) {
        let (gui, mounts) = self.pm.statuses();
        self.backend.update(|snapshot| {
            snapshot.gui = gui;
            snapshot.mounts = mounts;
        });
    }

    fn publish_config(&self, clear_error: bool) {
        self.backend.update(|snapshot| {
            snapshot.config = self.cfg.clone();
            snapshot.revision += 1;
            if clear_error {
                snapshot.config_error.clear();
            }
        });
    }

    fn refresh_environment(&mut self) {
        let mut info = environment::inspect(&self.cfg);
        if !self.startup_error.is_empty() {
            if !info.error.is_empty() {
                info.error.push('\n');
            }
            info.error.push_str(&self.startup_error);
        }
        self.backend.update(|snapshot| {
            snapshot.environment = info;
        });
    }

    fn check_revision(&self, revision: u64) -> Result<(), String> {
        let snapshot = self.backend.snapshot();
        if !snapshot.config_error.is_empty() {
            return Err("配置无法读取；请修复原文件并重新载入，未覆盖配置。".into());
        }
        if revision != snapshot.revision {
            return Err("配置已被另一操作修改；未保存草稿。请重新载入后显式保留输入。".into());
        }
        if self.store.fingerprint()? != self.fingerprint {
            return Err("配置已在外部修改；未覆盖文件。请重新载入后再保存。".into());
        }
        Ok(())
    }

    fn commit(
        &mut self,
        candidate: AppConfig,
        revision: u64,
        registry: bool,
    ) -> Result<(), String> {
        self.check_revision(revision)?;
        candidate.validate()?;
        let previous = self.cfg.clone();
        let old_entry = if registry {
            startup::read_entry()?
        } else {
            None
        };
        self.pm.update_config(&candidate)?;
        let save_result = (|| {
            if registry {
                if candidate.startup_enabled {
                    let executable = std::env::current_exe()
                        .map_err(|error| format!("无法确定应用路径：{error}"))?;
                    startup::enable(&executable)?;
                } else if old_entry.is_some() {
                    startup::disable()?;
                }
            }
            self.store.save(&candidate, self.fingerprint)
        })();
        match save_result {
            Ok(fingerprint) => {
                self.cfg = candidate;
                self.fingerprint = fingerprint;
                if registry {
                    self.startup_error.clear();
                }
                if !self.cfg.auto_mount_on_boot {
                    self.deadline = None;
                }
                self.publish_config(true);
                Ok(())
            }
            Err(error) => {
                let runtime_rollback = self.pm.update_config(&previous);
                let registry_rollback = if registry {
                    startup::restore(old_entry.as_deref())
                } else {
                    Ok(())
                };
                if let Err(rollback) = runtime_rollback {
                    return Err(format!(
                        "{error}\n运行配置回滚失败：{rollback}；仍保留进程管理关系。"
                    ));
                }
                if let Err(rollback) = registry_rollback {
                    self.startup_error =
                        format!("系统自启回滚失败：{rollback}；请检查真实注册表状态。");
                    if let Ok(enabled) = startup::is_enabled() {
                        self.cfg.startup_enabled = enabled;
                        self.publish_config(false);
                    }
                    self.refresh_environment();
                    return Err(format!("{error}\n{}", self.startup_error));
                }
                Err(error)
            }
        }
    }

    fn validate_settings(&self, cfg: &AppConfig) -> Result<(), String> {
        cfg.validate()?;
        let executable = environment::find_rclone(&cfg.rclone_path)?;
        environment::get_version(&executable)?;
        if !cfg.rclone_conf.is_empty() {
            environment::find_rclone_conf(&cfg.rclone_conf)?;
        }
        if cfg
            .gui_user
            .chars()
            .chain(cfg.gui_pass.chars())
            .any(|character| matches!(character, '\r' | '\n' | '\0'))
        {
            return Err("用户名和密码不能包含换行或空字符。".into());
        }
        Ok(())
    }

    fn validate_mount(&self, mount: &MountConfig) -> Result<(), String> {
        mount.validate()?;
        let remote_name = mount
            .remote
            .split_once(':')
            .filter(|(name, _)| !name.is_empty())
            .map(|(name, _)| format!("{name}:"))
            .ok_or_else(|| "Remote 格式应为名称:路径，例如 cloud:folder。".to_string())?;
        let conf = environment::find_rclone_conf(&self.cfg.rclone_conf)?;
        if !environment::list_remotes(conf.as_deref())?.contains(&remote_name) {
            return Err("Remote 不存在于当前 rclone 配置；请配置后刷新。".into());
        }
        if mount
            .mount_point
            .chars()
            .chain(mount.remote.chars())
            .chain(mount.volname.chars())
            .any(|character| matches!(character, '\r' | '\n' | '\0'))
        {
            return Err("挂载参数不能包含换行或空字符。".into());
        }
        let point = Path::new(&mount.mount_point);
        let drive = explicit_drive(&mount.mount_point);
        let current_owned = self.cfg.mounts.iter().any(|existing| {
            existing.id == mount.id
                && normalized_point(&existing.mount_point) == normalized_point(&mount.mount_point)
                && self.pm.owned_mount(&existing.id)
        });
        if let Some(letter) = drive {
            if unsafe { GetLogicalDrives() } & (1 << (letter - b'A')) != 0 && !current_owned {
                return Err("所选盘符已被占用；请选择未使用盘符。".into());
            }
        } else {
            if !point.is_absolute() || !point.parent().is_some_and(Path::is_dir) {
                return Err("挂载点须为 C:–Z: 盘符或父目录存在的绝对目录路径。".into());
            }
            if !current_owned {
                match std::fs::symlink_metadata(point) {
                    Ok(info) => {
                        if !info.is_dir()
                            || info.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0
                        {
                            return Err(
                                "挂载目录必须是普通空目录；不能是文件、链接或重解析点。".into()
                            );
                        }
                        let mut entries = std::fs::read_dir(point)
                            .map_err(|error| format!("无法检查挂载目录：{error}"))?;
                        if let Some(entry) = entries.next() {
                            entry.map_err(|error| format!("无法读取挂载目录：{error}"))?;
                            return Err("挂载目录必须为空；不会删除其中的文件。".into());
                        }
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
                    Err(error) => return Err(format!("无法检查挂载目录：{error}")),
                }
            }
        }
        if (mount.network_mode || mount.volname.starts_with("\\\\")) && drive.is_none() {
            return Err("网络模式只能挂载到明确盘符。".into());
        }
        for existing in self
            .cfg
            .mounts
            .iter()
            .filter(|existing| existing.id != mount.id)
        {
            if existing.name.case_fold().eq(mount.name.case_fold()) {
                return Err("挂载名称已存在，请使用不同名称。".into());
            }
            if normalized_point(&existing.mount_point) == normalized_point(&mount.mount_point) {
                return Err("挂载点已被其他配置使用。".into());
            }
            if !mount.volname.is_empty()
                && (mount.network_mode || mount.volname.starts_with("\\\\"))
                && (existing.network_mode || existing.volname.starts_with("\\\\"))
                && share_name(existing) == share_name(mount)
            {
                return Err("网络共享名已被其他挂载使用，请修改卷标。".into());
            }
        }
        Ok(())
    }

    fn perform(&mut self, command: Command) -> Result<String, String> {
        match command {
            Command::RefreshEnvironment => {
                self.refresh_environment();
                let info = &self.backend.snapshot().environment;
                if info.error.is_empty() {
                    Ok("环境已刷新。".into())
                } else {
                    Err(info.error.clone())
                }
            }
            Command::ReloadConfig => {
                let (mut candidate, fingerprint) = self.store.load().map_err(|error| {
                    self.backend.update(|snapshot| {
                        snapshot.config_error = error.clone();
                    });
                    error
                })?;
                match startup::is_enabled() {
                    Ok(enabled) => {
                        candidate.startup_enabled = enabled;
                        self.startup_error.clear();
                    }
                    Err(error) => self.startup_error = error,
                }
                self.pm.update_config(&candidate)?;
                self.cfg = candidate;
                self.fingerprint = fingerprint;
                if !self.cfg.auto_mount_on_boot {
                    self.deadline = None;
                }
                self.publish_config(true);
                self.refresh_environment();
                Ok("配置已重新读取；未覆盖任何文件。".into())
            }
            Command::SaveSettings {
                mut draft,
                revision,
            } => {
                draft.mounts = self.cfg.mounts.clone();
                self.validate_settings(&draft)?;
                self.commit(draft, revision, true)?;
                self.refresh_environment();
                Ok("设置已保存；运行实例未重新启动。".into())
            }
            Command::SaveMount {
                mut draft,
                revision,
            } => {
                draft.name = draft.name.trim().into();
                draft.remote = draft.remote.trim().into();
                draft.mount_point = draft.mount_point.trim().into();
                self.check_revision(revision)?;
                self.validate_mount(&draft)?;
                let name = draft.name.clone();
                let mut candidate = self.cfg.clone();
                if let Some(existing) = candidate
                    .mounts
                    .iter_mut()
                    .find(|mount| mount.id == draft.id)
                {
                    *existing = draft;
                } else {
                    candidate.mounts.push(draft);
                }
                self.commit(candidate, revision, false)?;
                Ok(format!("「{name}」配置已保存；未自动启动挂载。"))
            }
            Command::DeleteMount { id, revision } => {
                let mut candidate = self.cfg.clone();
                let index = candidate
                    .mounts
                    .iter()
                    .position(|mount| mount.id == id)
                    .ok_or_else(|| "挂载配置已不存在，请刷新列表。".to_string())?;
                let mount = candidate.mounts.remove(index);
                self.commit(candidate, revision, false)?;
                Ok(format!("「{}」配置已删除；未删除远端文件。", mount.name))
            }
            Command::StartMount(id) => status_result(self.pm.start_mount(&id)),
            Command::StopMount(id) => status_result(self.pm.stop_mount(&id)),
            Command::StartAllMounts => batch_result(self.pm.start_all_mounts(false)),
            Command::StopAllMounts => batch_result(self.pm.stop_all_mounts()),
            Command::StartGui => status_result(self.pm.start_gui()),
            Command::StopGui => status_result(self.pm.stop_gui()),
            Command::OpenGui => {
                let url = self
                    .pm
                    .gui_url()
                    .ok_or_else(|| "GUI 尚未就绪；请先启动服务。".to_string())?;
                shell_open(&url)?;
                Ok("已打开当前 GUI；请在网页中输入认证凭据。".into())
            }
            Command::OpenMount(id) => {
                let (_, statuses) = self.pm.statuses();
                if !statuses
                    .get(&id)
                    .is_some_and(|status| status.state == RunState::Ready)
                {
                    return Err("挂载尚未就绪；请先启动并等待。".into());
                }
                let mount = self
                    .cfg
                    .mounts
                    .iter()
                    .find(|mount| mount.id == id)
                    .ok_or_else(|| "挂载配置已不存在。".to_string())?;
                let target = if explicit_drive(&mount.mount_point).is_some() {
                    format!("{}\\", mount.mount_point)
                } else {
                    mount.mount_point.clone()
                };
                open_path(Path::new(&target))?;
                Ok("已在资源管理器打开挂载位置。".into())
            }
            Command::OpenPath(path) => {
                open_path(&path)?;
                Ok("已打开目标位置。".into())
            }
            Command::OpenUrl(url) => {
                if !url.starts_with("https://") && !url.starts_with("http://") {
                    return Err("帮助链接必须是 HTTP(S) 地址。".into());
                }
                shell_open(&url)?;
                Ok("已在浏览器打开链接。".into())
            }
            Command::OpenRcloneConfig => {
                let path =
                    environment::find_rclone_conf(&self.cfg.rclone_conf)?.ok_or_else(|| {
                        "未找到 rclone.conf；请先使用 rclone config 配置远端。".to_string()
                    })?;
                open_path(&path)?;
                Ok("已打开 rclone 配置。".into())
            }
            Command::SetStartup(enabled)
            | Command::SetAutoMount(enabled)
            | Command::SetGuiAutoStart(enabled) => {
                let mut candidate = self.cfg.clone();
                match command {
                    Command::SetStartup(_) => candidate.startup_enabled = enabled,
                    Command::SetAutoMount(_) => candidate.auto_mount_on_boot = enabled,
                    Command::SetGuiAutoStart(_) => candidate.gui_auto_start = enabled,
                    _ => unreachable!(),
                }
                self.validate_settings(&candidate)?;
                let revision = self.backend.snapshot().revision;
                self.commit(candidate, revision, true)?;
                self.refresh_environment();
                Ok("启动偏好已保存。".into())
            }
            Command::CheckUpdate { quiet } => {
                self.backend.update(|snapshot| {
                    snapshot.update.state = crate::update::UpdateState::Checking;
                    snapshot.update.message = "正在检查更新…".into();
                });
                match crate::update::check(crate::version::CURRENT) {
                    Ok(info) => {
                        let message = info.message.clone();
                        self.backend.update(|snapshot| snapshot.update = info);
                        Ok(message)
                    }
                    Err(error) => {
                        self.backend.update(|snapshot| {
                            snapshot.update.state = crate::update::UpdateState::Failed;
                            snapshot.update.message = error.clone();
                            snapshot.update.checked_at = crate::version::now_stamp();
                            snapshot.update.download_url =
                                crate::update::RELEASES_PAGE.to_string();
                        });
                        let message = format!("更新检查未完成：{error}");
                        // A startup check must not raise a tray error balloon when
                        // offline; the About page keeps the factual reason.
                        if quiet {
                            Ok(message)
                        } else {
                            Err(message)
                        }
                    }
                }
            }
            Command::Shutdown => {
                self.deadline = None;
                let result = batch_result(self.pm.stop_all());
                let (gui, mounts) = self.pm.statuses();
                if self.pm.owned_gui()
                    || self
                        .cfg
                        .mounts
                        .iter()
                        .any(|mount| self.pm.owned_mount(&mount.id))
                    || gui.pid.is_some()
                    || mounts.values().any(|status| status.pid.is_some())
                {
                    return Err(
                        "仍有本应用进程、挂载或空目录恢复未完成；保留托盘和控制权，请重试停止。"
                            .into(),
                    );
                }
                result.map(|_| "所有本应用任务已停止。".into())
            }
        }
    }
}

fn explicit_drive(point: &str) -> Option<u8> {
    let bytes = point.as_bytes();
    if bytes.len() == 2 && bytes[1] == b':' {
        let letter = bytes[0].to_ascii_uppercase();
        if (b'C'..=b'Z').contains(&letter) {
            return Some(letter);
        }
    }
    None
}

fn normalized_point(point: &str) -> String {
    let mut normalized = PathBuf::new();
    for component in Path::new(point).components() {
        match component {
            Component::CurDir => {}
            Component::ParentDir => {
                normalized.pop();
            }
            component => normalized.push(component.as_os_str()),
        }
    }
    normalized
        .to_string_lossy()
        .trim_end_matches(['\\', '/'])
        .to_lowercase()
}

fn share_name(mount: &MountConfig) -> String {
    let name = if mount.volname.is_empty() {
        format!("\\\\rclone-tray\\{}", mount.id)
    } else if mount.volname.starts_with("\\\\") {
        mount.volname.clone()
    } else {
        format!("\\\\server\\{}", mount.volname)
    };
    name.trim_end_matches(['\\', '/']).to_lowercase()
}

fn status_result(status: RuntimeStatus) -> Result<String, String> {
    if matches!(status.state, RunState::Ready | RunState::Stopped) {
        Ok(status.message)
    } else {
        Err(status.message)
    }
}

fn batch_result(statuses: BTreeMap<String, RuntimeStatus>) -> Result<String, String> {
    let failures: Vec<_> = statuses
        .iter()
        .filter(|(_, status)| !matches!(status.state, RunState::Ready | RunState::Stopped))
        .collect();
    if failures.is_empty() {
        return Ok(format!("已完成 {} 项。", statuses.len()));
    }
    let detail = failures
        .iter()
        .map(|(key, status)| format!("{key}：{}", status.message))
        .collect::<Vec<_>>()
        .join("\n");
    Err(format!(
        "完成 {}/{} 项；失败项：\n{detail}",
        statuses.len() - failures.len(),
        statuses.len()
    ))
}

fn open_path(path: &Path) -> Result<(), String> {
    if !path.exists() {
        return Err("目标文件或目录不存在；请检查路径。".into());
    }
    let text_file = path.is_file()
        && path.extension().is_some_and(|extension| {
            ["log", "json", "conf"]
                .iter()
                .any(|expected| extension.eq_ignore_ascii_case(expected))
        });
    if text_file {
        let system_root = std::env::var_os("SystemRoot")
            .ok_or_else(|| "无法确定系统文本编辑器位置。".to_string())?;
        std::process::Command::new(
            PathBuf::from(system_root)
                .join("System32")
                .join("notepad.exe"),
        )
        .arg(path)
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .creation_flags(windows_sys::Win32::System::Threading::CREATE_NO_WINDOW)
        .spawn()
        .map_err(|error| format!("系统文本编辑器无法打开文件：{error}"))?;
        Ok(())
    } else {
        shell_open(&path.to_string_lossy())
    }
}

fn shell_open(target: &str) -> Result<(), String> {
    let wide: Vec<u16> = target.encode_utf16().chain(Some(0)).collect();
    let verb: Vec<u16> = "open".encode_utf16().chain(Some(0)).collect();
    let result = unsafe {
        ShellExecuteW(
            std::ptr::null_mut(),
            verb.as_ptr(),
            wide.as_ptr(),
            std::ptr::null(),
            std::ptr::null(),
            SW_SHOWNORMAL,
        )
    };
    if result as usize <= 32 {
        return Err(format!(
            "系统无法打开目标（Shell 错误 {}）。",
            result as usize
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn mount_paths_normalize_case_and_dot_segments() {
        assert_eq!(
            normalized_point("E:\\mounts\\old\\..\\DATA\\"),
            normalized_point("e:/mounts/data")
        );
        assert_ne!(normalized_point("E:\\data"), normalized_point("F:\\data"));
    }

    #[test]
    fn network_share_identity_is_case_insensitive_and_trims_separator() {
        let mut one = MountConfig::new("one".into(), "local:".into(), "X:".into()).unwrap();
        one.volname = "\\\\Host\\Share\\".into();
        let mut two = one.clone();
        two.volname = "\\\\host\\share".into();
        assert_eq!(share_name(&one), share_name(&two));
        two.volname = "\\\\host\\other".into();
        assert_ne!(share_name(&one), share_name(&two));
    }
}
