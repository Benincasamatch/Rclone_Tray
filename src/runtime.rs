//! Owned rclone children. This module has no frontend/window dependencies.
use std::collections::{BTreeMap, VecDeque};
use std::fs::{self, File};
use std::io::{self, Read, Write};
use std::net::{Ipv4Addr, SocketAddr, SocketAddrV4, TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Arc, LazyLock, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use base64::Engine;
use regex::{Captures, Regex};

use crate::config::{self, AppConfig, MountConfig};
use crate::contracts::{RunState, RuntimeStatus};
use crate::environment;

#[path = "runtime_safety.rs"]
mod safety;

pub type Notifier = Arc<dyn Fn(&str, RuntimeStatus) + Send + Sync>;
const START_TIMEOUT: Duration = Duration::from_secs(30);
const STOP_TIMEOUT: Duration = Duration::from_secs(8);
const HTTP_TIMEOUT: Duration = Duration::from_millis(700);
const STEP: Duration = Duration::from_millis(100);
const MAX_HTTP: usize = 65_536;
const MAX_RECORD: usize = 8192;

// Only the launch fields are retained; mounts never clone an AppConfig.
#[derive(Clone, PartialEq, Eq)]
struct LaunchSettings {
    exe: String,
    conf: String,
    no_auth: bool,
    user: String,
    password: String,
}
impl LaunchSettings {
    fn from_config(cfg: &AppConfig) -> Self {
        Self {
            exe: cfg.rclone_path.clone(),
            conf: cfg.rclone_conf.clone(),
            no_auth: cfg.gui_no_auth,
            user: cfg.gui_user.clone(),
            password: cfg.gui_pass.clone(),
        }
    }
    fn environment_changed(&self, other: &Self) -> bool {
        self.exe != other.exe || self.conf != other.conf
    }
    fn executable(&self, cancel: &AtomicBool) -> Result<(PathBuf, String), String> {
        check_cancel(cancel)?;
        let exe = environment::find_rclone(&self.exe)?;
        let version = environment::get_version(&exe)?;
        check_cancel(cancel)?;
        Ok((exe, version))
    }
    fn configure(&self, command: &mut Command) -> Result<(), String> {
        if let Some(conf) = environment::find_rclone_conf(&self.conf)? {
            command.arg("--config").arg(conf);
        } else if std::env::var_os("RCLONE_TRAY_DATA_DIR").is_some() {
            command
                .arg("--config")
                .arg(config::app_data_dir().join("rclone.conf"));
        }
        let temp = config::temp_dir()?;
        // Inherited debug/auth/output options must not override our ownership boundary.
        for (name, _) in std::env::vars_os() {
            if unsafe_environment_name(&name.to_string_lossy()) {
                command.env_remove(name);
            }
        }
        command
            .env("RCLONE_TEMP_DIR", &temp)
            .env("TMP", &temp)
            .env("TEMP", &temp);
        command
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            command.creation_flags(0x0800_0000 | 0x0000_0200); // NO_WINDOW | NEW_PROCESS_GROUP
        }
        Ok(())
    }
}
fn unsafe_environment_name(name: &str) -> bool {
    let name = name.to_ascii_uppercase();
    name.starts_with("RCLONE_RC_")
        || matches!(
            name.as_str(),
            "RCLONE_USER"
                | "RCLONE_PASS"
                | "RCLONE_NO_AUTH"
                | "RCLONE_LOG_FILE"
                | "RCLONE_DUMP"
                | "RCLONE_DUMP_BODIES"
                | "RCLONE_DUMP_HEADERS"
                | "RCLONE_CPUPROFILE"
                | "RCLONE_MEMPROFILE"
                | "RCLONE_SYSLOG"
                | "RCLONE_LOG_SYSTEMD"
                | "RCLONE_WINDOWS_EVENT_LOG_LEVEL"
        )
}
fn check_cancel(cancel: &AtomicBool) -> Result<(), String> {
    if cancel.load(Ordering::Acquire) {
        Err("启动已取消；应用正在退出。".into())
    } else {
        Ok(())
    }
}
fn io_message(action: &str, error: &io::Error) -> String {
    format!(
        "{action}失败（系统错误 {}）；请检查路径、权限和文件占用。",
        error.raw_os_error().unwrap_or(0)
    )
}

struct Redactor {
    secrets: Vec<String>,
}
impl Redactor {
    fn new(values: &[&str]) -> Self {
        let mut secrets = Vec::new();
        for value in values.iter().copied().filter(|s| s.chars().count() >= 4) {
            secrets.push(value.to_owned());
            secrets.push(
                percent_encoding::utf8_percent_encode(value, percent_encoding::NON_ALPHANUMERIC)
                    .to_string(),
            );
            secrets.push(url::form_urlencoded::byte_serialize(value.as_bytes()).collect());
        }
        for pair in values.chunks_exact(2) {
            if !pair[0].is_empty() || !pair[1].is_empty() {
                secrets.push(
                    base64::engine::general_purpose::STANDARD
                        .encode(format!("{}:{}", pair[0], pair[1])),
                );
            }
        }
        secrets.sort_by_key(|value| std::cmp::Reverse(value.len()));
        secrets.dedup();
        Self { secrets }
    }
    fn clean(&self, text: &str) -> String {
        static URLS: LazyLock<Regex> = LazyLock::new(|| {
            Regex::new(r#"(?i)https?://[^\s<>\"']+"#).expect("constant URL regex")
        });
        static FIELDS: LazyLock<Regex> = LazyLock::new(|| {
            Regex::new(
            r#"(?i)((?:--)?(?:rc-)?(?:pass(?:word)?|user(?:name)?|login_token|token|secret|authorization)\s*(?:=|:)\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"#
        ).expect("constant credential regex")
        });
        static BASIC: LazyLock<Regex> = LazyLock::new(|| {
            Regex::new(r"(?i)\b(?:Basic|Bearer)\s+[A-Za-z0-9+/=_\-.]+")
                .expect("constant auth regex")
        });
        let without_auth = BASIC.replace_all(text, "[redacted authorization]");
        let without_urls =
            URLS.replace_all(
                &without_auth,
                |matched: &Captures<'_>| match url::Url::parse(&matched[0]) {
                    Ok(mut url) => {
                        let _ = url.set_username("");
                        let _ = url.set_password(None);
                        url.set_query(None);
                        url.set_fragment(None);
                        url.to_string()
                    }
                    Err(_) => "[redacted URL]".into(),
                },
            );
        let mut safe = FIELDS
            .replace_all(&without_urls, "${1}[redacted]")
            .into_owned();
        for secret in &self.secrets {
            safe = safe.replace(secret, "[redacted]");
        }
        safe
    }
}

struct LogState {
    file: Option<File>,
    tail: VecDeque<String>,
    write_failed: bool,
    read_failed: bool,
}
struct Logs {
    shared: Arc<Mutex<LogState>>,
    readers: Vec<JoinHandle<()>>,
}
impl Logs {
    fn create(path: &Path) -> Result<Self, String> {
        let file = File::create(path).map_err(|e| io_message("创建日志", &e))?;
        Ok(Self {
            shared: Arc::new(Mutex::new(LogState {
                file: Some(file),
                tail: VecDeque::with_capacity(8),
                write_failed: false,
                read_failed: false,
            })),
            readers: Vec::with_capacity(2),
        })
    }
    fn drain<R: Read + Send + 'static>(
        &mut self,
        mut input: R,
        redactor: Arc<Redactor>,
    ) -> Result<(), String> {
        let shared = self.shared.clone();
        let reader = thread::Builder::new()
            .name("rclone-log-drain".into())
            .spawn(move || {
                let mut block = [0u8; 4096];
                let mut record = Vec::with_capacity(MAX_RECORD);
                let mut overflow = false;
                loop {
                    match input.read(&mut block) {
                        Ok(0) => {
                            if !record.is_empty() || overflow {
                                record_log(&shared, &redactor, &record, overflow);
                            }
                            break;
                        }
                        Ok(count) => {
                            for byte in &block[..count] {
                                if *byte == b'\n' {
                                    record_log(&shared, &redactor, &record, overflow);
                                    record.clear();
                                    overflow = false;
                                } else if !overflow {
                                    if record.len() == MAX_RECORD {
                                        record.clear();
                                        overflow = true;
                                    } else {
                                        record.push(*byte);
                                    }
                                }
                            }
                        }
                        Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                        Err(_) => {
                            shared.lock().unwrap_or_else(|e| e.into_inner()).read_failed = true;
                            break;
                        }
                    }
                }
            })
            .map_err(|e| io_message("创建日志读取线程", &e))?;
        self.readers.push(reader);
        Ok(())
    }
    fn error(&self) -> Option<&'static str> {
        let shared = self.shared.lock().unwrap_or_else(|e| e.into_inner());
        if shared.write_failed {
            Some("日志写入失败；请检查应用数据目录权限和磁盘空间。")
        } else if shared.read_failed {
            Some("日志读取失败；无法可靠记录本实例诊断信息。")
        } else {
            None
        }
    }
    fn detail(&self) -> String {
        self.shared
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .tail
            .iter()
            .cloned()
            .collect::<Vec<_>>()
            .join(" | ")
    }
    fn finish(&mut self, duration: Duration) {
        let deadline = Instant::now() + duration;
        while self.readers.iter().any(|r| !r.is_finished()) && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(10));
        }
        let mut index = 0;
        while index < self.readers.len() {
            if self.readers[index].is_finished() {
                let _ = self.readers.swap_remove(index).join();
            } else {
                index += 1;
            }
        }
        if self.readers.is_empty() {
            self.shared.lock().unwrap_or_else(|e| e.into_inner()).file = None;
        }
    }
}
fn record_log(shared: &Mutex<LogState>, redactor: &Redactor, bytes: &[u8], overflow: bool) {
    // Never release partial overlong records: a credential can straddle any chunk boundary.
    let safe = if overflow {
        "[overlong log record omitted]".into()
    } else {
        redactor.clean(String::from_utf8_lossy(bytes).trim_end_matches('\r'))
    };
    let mut state = shared.lock().unwrap_or_else(|e| e.into_inner());
    if let Some(file) = &mut state.file {
        if writeln!(file, "{safe}").and_then(|_| file.flush()).is_err() {
            state.write_failed = true;
            state.file = None; // Keep draining even when storage is unavailable.
        }
    }
    let tail = safe.trim();
    if !tail.is_empty() {
        if state.tail.len() == 8 {
            state.tail.pop_front();
        }
        let start = tail
            .char_indices()
            .rev()
            .nth(1599)
            .map(|(index, _)| index)
            .unwrap_or(0);
        state.tail.push_back(tail[start..].to_owned());
    }
}

struct Endpoint {
    port: u16,
    user: String,
    password: String,
}
impl Endpoint {
    fn address(&self) -> String {
        format!("127.0.0.1:{}", self.port)
    }
    fn url(&self) -> String {
        format!("http://{}/", self.address())
    }
    fn request(&self, path: &str, post: bool) -> Result<Vec<u8>, String> {
        loopback_http(self.port, &self.user, &self.password, path, post)
    }
}

struct Job {
    child: Option<Child>,
    exited: Option<String>,
    status: RuntimeStatus,
    endpoint: Option<Endpoint>,
    logs: Option<Logs>,
    redactor: Arc<Redactor>,
}
impl Job {
    fn new() -> Self {
        Self {
            child: None,
            exited: None,
            status: RuntimeStatus::default(),
            endpoint: None,
            logs: None,
            redactor: Arc::new(Redactor::new(&[])),
        }
    }
    fn live(&self) -> bool {
        self.child.is_some() && self.exited.is_none()
    }
    fn emit(
        &mut self,
        key: &str,
        state: RunState,
        message: &str,
        notify: &Notifier,
    ) -> RuntimeStatus {
        self.transition(key, state, message, notify);
        self.status.clone()
    }
    fn transition(&mut self, key: &str, state: RunState, message: &str, notify: &Notifier) -> bool {
        let status = RuntimeStatus {
            state,
            message: self.redactor.clean(message),
            pid: self
                .child
                .as_ref()
                .filter(|_| self.exited.is_none())
                .map(Child::id),
            log_path: self.status.log_path.clone(),
        };
        if status == self.status {
            return false;
        }
        self.status = status;
        notify(key, self.status.clone());
        true
    }
    fn spawn(
        &mut self,
        command: &mut Command,
        log_name: &str,
        key: &str,
        notify: &Notifier,
    ) -> Result<(), String> {
        let path = config::logs_dir()?.join(format!("{log_name}.log"));
        let logs = Logs::create(&path)?;
        self.logs = Some(logs);
        self.status.log_path = Some(path);
        let child = command
            .spawn()
            .map_err(|e| io_message("创建 rclone 进程", &e))?;
        self.child = Some(child);
        self.exited = None;
        self.emit(
            key,
            RunState::Starting,
            "已创建进程，正在验证本实例就绪",
            notify,
        );
        let child = self.child.as_mut().expect("assigned child");
        let logs = self.logs.as_mut().expect("assigned logs");
        logs.drain(
            child.stdout.take().ok_or("子进程标准输出管道不可用。")?,
            self.redactor.clone(),
        )?;
        logs.drain(
            child.stderr.take().ok_or("子进程标准错误管道不可用。")?,
            self.redactor.clone(),
        )?;
        Ok(())
    }
    fn observe_exit(&mut self) -> Result<bool, String> {
        if self.exited.is_some() {
            return Ok(true);
        }
        match &mut self.child {
            Some(child) => match child.try_wait() {
                Ok(Some(exit)) => {
                    self.exited = Some(exit.to_string());
                    Ok(true)
                }
                Ok(None) => Ok(false),
                Err(e) => Err(io_message("查询本实例进程状态", &e)),
            },
            None => Ok(true),
        }
    }
    fn pid_matches(&mut self) -> bool {
        if self.observe_exit() != Ok(false) {
            return false;
        }
        let Some(endpoint) = &self.endpoint else {
            return false;
        };
        let Ok(body) = endpoint.request("core/pid", true) else {
            return false;
        };
        let Ok(value) = serde_json::from_slice::<serde_json::Value>(&body) else {
            return false;
        };
        let matched = value
            .as_object()
            .and_then(|v| v.get("pid"))
            .and_then(|v| v.as_u64())
            == self.child.as_ref().map(|child| u64::from(child.id()));
        matched && self.observe_exit() == Ok(false)
    }
    fn failure(&self, message: &str) -> String {
        match &self.logs {
            Some(logs) => {
                let detail = logs.detail();
                if detail.is_empty() {
                    message.into()
                } else {
                    format!("{message} {detail}")
                }
            }
            None => message.into(),
        }
    }
    fn stop_child(&mut self, cancel: &AtomicBool) -> Result<(), String> {
        if self.observe_exit()? {
            if let Some(logs) = &mut self.logs {
                logs.finish(Duration::from_secs(1));
            }
            return Ok(());
        }
        if self.pid_matches() {
            if let Some(endpoint) = &self.endpoint {
                let _ = endpoint.request("core/quit", true);
            }
        }
        let deadline = Instant::now() + STOP_TIMEOUT;
        // Cancellation shortens the graceful period, never relinquishes control.
        while !self.observe_exit()? && Instant::now() < deadline && !cancel.load(Ordering::Acquire)
        {
            thread::sleep(STEP);
        }
        if !self.observe_exit()? {
            if let Some(child) = &mut self.child {
                if let Err(error) = child.kill() {
                    if !self.observe_exit()? {
                        return Err(io_message("停止本实例进程", &error));
                    }
                }
            }
            let deadline = Instant::now() + Duration::from_secs(3);
            while !self.observe_exit()? && Instant::now() < deadline {
                thread::sleep(STEP);
            }
            if !self.observe_exit()? {
                return Err("无法确认 rclone 退出；仍保留进程控制权，请重试停止。".into());
            }
        }
        if let Some(logs) = &mut self.logs {
            logs.finish(Duration::from_secs(1));
        }
        Ok(())
    }
    fn forget_stopped_child(&mut self) {
        self.child = None;
        self.exited = None;
        self.endpoint = None;
    }
}

// At most one probe per mount can be outstanding; a hung provider cannot cause
// an unbounded collection of probe threads on every readiness/poll iteration.
struct Task<T> {
    receive: mpsc::Receiver<T>,
}
impl<T: Send + 'static> Task<T> {
    fn start(name: &str, work: impl FnOnce() -> T + Send + 'static) -> Result<Self, String> {
        let (send, receive) = mpsc::sync_channel(1);
        thread::Builder::new()
            .name(name.into())
            .spawn(move || {
                let _ = send.send(work());
            })
            .map_err(|e| io_message("创建挂载探测线程", &e))?;
        Ok(Self { receive })
    }
    fn take(&self) -> Result<Option<T>, String> {
        match self.receive.try_recv() {
            Ok(value) => Ok(Some(value)),
            Err(mpsc::TryRecvError::Empty) => Ok(None),
            Err(mpsc::TryRecvError::Disconnected) => {
                Err("挂载探测线程意外退出；无法确认路径安全。".into())
            }
        }
    }
}

struct MountJob {
    mount: MountConfig,
    job: Job,
    lease: Option<safety::Lease>,
    preparation: Option<Task<Result<safety::Lease, String>>>,
    probe: Option<Task<Result<bool, String>>>,
    cleanup: Option<Task<Result<safety::Lease, (safety::Lease, String)>>>,
}
impl MountJob {
    fn new(mount: MountConfig) -> Self {
        Self {
            mount,
            job: Job::new(),
            lease: None,
            preparation: None,
            probe: None,
            cleanup: None,
        }
    }
    fn owned(&self) -> bool {
        self.job.live() || self.lease.is_some() || self.cleanup.is_some()
    }
    fn connection_matches(&self, mount: &MountConfig) -> bool {
        let previous = &self.mount;
        previous.remote == mount.remote
            && previous.mount_point == mount.mount_point
            && previous.vfs_cache_mode == mount.vfs_cache_mode
            && previous.network_mode == mount.network_mode
            && previous.read_only == mount.read_only
            && previous.volname == mount.volname
    }
    fn prepare(&mut self, cancel: &AtomicBool) -> Result<safety::Lease, String> {
        if let Some(task) = &self.preparation {
            match task.take()? {
                Some(_) => {
                    self.preparation = None;
                }
                None => {
                    return Err(
                        "上次路径安全探测仍被系统阻塞；不会重复创建探测，请稍后重试。".into(),
                    )
                }
            }
        }
        let mount = self.mount.clone();
        self.preparation = Some(Task::start("rclone-mount-preflight", move || {
            safety::prepare(&mount)
        })?);
        let deadline = Instant::now() + Duration::from_secs(3);
        loop {
            check_cancel(cancel)?;
            if let Some(result) = self
                .preparation
                .as_ref()
                .expect("started preparation")
                .take()?
            {
                self.preparation = None;
                return result;
            }
            if Instant::now() >= deadline {
                return Err("检查挂载点超时；路径或网络提供程序没有响应，未删除任何对象。".into());
            }
            thread::sleep(STEP);
        }
    }
    fn ready_root(&mut self) -> Result<bool, String> {
        if let Some(task) = &self.probe {
            if let Some(result) = task.take()? {
                self.probe = None;
                return result;
            }
            return Ok(false);
        }
        let target = self.mount.mount_point.clone();
        self.probe = Some(Task::start("rclone-mount-root", move || {
            safety::ready_root(&target)
        })?);
        Ok(false)
    }
    fn cleanup_mount(&mut self) -> Result<(), String> {
        // A blocked root opener must finish before restoration (it might hold
        // the very mount handle whose closure allows WinFsp to unmount).
        if let Some(probe) = &self.probe {
            if probe.take()?.is_none() {
                return Err("挂载根目录探测仍未返回；保留控制权，请稍后重试停止。".into());
            }
            self.probe = None;
        }
        if self.cleanup.is_none() {
            if let Some(lease) = self.lease.take() {
                let (send, receive) = mpsc::sync_channel(1);
                // The lease must never be lost if thread creation fails.
                let holder = Arc::new(Mutex::new(Some(lease)));
                let worker = holder.clone();
                match thread::Builder::new()
                    .name("rclone-mount-cleanup".into())
                    .spawn(move || {
                        let mut lease = worker
                            .lock()
                            .unwrap_or_else(|e| e.into_inner())
                            .take()
                            .expect("cleanup lease");
                        let result = lease.cleanup();
                        let _ = send.send(match result {
                            Ok(()) => Ok(lease),
                            Err(error) => Err((lease, error)),
                        });
                    }) {
                    Ok(_) => {
                        self.cleanup = Some(Task { receive });
                    }
                    Err(error) => {
                        self.lease = holder.lock().unwrap_or_else(|e| e.into_inner()).take();
                        return Err(io_message("创建卸载确认线程", &error));
                    }
                }
            } else {
                return Ok(());
            }
        }
        let deadline = Instant::now() + STOP_TIMEOUT;
        loop {
            if let Some(result) = self.cleanup.as_ref().expect("cleanup started").take()? {
                self.cleanup = None;
                match result {
                    Ok(_) => return Ok(()),
                    Err((lease, error)) => {
                        self.lease = Some(lease);
                        return Err(error);
                    }
                }
            }
            if Instant::now() >= deadline {
                return Err(
                    "进程已退出，但卸载/空目录恢复尚未确认；仍保留控制权，请重试停止。".into(),
                );
            }
            thread::sleep(STEP);
        }
    }
    fn stop(&mut self, cancel: &AtomicBool, notify: &Notifier) -> RuntimeStatus {
        let id = &self.mount.id;
        if !self.owned() && self.job.child.is_none() {
            return self.job.emit(id, RunState::Stopped, "已停止", notify);
        }
        self.job
            .emit(id, RunState::Stopping, "正在停止并确认实际卸载", notify);
        let result = self
            .job
            .stop_child(cancel)
            .and_then(|_| self.cleanup_mount());
        match result {
            Ok(()) => {
                self.job.forget_stopped_child();
                self.job.emit(
                    &self.mount.id,
                    RunState::Stopped,
                    "已停止、确认卸载并恢复原空目录",
                    notify,
                )
            }
            Err(error) => self.job.emit(
                &self.mount.id,
                RunState::Failed,
                &format!("{error} 仍保留控制权，请重试停止。"),
                notify,
            ),
        }
    }
    fn abort(&mut self, message: &str, cancel: &AtomicBool, notify: &Notifier) -> RuntimeStatus {
        let failure = self.job.failure(message);
        let result = self.stop(cancel, notify);
        let message = if result.state == RunState::Stopped {
            failure
        } else {
            format!("{failure} 清理未完成：{}", result.message)
        };
        self.job
            .emit(&self.mount.id, RunState::Failed, &message, notify)
    }
    fn start(
        &mut self,
        settings: &LaunchSettings,
        cancel: &AtomicBool,
        notify: &Notifier,
    ) -> RuntimeStatus {
        if self.job.live() {
            match self.job.observe_exit() {
                Ok(false) => return self.job.status.clone(),
                Ok(true) => {}
                Err(error) => {
                    return self
                        .job
                        .emit(&self.mount.id, RunState::Failed, &error, notify)
                }
            }
        }
        if self.owned() {
            return self.job.emit(
                &self.mount.id,
                RunState::Failed,
                "上次挂载或空目录恢复尚未完成；请先重试停止。",
                notify,
            );
        }
        self.job.redactor = Arc::new(Redactor::new(&[&settings.user, &settings.password]));
        self.job.emit(
            &self.mount.id,
            RunState::Starting,
            "正在启动并验证实际挂载",
            notify,
        );
        let prepared = (|| -> Result<Command, String> {
            self.mount.validate()?;
            let (exe, _) = settings.executable(cancel)?;
            if !environment::winfsp_installed() {
                return Err("未找到 WinFsp 运行库；请安装匹配架构的 WinFsp 后重新检测。".into());
            }
            let lease = self.prepare(cancel)?;
            let port = available_port()?;
            let mut random = [0u8; 32];
            getrandom::getrandom(&mut random)
                .map_err(|_| "无法生成安全随机 RC 凭据；未创建挂载进程。")?;
            let password = base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(random);
            let endpoint = Endpoint {
                port,
                user: "rclone_tray".into(),
                password,
            };
            self.job.redactor = Arc::new(Redactor::new(&[
                &settings.user,
                &settings.password,
                &endpoint.user,
                &endpoint.password,
            ]));
            let cache = config::cache_dir()?.join("mounts").join(&self.mount.id);
            fs::create_dir_all(&cache).map_err(|e| io_message("创建独立挂载缓存目录", &e))?;
            let mut command = Command::new(exe);
            command
                .arg("mount")
                .arg("--vfs-cache-mode")
                .arg(&self.mount.vfs_cache_mode)
                .arg("--cache-dir")
                .arg(cache)
                .arg("--rc")
                .arg("--rc-addr")
                .arg(endpoint.address());
            if self.mount.network_mode {
                command.arg("--network-mode");
            }
            if self.mount.read_only {
                command.arg("--read-only");
            }
            if !self.mount.volname.is_empty() {
                command.arg("--volname").arg(&self.mount.volname);
            } else if self.mount.network_mode {
                command
                    .arg("--volname")
                    .arg(format!("\\\\rclone-tray\\{}", self.mount.id));
            }
            settings.configure(&mut command)?;
            command
                .arg("--")
                .arg(&self.mount.remote)
                .arg(&self.mount.mount_point);
            command
                .env("RCLONE_RC_USER", &endpoint.user)
                .env("RCLONE_RC_PASS", &endpoint.password)
                .env("RCLONE_RC_NO_AUTH", "false");
            self.job.endpoint = Some(endpoint);
            self.lease = Some(lease);
            check_cancel(cancel)?;
            self.lease
                .as_mut()
                .expect("prepared lease")
                .remove_placeholder()?;
            check_cancel(cancel)?;
            Ok(command)
        })();
        let mut command = match prepared {
            Ok(command) => command,
            Err(error) => return self.abort(&error, cancel, notify),
        };
        let spawned = self.job.spawn(
            &mut command,
            &format!("mount-{}", self.mount.id),
            &self.mount.id,
            notify,
        );
        // A reader-thread failure occurs AFTER spawn; it still creates an
        // actual mount/unmount obligation even when spawn returns an error.
        if self.job.child.is_some() && self.job.exited.is_none() {
            self.lease.as_mut().expect("prepared lease").mark_spawned();
        }
        if let Err(error) = spawned {
            return self.abort(&error, cancel, notify);
        }
        let deadline = Instant::now() + START_TIMEOUT;
        loop {
            if let Err(error) = check_cancel(cancel) {
                return self.abort(&error, cancel, notify);
            }
            match self.job.observe_exit() {
                Ok(true) => {
                    return self.abort("rclone 启动失败并退出；请检查配置和日志。", cancel, notify)
                }
                Err(error) => return self.abort(&error, cancel, notify),
                Ok(false) => {}
            }
            if let Some(error) = self.job.logs.as_ref().and_then(Logs::error) {
                return self.abort(error, cancel, notify);
            }
            if self.job.pid_matches() {
                match self.ready_root() {
                    Ok(true)
                        if !cancel.load(Ordering::Acquire)
                            && self.job.observe_exit() == Ok(false) =>
                    {
                        return self.job.emit(
                            &self.mount.id,
                            RunState::Ready,
                            "已验证本实例 PID，挂载根目录可访问",
                            notify,
                        )
                    }
                    Err(error) => return self.abort(&error, cancel, notify),
                    _ => {}
                }
            }
            if Instant::now() >= deadline {
                return self.abort(
                    "等待实际挂载就绪超时；请检查 remote、网络、WinFsp 和日志。",
                    cancel,
                    notify,
                );
            }
            thread::sleep(STEP);
        }
    }
}

pub struct ProcessManager {
    settings: LaunchSettings,
    gui: Job,
    gui_open_url: Option<String>,
    mounts: BTreeMap<String, MountJob>,
    cancel: Arc<AtomicBool>,
    notify: Notifier,
}
impl ProcessManager {
    pub fn new(cfg: &AppConfig, cancel: Arc<AtomicBool>, notify: Notifier) -> Self {
        Self {
            settings: LaunchSettings::from_config(cfg),
            gui: Job::new(),
            gui_open_url: None,
            mounts: cfg
                .mounts
                .iter()
                .map(|mount| (mount.id.clone(), MountJob::new(mount.clone())))
                .collect(),
            cancel,
            notify,
        }
    }
    pub fn update_config(&mut self, cfg: &AppConfig) -> Result<(), String> {
        cfg.validate()?;
        let settings = LaunchSettings::from_config(cfg);
        if self.gui.live() && self.settings != settings {
            return Err("GUI 正在运行；请先停止 GUI，再修改程序路径、rclone 配置或认证。".into());
        }
        let proposed: BTreeMap<_, _> = cfg.mounts.iter().map(|m| (m.id.as_str(), m)).collect();
        for (id, job) in &self.mounts {
            if job.owned()
                && (self.settings.environment_changed(&settings)
                    || proposed
                        .get(id.as_str())
                        .is_none_or(|mount| !job.connection_matches(mount)))
            {
                return Err("挂载仍在运行或尚未卸载；请先停止，再修改连接参数或删除。".into());
            }
        }
        // All rejection checks precede mutation. Each unchanged ID retains its Child and lease.
        self.settings = settings;
        self.mounts
            .retain(|id, _| proposed.contains_key(id.as_str()));
        for mount in &cfg.mounts {
            self.mounts
                .entry(mount.id.clone())
                .and_modify(|job| job.mount = mount.clone())
                .or_insert_with(|| MountJob::new(mount.clone()));
        }
        Ok(())
    }
    pub fn statuses(&self) -> (RuntimeStatus, BTreeMap<String, RuntimeStatus>) {
        (
            self.gui.status.clone(),
            self.mounts
                .iter()
                .map(|(id, job)| (id.clone(), job.job.status.clone()))
                .collect(),
        )
    }
    pub fn gui_url(&self) -> Option<String> {
        if self.gui.live() && self.gui.status.state == RunState::Ready {
            self.gui_open_url.clone()
        } else {
            None
        }
    }
    pub fn owned_gui(&self) -> bool {
        self.gui.live()
    }
    pub fn owned_mount(&self, id: &str) -> bool {
        self.mounts.get(id).is_some_and(MountJob::owned)
    }
    pub fn has_live_children(&self) -> bool {
        self.gui.live() || self.mounts.values().any(|m| m.job.live())
    }
    pub fn start_gui(&mut self) -> RuntimeStatus {
        if self.gui.live() {
            match self.gui.observe_exit() {
                Ok(false) => return self.gui.status.clone(),
                Err(error) => return self.gui.emit("gui", RunState::Failed, &error, &self.notify),
                Ok(true) => {}
            }
        }
        self.gui_open_url = None;
        self.gui.redactor = Arc::new(Redactor::new(&[
            &self.settings.user,
            &self.settings.password,
        ]));
        self.gui.emit(
            "gui",
            RunState::Starting,
            "正在启动 GUI 并验证本实例服务",
            &self.notify,
        );
        let prepared = (|| -> Result<(Command, u16), String> {
            if !self.settings.no_auth
                && (self.settings.user.trim().is_empty() || self.settings.password.is_empty())
            {
                return Err("启用认证必须填写用户名和密码；不会自动使用默认凭据。".into());
            }
            if self.settings.user.contains([':', '\r', '\n']) {
                return Err("认证用户名不能包含冒号或换行。".into());
            }
            let (exe, version) = self.settings.executable(&self.cancel)?;
            let modern = modern_gui(&version)?;
            ensure_free_port(5572)?;
            if modern {
                ensure_free_port(5570)?;
            }
            let endpoint = Endpoint {
                port: 5572,
                user: if self.settings.no_auth {
                    String::new()
                } else {
                    self.settings.user.clone()
                },
                password: if self.settings.no_auth {
                    String::new()
                } else {
                    self.settings.password.clone()
                },
            };
            let gui_port = if modern { 5570 } else { 5572 };
            let mut command = Command::new(exe);
            if modern {
                command
                    .arg("gui")
                    .arg("--no-open-browser")
                    .arg("--addr")
                    .arg("127.0.0.1:5570")
                    .arg("--api-addr")
                    .arg(endpoint.address());
                if self.settings.no_auth {
                    command.arg("--no-auth");
                }
            } else {
                command
                    .arg("rcd")
                    .arg("--rc-web-gui")
                    .arg("--rc-web-gui-no-open-browser")
                    .arg("--rc-addr")
                    .arg(endpoint.address());
                if self.settings.no_auth {
                    command.arg("--rc-no-auth");
                }
            }
            settings_gui_cache(&mut command)?;
            self.settings.configure(&mut command)?;
            if !self.settings.no_auth {
                command
                    .env("RCLONE_RC_USER", &endpoint.user)
                    .env("RCLONE_RC_PASS", &endpoint.password);
            }
            let query = url::form_urlencoded::Serializer::new(String::new())
                .append_pair("url", &endpoint.url())
                .finish();
            self.gui_open_url = Some(format!(
                "http://127.0.0.1:{gui_port}/{}login?{query}",
                if modern { "" } else { "#/" }
            ));
            self.gui.endpoint = Some(endpoint);
            check_cancel(&self.cancel)?;
            Ok((command, gui_port))
        })();
        let (mut command, port) = match prepared {
            Ok(value) => value,
            Err(error) => return self.abort_gui(&error),
        };
        if let Err(error) = self.gui.spawn(&mut command, "gui", "gui", &self.notify) {
            return self.abort_gui(&error);
        }
        let deadline = Instant::now() + START_TIMEOUT;
        loop {
            if let Err(error) = check_cancel(&self.cancel) {
                return self.abort_gui(&error);
            }
            match self.gui.observe_exit() {
                Ok(true) => return self.abort_gui("rclone GUI 启动失败并退出；请检查配置和日志。"),
                Err(error) => return self.abort_gui(&error),
                Ok(false) => {}
            }
            if let Some(error) = self.gui.logs.as_ref().and_then(Logs::error) {
                return self.abort_gui(error);
            }
            if self.gui.pid_matches() {
                let endpoint = self.gui.endpoint.as_ref().expect("GUI endpoint");
                if loopback_http(port, &endpoint.user, &endpoint.password, "", false).is_ok()
                    && !self.cancel.load(Ordering::Acquire)
                    && self.gui.observe_exit() == Ok(false)
                {
                    return self.gui.emit(
                        "gui",
                        RunState::Ready,
                        "已验证本实例 PID 和 GUI HTTP 服务",
                        &self.notify,
                    );
                }
            }
            if Instant::now() >= deadline {
                return self.abort_gui("等待 GUI 就绪超时；请检查网络和日志。");
            }
            thread::sleep(STEP);
        }
    }
    fn abort_gui(&mut self, message: &str) -> RuntimeStatus {
        let mut message = self.gui.failure(message);
        let result = self.stop_gui();
        if result.state != RunState::Stopped {
            message.push_str(&format!(" 清理未完成：{}", result.message));
        }
        self.gui
            .emit("gui", RunState::Failed, &message, &self.notify)
    }
    pub fn stop_gui(&mut self) -> RuntimeStatus {
        self.gui_open_url = None;
        if self.gui.child.is_none() {
            return self
                .gui
                .emit("gui", RunState::Stopped, "已停止", &self.notify);
        }
        self.gui.emit(
            "gui",
            RunState::Stopping,
            "正在停止并确认退出",
            &self.notify,
        );
        match self.gui.stop_child(&self.cancel) {
            Ok(()) => {
                self.gui.forget_stopped_child();
                self.gui
                    .emit("gui", RunState::Stopped, "已停止并确认退出", &self.notify)
            }
            Err(error) => self.gui.emit(
                "gui",
                RunState::Failed,
                &format!("{error} 仍保留控制权，请重试停止。"),
                &self.notify,
            ),
        }
    }
    pub fn start_mount(&mut self, id: &str) -> RuntimeStatus {
        let Some(job) = self.mounts.get(id) else {
            return missing_mount();
        };
        let share = safety::share_name(&job.mount);
        if let Some(share) = share {
            if self.mounts.iter().any(|(other, job)| {
                other != id
                    && job.owned()
                    && safety::share_name(&job.mount).as_ref() == Some(&share)
            }) {
                return self.mounts.get_mut(id).expect("known mount").job.emit(
                    id,
                    RunState::Failed,
                    "网络共享名与本应用仍控制的其他挂载冲突；请先停止冲突挂载。",
                    &self.notify,
                );
            }
        }
        self.mounts.get_mut(id).expect("known mount").start(
            &self.settings,
            &self.cancel,
            &self.notify,
        )
    }
    pub fn stop_mount(&mut self, id: &str) -> RuntimeStatus {
        match self.mounts.get_mut(id) {
            Some(job) => job.stop(&self.cancel, &self.notify),
            None => missing_mount(),
        }
    }
    pub fn start_all_mounts(&mut self, auto_only: bool) -> BTreeMap<String, RuntimeStatus> {
        let ids: Vec<_> = self
            .mounts
            .iter()
            .filter(|(_, m)| !auto_only || m.mount.auto_mount)
            .map(|(id, _)| id.clone())
            .collect();
        ids.into_iter()
            .map(|id| {
                let status = self.start_mount(&id);
                (id, status)
            })
            .collect()
    }
    pub fn stop_all_mounts(&mut self) -> BTreeMap<String, RuntimeStatus> {
        let ids: Vec<_> = self.mounts.keys().cloned().collect();
        ids.into_iter()
            .map(|id| {
                let status = self.stop_mount(&id);
                (id, status)
            })
            .collect()
    }
    pub fn stop_all(&mut self) -> BTreeMap<String, RuntimeStatus> {
        let mut statuses = self.stop_all_mounts();
        statuses.insert("gui".into(), self.stop_gui());
        statuses
    }
    pub fn poll_exits(&mut self) -> bool {
        let mut changed = poll_job("gui", &mut self.gui, &self.notify);
        if !self.gui.live() {
            self.gui_open_url = None;
        }
        for (id, mount) in &mut self.mounts {
            changed |= poll_job(id, &mut mount.job, &self.notify);
        }
        changed
    }
}
fn poll_job(key: &str, job: &mut Job, notify: &Notifier) -> bool {
    if !job.live() {
        return false;
    }
    match job.observe_exit() {
        Ok(true) => {
            if let Some(logs) = &mut job.logs {
                logs.finish(Duration::from_millis(250));
            }
            let message = job.failure(&format!(
                "rclone 已意外退出（{}）；请查看日志，挂载请重试停止以确认卸载。",
                job.exited.as_deref().unwrap_or("未知退出代码")
            ));
            job.transition(key, RunState::Failed, &message, notify)
        }
        Err(error) => job.transition(key, RunState::Failed, &error, notify),
        Ok(false) => {
            if job.status.state != RunState::Failed {
                if let Some(error) = job.logs.as_ref().and_then(Logs::error) {
                    return job.transition(key, RunState::Failed, error, notify);
                }
            }
            false
        }
    }
}
fn missing_mount() -> RuntimeStatus {
    RuntimeStatus {
        state: RunState::Failed,
        message: "挂载配置已不存在；请刷新列表。".into(),
        ..RuntimeStatus::default()
    }
}
fn settings_gui_cache(command: &mut Command) -> Result<(), String> {
    let cache = config::cache_dir()?.join("gui");
    fs::create_dir_all(&cache).map_err(|e| io_message("创建 GUI 缓存目录", &e))?;
    command.arg("--cache-dir").arg(cache);
    Ok(())
}
fn modern_gui(version: &str) -> Result<bool, String> {
    let version = version.trim().trim_start_matches('v');
    let mut numbers = version.split('.');
    let major = numbers
        .next()
        .and_then(|n| n.parse::<u32>().ok())
        .ok_or("rclone 版本格式无效。")?;
    let minor = numbers
        .next()
        .and_then(|n| n.parse::<u32>().ok())
        .ok_or("rclone 版本格式无效。")?;
    Ok((major, minor) >= (1, 74))
}
fn available_port() -> Result<u16, String> {
    TcpListener::bind((Ipv4Addr::LOCALHOST, 0))
        .and_then(|s| s.local_addr())
        .map(|addr| addr.port())
        .map_err(|e| io_message("分配本地 RC 端口", &e))
}
fn ensure_free_port(port: u16) -> Result<(), String> {
    TcpListener::bind((Ipv4Addr::LOCALHOST, port))
        .map(|_| ())
        .map_err(|_| format!("本地端口 {port} 被占用；请停止其他服务后重试，不会接管外部实例。"))
}

// Numeric loopback only; no DNS, proxy discovery, cookies, redirects, or foreign endpoints.
fn loopback_http(
    port: u16,
    user: &str,
    password: &str,
    path: &str,
    post: bool,
) -> Result<Vec<u8>, String> {
    if !matches!(path, "" | "core/pid" | "core/quit") {
        return Err("不允许的 RC 请求路径。".into());
    }
    let deadline = Instant::now() + HTTP_TIMEOUT;
    let address = SocketAddr::V4(SocketAddrV4::new(Ipv4Addr::LOCALHOST, port));
    let mut stream = TcpStream::connect_timeout(&address, HTTP_TIMEOUT)
        .map_err(|_| "本地 HTTP 服务尚未响应。")?;
    stream
        .set_write_timeout(Some(HTTP_TIMEOUT))
        .map_err(|_| "设置本地请求超时失败。")?;
    let mut request = format!(
        "{} /{path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n",
        if post { "POST" } else { "GET" }
    );
    if !user.is_empty() || !password.is_empty() {
        if user.len().saturating_add(password.len()) > 4096 {
            return Err("本地 HTTP 认证头超过安全上限；未发送请求。".into());
        }
        let basic = base64::engine::general_purpose::STANDARD.encode(format!("{user}:{password}"));
        request.push_str(&format!("Authorization: Basic {basic}\r\n"));
    }
    if post {
        request.push_str("Content-Type: application/json\r\nContent-Length: 2\r\n\r\n{}");
    } else {
        request.push_str("\r\n");
    }
    let mut written = 0;
    while written < request.len() {
        let remaining = deadline
            .checked_duration_since(Instant::now())
            .ok_or("本地 HTTP 请求超时。")?;
        stream
            .set_write_timeout(Some(remaining.max(Duration::from_millis(1))))
            .map_err(|_| "设置本地请求超时失败。")?;
        let count = stream
            .write(&request.as_bytes()[written..])
            .map_err(|_| "本地 HTTP 请求写入失败或超时。")?;
        if count == 0 {
            return Err("本地 HTTP 请求未完成。".into());
        }
        written += count;
    }
    let mut data = Vec::with_capacity(4096);
    let mut buffer = [0u8; 4096];
    let mut headers = None;
    loop {
        let remaining = deadline
            .checked_duration_since(Instant::now())
            .ok_or("本地 HTTP 请求超时。")?;
        stream
            .set_read_timeout(Some(remaining.max(Duration::from_millis(1))))
            .map_err(|_| "设置本地请求超时失败。")?;
        let count = stream
            .read(&mut buffer)
            .map_err(|_| "本地 HTTP 响应读取失败或超时。")?;
        if count == 0 {
            break;
        }
        if data.len() + count > MAX_HTTP + 8192 {
            return Err("本地 HTTP 响应超过安全上限。".into());
        }
        data.extend_from_slice(&buffer[..count]);
        if headers.is_none() {
            if let Some(end) = data.windows(4).position(|w| w == b"\r\n\r\n") {
                if end > 8192 {
                    return Err("本地 HTTP 响应头超过安全上限。".into());
                }
                headers = Some(parse_headers(&data[..end])?);
                if !post {
                    return Ok(Vec::new());
                } // GUI requires a successful HTTP status, not unbounded HTML.
                let (_, length, chunked) = headers.as_ref().expect("parsed headers");
                if !*chunked {
                    if let Some(length) = length {
                        if data.len() >= end + 4 + length {
                            return Ok(data[end + 4..end + 4 + length].to_vec());
                        }
                    }
                }
            } else if data.len() > 8192 {
                return Err("本地 HTTP 响应头无效。".into());
            }
        } else if let Some((end, length, false)) = &headers {
            if let Some(length) = length {
                if data.len() >= end + 4 + length {
                    return Ok(data[end + 4..end + 4 + length].to_vec());
                }
            }
        }
    }
    let (end, length, chunked) = headers.ok_or("本地 HTTP 响应头不完整。")?;
    let body = &data[end + 4..];
    if chunked {
        decode_chunks(body)
    } else {
        if length.is_some_and(|length| length != body.len()) || body.len() > MAX_HTTP {
            return Err("本地 HTTP 响应体长度无效。".into());
        }
        Ok(body.to_vec())
    }
}
fn parse_headers(bytes: &[u8]) -> Result<(usize, Option<usize>, bool), String> {
    let text = std::str::from_utf8(bytes).map_err(|_| "本地 HTTP 响应头编码无效。")?;
    let mut lines = text.split("\r\n");
    let mut status = lines
        .next()
        .ok_or("本地 HTTP 状态缺失。")?
        .split_whitespace();
    if !matches!(status.next(), Some("HTTP/1.0" | "HTTP/1.1")) {
        return Err("本地 HTTP 协议无效。".into());
    }
    let code = status
        .next()
        .and_then(|s| s.parse::<u16>().ok())
        .ok_or("本地 HTTP 状态无效。")?;
    if !(200..300).contains(&code) {
        return Err(format!("本地 HTTP 返回状态 {code}；未确认就绪。"));
    }
    let mut length = None;
    let mut chunked = false;
    for line in lines {
        let (name, value) = line.split_once(':').ok_or("本地 HTTP 响应头格式无效。")?;
        if name.eq_ignore_ascii_case("content-length") {
            if length.is_some() {
                return Err("本地 HTTP 长度头重复。".into());
            }
            let count = value
                .trim()
                .parse::<usize>()
                .map_err(|_| "本地 HTTP 长度无效。")?;
            if count > MAX_HTTP {
                return Err("本地 HTTP 响应超过安全上限。".into());
            }
            length = Some(count);
        } else if name.eq_ignore_ascii_case("transfer-encoding") {
            if chunked || !value.trim().eq_ignore_ascii_case("chunked") {
                return Err("不支持的本地 HTTP 传输编码。".into());
            }
            chunked = true;
        }
    }
    if chunked && length.is_some() {
        return Err("本地 HTTP 长度/分块头冲突。".into());
    }
    Ok((bytes.len(), length, chunked))
}
fn decode_chunks(mut bytes: &[u8]) -> Result<Vec<u8>, String> {
    let mut body = Vec::new();
    loop {
        let end = bytes
            .windows(2)
            .position(|w| w == b"\r\n")
            .ok_or("HTTP 分块长度不完整。")?;
        let size = std::str::from_utf8(&bytes[..end]).map_err(|_| "HTTP 分块长度编码无效。")?;
        let size = usize::from_str_radix(size.split(';').next().unwrap_or("").trim(), 16)
            .map_err(|_| "HTTP 分块长度无效。")?;
        bytes = &bytes[end + 2..];
        if size == 0 {
            if bytes.starts_with(b"\r\n") || bytes.windows(4).any(|w| w == b"\r\n\r\n") {
                return Ok(body);
            }
            return Err("HTTP 分块结束标记不完整。".into());
        }
        if size > MAX_HTTP - body.len()
            || bytes.len() < 2
            || size > bytes.len() - 2
            || &bytes[size..size + 2] != b"\r\n"
        {
            return Err("HTTP 分块响应长度无效。".into());
        }
        body.extend_from_slice(&bytes[..size]);
        bytes = &bytes[size + 2..];
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    struct OwnedTest(ProcessManager);
    impl Drop for OwnedTest {
        fn drop(&mut self) {
            for mount in self.0.mounts.values_mut() {
                if let Some(child) = &mut mount.job.child {
                    let _ = child.kill();
                    let _ = child.wait();
                }
            }
        }
    }
    struct DisposableChild(Child);
    impl Drop for DisposableChild {
        fn drop(&mut self) {
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }
    fn disposable_child() -> Child {
        let mut command = Command::new(std::env::current_exe().unwrap());
        command
            .args([
                "--ignored",
                "--exact",
                "runtime::tests::disposable_owned_child",
            ])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            command.creation_flags(0x0800_0000);
        }
        command.spawn().unwrap()
    }
    #[test]
    #[ignore = "Only invoked by a disposable subprocess in ownership tests"]
    fn disposable_owned_child() {
        thread::sleep(Duration::from_secs(60));
    }
    fn owned_test() -> (OwnedTest, AppConfig, String) {
        let mount = MountConfig::new("owned".into(), "sample:".into(), "Y:".into()).unwrap();
        let id = mount.id.clone();
        let mut cfg = AppConfig::default();
        cfg.mounts.push(mount);
        let mut manager = OwnedTest(ProcessManager::new(
            &cfg,
            Arc::new(AtomicBool::new(false)),
            Arc::new(|_, _| {}),
        ));
        let notify = manager.0.notify.clone();
        let job = &mut manager.0.mounts.get_mut(&id).unwrap().job;
        job.child = Some(disposable_child());
        job.emit(&id, RunState::Ready, "owned test process", &notify);
        (manager, cfg, id)
    }
    #[test]
    fn metadata_updates_retain_real_children_and_rejections_are_atomic() {
        let (mut owned, cfg, id) = owned_test();
        let pid = owned.0.mounts[&id].job.child.as_ref().unwrap().id();
        let mut candidate = cfg.clone();
        candidate.theme = "light".into();
        candidate.mounts[0].name = "renamed".into();
        owned.0.update_config(&candidate).unwrap();
        candidate.mounts[0].name = "mutated draft".into();
        assert_eq!(owned.0.mounts[&id].mount.name, "renamed");
        assert_eq!(owned.0.statuses().1[&id].pid, Some(pid));
        candidate.rclone_path = "unaccepted-program".into();
        candidate.mounts[0].remote = "other:".into();
        assert!(owned.0.update_config(&candidate).is_err());
        assert_eq!(owned.0.settings.exe, cfg.rclone_path);
        assert_eq!(owned.0.mounts[&id].mount.remote, "sample:");
        candidate = cfg.clone();
        candidate.mounts.clear();
        assert!(owned.0.update_config(&candidate).is_err());
        assert!(owned.0.owned_mount(&id));
        assert_eq!(
            owned.0.mounts.get_mut(&id).unwrap().job.observe_exit(),
            Ok(false)
        );
    }
    #[test]
    fn authenticated_foreign_pid_is_not_ready_and_owned_stop_leaves_foreign_child_alive() {
        let (mut owned, _, id) = owned_test();
        let mut foreign = DisposableChild(disposable_child());
        let foreign_pid = foreign.0.id();
        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        let port = listener.local_addr().unwrap().port();
        let server = thread::spawn(move || {
            listener.set_nonblocking(true).unwrap();
            let deadline = Instant::now() + Duration::from_secs(3);
            let mut stream = loop {
                match listener.accept() {
                    Ok((stream, _)) => break stream,
                    Err(error)
                        if error.kind() == io::ErrorKind::WouldBlock
                            && Instant::now() < deadline =>
                    {
                        thread::sleep(Duration::from_millis(10))
                    }
                    Err(error) => panic!("loopback fixture accept: {error}"),
                }
            };
            stream
                .set_read_timeout(Some(Duration::from_secs(1)))
                .unwrap();
            stream
                .set_write_timeout(Some(Duration::from_secs(1)))
                .unwrap();
            let mut request = Vec::new();
            let mut block = [0u8; 2048];
            while !request.windows(4).any(|bytes| bytes == b"\r\n\r\n") {
                let count = stream.read(&mut block).unwrap();
                assert!(count != 0 && request.len() + count < 8192);
                request.extend_from_slice(&block[..count]);
            }
            let body = format!("{{\"pid\":{foreign_pid}}}");
            write!(
                stream,
                "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len()
            )
            .unwrap();
            String::from_utf8(request).unwrap()
        });
        let job = &mut owned.0.mounts.get_mut(&id).unwrap().job;
        job.endpoint = Some(Endpoint {
            port,
            user: "boundary-user".into(),
            password: "boundary-password".into(),
        });
        assert!(!job.pid_matches());
        let request = server.join().unwrap();
        assert!(request.starts_with("POST /core/pid HTTP/1.1\r\n"));
        let expected =
            base64::engine::general_purpose::STANDARD.encode("boundary-user:boundary-password");
        assert!(request.contains(&format!("Authorization: Basic {expected}\r\n")));
        job.endpoint = None;
        job.stop_child(&AtomicBool::new(true)).unwrap();
        assert!(foreign.0.try_wait().unwrap().is_none());
    }
    #[test]
    fn redacts_url_userinfo_queries_fragments_and_encoded_credentials() {
        let redactor = Redactor::new(&["long secret", "a"]);
        let safe = redactor.clean("mount a failed pass=short http://user:pw@127.0.0.1:5570/login?token=secret#/login?pass=x long%20secret long+secret Authorization: Basic YTpi");
        assert!(safe.contains("mount a failed"));
        assert!(!safe.contains("user:pw"));
        assert!(!safe.contains("token=secret"));
        assert!(!safe.contains("long%20secret"));
        assert!(!safe.contains("long+secret"));
        assert!(!safe.contains("YTpi"));
        assert!(!safe.contains("pass=short"));
    }
    #[test]
    fn log_records_and_tail_are_bounded_without_partial_secret_release() {
        let redactor = Redactor::new(&["secret-credential"]);
        let shared = Mutex::new(LogState {
            file: None,
            tail: VecDeque::new(),
            write_failed: false,
            read_failed: false,
        });
        for _ in 0..20 {
            record_log(&shared, &redactor, "界".repeat(3000).as_bytes(), false);
        }
        record_log(&shared, &redactor, b"secret-", true);
        let state = shared.lock().unwrap();
        assert_eq!(state.tail.len(), 8);
        assert!(state.tail.iter().all(|line| line.chars().count() <= 1600));
        assert_eq!(state.tail.back().unwrap(), "[overlong log record omitted]");
    }
    #[test]
    fn http_parser_rejects_ambiguous_lengths_redirects_and_chunk_overflow() {
        assert!(parse_headers(b"HTTP/1.1 302 Found\r\nLocation: http://foreign/").is_err());
        assert!(
            parse_headers(b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\nContent-Length: 1").is_err()
        );
        assert!(parse_headers(
            b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\nTransfer-Encoding: chunked"
        )
        .is_err());
        assert!(decode_chunks(b"ffffffffffffffff\r\nx\r\n0\r\n\r\n").is_err());
        assert_eq!(decode_chunks(b"3\r\nabc\r\n0\r\n\r\n").unwrap(), b"abc");
    }
    #[test]
    fn gui_version_boundary_is_exact() {
        assert!(!modern_gui("1.73.9").unwrap());
        assert!(modern_gui("1.74.0").unwrap());
        assert!(modern_gui("2.0.0").unwrap());
        assert!(modern_gui("invalid").is_err());
    }
    #[test]
    fn inherited_environment_screening_is_case_insensitive() {
        assert!(unsafe_environment_name("rclone_rc_no_auth"));
        assert!(unsafe_environment_name("RCLONE_LOG_FILE"));
        assert!(!unsafe_environment_name("RCLONE_CONFIG_PASS"));
    }
}
