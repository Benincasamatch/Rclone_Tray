use std::collections::{BTreeMap, BTreeSet};
use std::path::PathBuf;

use crate::config::{AppConfig, MountConfig};
use crate::update::UpdateInfo;

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum RunState {
    #[default]
    Stopped,
    Starting,
    Ready,
    Stopping,
    Failed,
}

impl RunState {
    pub fn label(self) -> &'static str {
        match self {
            Self::Stopped => "已停止",
            Self::Starting => "启动中",
            Self::Ready => "已就绪",
            Self::Stopping => "停止中",
            Self::Failed => "失败",
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RuntimeStatus {
    pub state: RunState,
    pub message: String,
    pub pid: Option<u32>,
    pub log_path: Option<PathBuf>,
}

impl Default for RuntimeStatus {
    fn default() -> Self {
        Self {
            state: RunState::Stopped,
            message: "已停止".into(),
            pid: None,
            log_path: None,
        }
    }
}

#[derive(Clone, Debug, Default)]
pub struct EnvironmentInfo {
    pub exe: Option<PathBuf>,
    pub version: String,
    pub conf: Option<PathBuf>,
    pub winfsp: bool,
    pub remotes: Vec<String>,
    pub error: String,
}

#[derive(Clone, Debug)]
pub struct Snapshot {
    pub config: AppConfig,
    pub revision: u64,
    pub config_error: String,
    pub environment: EnvironmentInfo,
    pub gui: RuntimeStatus,
    pub mounts: BTreeMap<String, RuntimeStatus>,
    pub update: UpdateInfo,
    pub busy: BTreeSet<String>,
    pub closing: bool,
    pub message: String,
}

#[derive(Clone)]
pub enum Command {
    RefreshEnvironment,
    ReloadConfig,
    SaveSettings { draft: AppConfig, revision: u64 },
    SaveMount { draft: MountConfig, revision: u64 },
    DeleteMount { id: String, revision: u64 },
    StartMount(String),
    StopMount(String),
    StartAllMounts,
    StopAllMounts,
    StartGui,
    StopGui,
    OpenGui,
    OpenMount(String),
    OpenPath(PathBuf),
    OpenUrl(String),
    OpenRcloneConfig,
    SetStartup(bool),
    SetAutoMount(bool),
    SetGuiAutoStart(bool),
    CheckUpdate { quiet: bool },
    Shutdown,
}

impl Command {
    pub fn key(&self) -> String {
        match self {
            Self::RefreshEnvironment => "environment".into(),
            Self::ReloadConfig
            | Self::SaveSettings { .. }
            | Self::SetStartup(_)
            | Self::SetAutoMount(_)
            | Self::SetGuiAutoStart(_) => "settings".into(),
            Self::SaveMount { draft, .. } => draft.id.clone(),
            Self::DeleteMount { id, .. } | Self::StartMount(id) | Self::StopMount(id) => id.clone(),
            Self::StartAllMounts | Self::StopAllMounts => "mounts".into(),
            Self::StartGui | Self::StopGui | Self::OpenGui => "gui".into(),
            Self::CheckUpdate { .. } => "update".into(),
            Self::OpenMount(_) | Self::OpenPath(_) | Self::OpenUrl(_) | Self::OpenRcloneConfig => {
                "path".into()
            }
            Self::Shutdown => "shutdown".into(),
        }
    }

    pub fn action(&self) -> &'static str {
        match self {
            Self::RefreshEnvironment => "refresh_environment",
            Self::ReloadConfig => "reload_config",
            Self::SaveSettings { .. }
            | Self::SetStartup(_)
            | Self::SetAutoMount(_)
            | Self::SetGuiAutoStart(_) => "save_settings",
            Self::SaveMount { .. } => "save_mount",
            Self::DeleteMount { .. } => "delete_mount",
            Self::StartMount(_) => "start_mount",
            Self::StopMount(_) => "stop_mount",
            Self::StartAllMounts => "start_all_mounts",
            Self::StopAllMounts => "stop_all_mounts",
            Self::StartGui => "start_gui",
            Self::StopGui => "stop_gui",
            Self::OpenGui => "open_gui",
            Self::OpenMount(_) => "open_mount",
            Self::OpenPath(_) | Self::OpenUrl(_) => "open_path",
            Self::OpenRcloneConfig => "open_rclone_config",
            Self::CheckUpdate { .. } => "check_update",
            Self::Shutdown => "shutdown",
        }
    }
}

#[derive(Clone, Debug)]
pub struct Event {
    pub key: String,
    pub action: String,
    pub request_id: Option<u64>,
    pub ok: bool,
    pub message: String,
    pub shutdown_complete: bool,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum Page {
    #[default]
    Mounts,
    Service,
    Settings,
    About,
}

impl Page {
    pub fn label(self) -> &'static str {
        match self {
            Self::Mounts => "挂载",
            Self::Service => "rclone GUI",
            Self::Settings => "设置",
            Self::About => "关于与诊断",
        }
    }
}
