//! Strict, migration-compatible configuration with protected credentials and CAS persistence.
use base64::{engine::general_purpose::STANDARD, Engine};
use fs2::FileExt;
use serde::de::{self, MapAccess, SeqAccess, Visitor};
use serde::{Deserialize, Deserializer, Serialize};
use serde_json::{Map, Number, Value};
use sha2::{Digest, Sha256};
use std::collections::HashSet;
use std::fmt;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Write};
use std::path::{Path, PathBuf};

pub type Fingerprint = Option<[u8; 32]>;
const APP_NAME: &str = "rclone_tray";
const FIELDS: &[&str] = &[
    "rclone_path",
    "gui_no_auth",
    "gui_user",
    "gui_pass",
    "gui_auto_start",
    "startup_enabled",
    "auto_mount_on_boot",
    "mount_delay_seconds",
    "theme",
    "mounts",
    "rclone_conf",
    "reduce_motion",
];

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MountConfig {
    pub id: String,
    pub name: String,
    pub remote: String,
    pub mount_point: String,
    #[serde(default = "writes")]
    pub vfs_cache_mode: String,
    #[serde(default)]
    pub network_mode: bool,
    #[serde(default)]
    pub read_only: bool,
    #[serde(default = "yes")]
    pub auto_mount: bool,
    #[serde(default)]
    pub volname: String,
}
fn writes() -> String {
    "writes".into()
}
fn yes() -> bool {
    true
}

impl MountConfig {
    pub fn new(name: String, remote: String, mount_point: String) -> Result<Self, String> {
        let mut bytes = [0u8; 6];
        getrandom::getrandom(&mut bytes).map_err(|_| "无法生成安全挂载 ID。".to_string())?;
        let mut id = String::with_capacity(12);
        for byte in bytes {
            use fmt::Write;
            let _ = write!(id, "{byte:02x}");
        }
        let value = Self {
            id,
            name,
            remote,
            mount_point,
            vfs_cache_mode: writes(),
            network_mode: false,
            read_only: false,
            auto_mount: true,
            volname: String::new(),
        };
        value.validate()?;
        Ok(value)
    }
    pub fn validate(&self) -> Result<(), String> {
        for value in [
            &self.id,
            &self.name,
            &self.remote,
            &self.mount_point,
            &self.vfs_cache_mode,
            &self.volname,
        ] {
            valid_string(value)?;
        }
        if self.id.is_empty()
            || self.id.len() > 128
            || !self
                .id
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
        {
            return Err("挂载 ID 无效；不得自动替换既有身份。".into());
        }
        if self.name.trim().is_empty()
            || self.remote.trim().is_empty()
            || self.mount_point.trim().is_empty()
        {
            return Err("挂载名称、remote 和挂载点不能为空。".into());
        }
        if !["off", "minimal", "writes", "full"].contains(&self.vfs_cache_mode.as_str()) {
            return Err("挂载缓存模式必须为 off/minimal/writes/full。".into());
        }
        Ok(())
    }
}

#[derive(Clone, PartialEq, Eq)]
pub struct AppConfig {
    pub rclone_path: String,
    pub gui_no_auth: bool,
    pub gui_user: String,
    pub gui_pass: String,
    pub gui_auto_start: bool,
    pub startup_enabled: bool,
    pub auto_mount_on_boot: bool,
    pub mount_delay_seconds: u32,
    pub theme: String,
    pub mounts: Vec<MountConfig>,
    pub rclone_conf: String,
    pub reduce_motion: bool,
}
impl fmt::Debug for AppConfig {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("AppConfig")
            .field("rclone_path", &self.rclone_path)
            .field("gui_no_auth", &self.gui_no_auth)
            .field("gui_user", &"[REDACTED]")
            .field("gui_pass", &"[REDACTED]")
            .field("gui_auto_start", &self.gui_auto_start)
            .field("startup_enabled", &self.startup_enabled)
            .field("auto_mount_on_boot", &self.auto_mount_on_boot)
            .field("mount_delay_seconds", &self.mount_delay_seconds)
            .field("theme", &self.theme)
            .field("mounts", &self.mounts)
            .field("rclone_conf", &self.rclone_conf)
            .field("reduce_motion", &self.reduce_motion)
            .finish()
    }
}
impl Default for AppConfig {
    fn default() -> Self {
        Self {
            rclone_path: String::new(),
            gui_no_auth: false,
            gui_user: String::new(),
            gui_pass: String::new(),
            gui_auto_start: false,
            startup_enabled: false,
            auto_mount_on_boot: true,
            mount_delay_seconds: 15,
            theme: "dark".into(),
            mounts: Vec::new(),
            rclone_conf: String::new(),
            reduce_motion: false,
        }
    }
}
impl AppConfig {
    pub fn validate(&self) -> Result<(), String> {
        for value in [
            &self.rclone_path,
            &self.gui_user,
            &self.gui_pass,
            &self.theme,
            &self.rclone_conf,
        ] {
            valid_string(value)?;
        }
        if !["system", "light", "dark"].contains(&self.theme.as_str()) {
            return Err("主题必须为 system/light/dark。".into());
        }
        if self.mount_delay_seconds > 86400 {
            return Err("自动挂载延迟须为 0–86400 秒的整数。".into());
        }
        let mut ids = HashSet::with_capacity(self.mounts.len());
        for mount in &self.mounts {
            mount.validate()?;
            if !ids.insert(&mount.id) {
                return Err("挂载 ID 重复；请修复原配置，不得丢弃记录。".into());
            }
        }
        Ok(())
    }
}
fn valid_string(value: &str) -> Result<(), String> {
    if value.contains('\0') {
        Err("配置字段含无效字符；请修复原文件。".into())
    } else {
        Ok(())
    }
}
pub(crate) fn io_error(action: &str, error: &io::Error) -> String {
    format!(
        "{action}失败（系统错误 {}）。请检查路径、权限和文件占用；原配置未被重置。",
        error.raw_os_error().unwrap_or(0)
    )
}

/// Path resolution only: the contracted infallible API does not hide directory-creation failures.
pub fn app_data_dir() -> PathBuf {
    resolve_data_dir().0
}
pub fn try_app_data_dir() -> Result<PathBuf, String> {
    let (path, error) = resolve_data_dir();
    if let Some(error) = error {
        return Err(error);
    }
    ensure_dir(&path)?;
    Ok(path)
}
fn nonempty_env(name: &str) -> Option<std::ffi::OsString> {
    std::env::var_os(name).filter(|v| !v.is_empty())
}
pub(crate) fn home_dir() -> Result<PathBuf, String> {
    if let Some(home) = nonempty_env("USERPROFILE").or_else(|| nonempty_env("HOME")) {
        return Ok(PathBuf::from(home));
    }
    if let (Some(drive), Some(path)) = (nonempty_env("HOMEDRIVE"), nonempty_env("HOMEPATH")) {
        let mut home = drive;
        home.push(path);
        return Ok(PathBuf::from(home));
    }
    Err("无法确定用户主目录；请显式设置 RCLONE_TRAY_DATA_DIR。".into())
}
fn resolve_data_dir() -> (PathBuf, Option<String>) {
    if let Some(value) = nonempty_env("RCLONE_TRAY_DATA_DIR") {
        let path = PathBuf::from(value);
        if path == Path::new("~") || path.starts_with("~") {
            return match home_dir() {
                Ok(home) => (
                    home.join(path.strip_prefix("~").unwrap_or(Path::new(""))),
                    None,
                ),
                Err(e) => (path, Some(e)),
            };
        }
        return (path, None);
    }
    if let Some(base) = nonempty_env("APPDATA") {
        return (PathBuf::from(base).join(APP_NAME), None);
    }
    match home_dir() {
        Ok(home) => (home.join(format!(".{APP_NAME}")), None),
        Err(error) => (PathBuf::new(), Some(error)),
    }
}
fn ensure_dir(path: &Path) -> Result<(), String> {
    fs::create_dir_all(path).map_err(|e| io_error("创建应用数据目录", &e))
}
fn subdir(name: &str) -> Result<PathBuf, String> {
    let path = try_app_data_dir()?.join(name);
    ensure_dir(&path)?;
    Ok(path)
}
pub fn logs_dir() -> Result<PathBuf, String> {
    subdir("logs")
}
pub fn cache_dir() -> Result<PathBuf, String> {
    subdir("cache")
}
pub fn temp_dir() -> Result<PathBuf, String> {
    subdir("temp")
}

pub struct ConfigStore {
    directory: PathBuf,
    path_error: Option<String>,
}
impl ConfigStore {
    pub fn new() -> Self {
        let (directory, path_error) = resolve_data_dir();
        Self {
            directory,
            path_error,
        }
    }
    /// Explicit roots avoid process-global environment changes in isolated consumers and tests.
    pub fn in_dir(directory: PathBuf) -> Self {
        Self {
            directory,
            path_error: None,
        }
    }
    pub fn data_dir(&self) -> &Path {
        &self.directory
    }
    pub fn config_path(&self) -> PathBuf {
        self.directory.join("config.json")
    }
    fn prepare(&self) -> Result<(), String> {
        if let Some(error) = &self.path_error {
            return Err(error.clone());
        }
        ensure_dir(&self.directory)
    }
    pub fn load(&self) -> Result<(AppConfig, Fingerprint), String> {
        self.prepare()?;
        let raw = read_bytes(&self.config_path())?;
        let fingerprint = digest(raw.as_deref());
        let config = match raw {
            Some(raw) => parse_config(&raw)?,
            None => AppConfig::default(),
        };
        Ok((config, fingerprint))
    }
    pub fn fingerprint(&self) -> Result<Fingerprint, String> {
        self.prepare()?;
        Ok(digest(read_bytes(&self.config_path())?.as_deref()))
    }
    pub fn save(&self, cfg: &AppConfig, expected: Fingerprint) -> Result<Fingerprint, String> {
        self.prepare()?;
        cfg.validate()?;
        // Persistent lock inode serializes independent application instances, including first save.
        let lock = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(self.directory.join(".config.lock"))
            .map_err(|e| io_error("打开配置事务锁", &e))?;
        FileExt::try_lock_exclusive(&lock).map_err(|e| io_error("锁定配置事务", &e))?;
        let path = self.config_path();
        let original = read_bytes(&path)?;
        if digest(original.as_deref()) != expected {
            return Err("配置已被外部修改；未保存。请重新加载后再应用草稿。".into());
        }
        if let Some(raw) = &original {
            parse_config(raw)?;
        }
        let encoded = encode_config(cfg)?;
        let (mut file, temporary) = create_temporary(&self.directory, ".config-", ".tmp")?;
        file.write_all(&encoded)
            .and_then(|_| file.flush())
            .and_then(|_| file.sync_all())
            .map_err(|e| io_error("写入应用配置临时文件", &e))?;
        drop(file);
        if read_bytes(&path)? != original {
            return Err("保存期间配置被外部修改；未覆盖。请重新加载后重试。".into());
        }
        replace_file(temporary.path(), &path)?;
        // Temporary guard removes only its own old path; after replacement that path is absent.
        Ok(digest(Some(&encoded)))
    }
}
impl Default for ConfigStore {
    fn default() -> Self {
        Self::new()
    }
}
fn read_bytes(path: &Path) -> Result<Option<Vec<u8>>, String> {
    match fs::read(path) {
        Ok(raw) => Ok(Some(raw)),
        Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(e) => Err(io_error("读取应用配置", &e)),
    }
}
fn digest(raw: Option<&[u8]>) -> Fingerprint {
    raw.map(|bytes| Sha256::digest(bytes).into())
}

pub(crate) struct TemporaryPath(PathBuf);
impl TemporaryPath {
    pub(crate) fn path(&self) -> &Path {
        &self.0
    }
}
impl Drop for TemporaryPath {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.0);
    }
}
pub(crate) fn create_temporary(
    directory: &Path,
    prefix: &str,
    suffix: &str,
) -> Result<(File, TemporaryPath), String> {
    for _ in 0..16 {
        let mut bytes = [0u8; 16];
        getrandom::getrandom(&mut bytes).map_err(|_| "无法生成安全临时文件名。".to_string())?;
        let mut name = String::from(prefix);
        for byte in bytes {
            use fmt::Write;
            let _ = write!(name, "{byte:02x}");
        }
        name.push_str(suffix);
        let path = directory.join(name);
        match OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(true)
            .open(&path)
        {
            Ok(file) => return Ok((file, TemporaryPath(path))),
            Err(e) if e.kind() == io::ErrorKind::AlreadyExists => continue,
            Err(e) => return Err(io_error("创建临时文件", &e)),
        }
    }
    Err("无法创建唯一临时文件。".into())
}
#[cfg(windows)]
fn replace_file(source: &Path, destination: &Path) -> Result<(), String> {
    use std::os::windows::ffi::OsStrExt;
    #[link(name = "kernel32")]
    extern "system" {
        fn MoveFileExW(existing: *const u16, new: *const u16, flags: u32) -> i32;
    }
    let source: Vec<u16> = source.as_os_str().encode_wide().chain(Some(0)).collect();
    let destination: Vec<u16> = destination
        .as_os_str()
        .encode_wide()
        .chain(Some(0))
        .collect();
    if unsafe { MoveFileExW(source.as_ptr(), destination.as_ptr(), 0x1 | 0x8) } == 0 {
        Err(io_error("原子保存应用配置", &io::Error::last_os_error()))
    } else {
        Ok(())
    }
}
#[cfg(not(windows))]
fn replace_file(source: &Path, destination: &Path) -> Result<(), String> {
    fs::rename(source, destination).map_err(|e| io_error("原子保存应用配置", &e))
}

// serde_json::Value alone keeps the last duplicate. This recursive visitor rejects all duplicates
// before inspecting schemas, including duplicate fields inside encrypted credential objects.
struct Unique(Value);
impl<'de> Deserialize<'de> for Unique {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct Strict;
        impl<'de> Visitor<'de> for Strict {
            type Value = Unique;
            fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str("valid JSON without duplicate fields")
            }
            fn visit_unit<E: de::Error>(self) -> Result<Unique, E> {
                Ok(Unique(Value::Null))
            }
            fn visit_bool<E: de::Error>(self, v: bool) -> Result<Unique, E> {
                Ok(Unique(Value::Bool(v)))
            }
            fn visit_i64<E: de::Error>(self, v: i64) -> Result<Unique, E> {
                Ok(Unique(Value::Number(v.into())))
            }
            fn visit_u64<E: de::Error>(self, v: u64) -> Result<Unique, E> {
                Ok(Unique(Value::Number(v.into())))
            }
            fn visit_f64<E: de::Error>(self, v: f64) -> Result<Unique, E> {
                Number::from_f64(v)
                    .map(|n| Unique(Value::Number(n)))
                    .ok_or_else(|| E::custom("invalid number"))
            }
            fn visit_str<E: de::Error>(self, v: &str) -> Result<Unique, E> {
                Ok(Unique(Value::String(v.into())))
            }
            fn visit_string<E: de::Error>(self, v: String) -> Result<Unique, E> {
                Ok(Unique(Value::String(v)))
            }
            fn visit_seq<A: SeqAccess<'de>>(self, mut a: A) -> Result<Unique, A::Error> {
                let mut values = Vec::new();
                while let Some(Unique(value)) = a.next_element()? {
                    values.push(value);
                }
                Ok(Unique(Value::Array(values)))
            }
            fn visit_map<A: MapAccess<'de>>(self, mut a: A) -> Result<Unique, A::Error> {
                let mut values = Map::new();
                while let Some(key) = a.next_key::<String>()? {
                    if values.contains_key(&key) {
                        return Err(de::Error::custom("duplicate field"));
                    }
                    let Unique(value) = a.next_value()?;
                    values.insert(key, value);
                }
                Ok(Unique(Value::Object(values)))
            }
        }
        deserializer.deserialize_any(Strict)
    }
}
fn parse_config(raw: &[u8]) -> Result<AppConfig, String> {
    let raw = raw.strip_prefix(&[0xef, 0xbb, 0xbf]).unwrap_or(raw);
    let Unique(value) = serde_json::from_slice(raw)
        .map_err(|_| "配置不是有效 UTF-8 JSON 或包含重复字段；原文件未被重置。".to_string())?;
    let Value::Object(mut values) = value else {
        return Err("配置根节点必须是 JSON 对象；原文件未被重置。".into());
    };
    let schema = match values.remove("schema_version") {
        None => 1,
        Some(Value::Number(v)) if v.as_u64() == Some(1) => 1,
        Some(Value::Number(v)) if v.as_u64() == Some(2) => 2,
        _ => return Err("不支持此配置 schema_version；请使用兼容版本。".into()),
    };
    if values.keys().any(|key| !FIELDS.contains(&key.as_str())) {
        return Err("配置包含无法识别的字段；请使用兼容版本，避免丢失数据。".into());
    }
    if schema == 2 && FIELDS.iter().any(|key| !values.contains_key(*key)) {
        return Err("当前版本配置缺少必需字段；不能静默填充默认值。".into());
    }
    let mut cfg = AppConfig::default();
    macro_rules! take {
        ($field:ident, $variant:ident) => {
            if let Some(value) = values.remove(stringify!($field)) {
                match value {
                    Value::$variant(v) => cfg.$field = v,
                    _ => {
                        return Err(concat!(
                            "配置字段 ",
                            stringify!($field),
                            " 类型错误；请修复原文件。"
                        )
                        .into())
                    }
                }
            }
        };
    }
    take!(rclone_path, String);
    take!(gui_no_auth, Bool);
    take!(gui_user, String);
    take!(gui_auto_start, Bool);
    take!(startup_enabled, Bool);
    take!(auto_mount_on_boot, Bool);
    take!(theme, String);
    take!(rclone_conf, String);
    take!(reduce_motion, Bool);
    if let Some(value) = values.remove("mount_delay_seconds") {
        cfg.mount_delay_seconds = value
            .as_u64()
            .and_then(|n| u32::try_from(n).ok())
            .ok_or("自动挂载延迟须为 0–86400 秒的整数。")?;
    }
    if let Some(value) = values.remove("mounts") {
        cfg.mounts = serde_json::from_value(value)
            .map_err(|_| "挂载配置格式错误、缺少身份字段或包含未知字段。".to_string())?;
    }
    if let Some(value) = values.remove("gui_pass") {
        cfg.gui_pass = decode_password(value, schema == 1)?;
    }
    cfg.validate()?;
    Ok(cfg)
}
fn decode_password(value: Value, legacy: bool) -> Result<String, String> {
    if legacy {
        if let Value::String(value) = value {
            return Ok(value);
        }
    }
    let Value::Object(mut fields) = value else {
        return Err("gui_pass 必须是受保护凭据；勿覆盖原文件。".into());
    };
    if fields.len() != 2 || !fields.contains_key("scheme") || !fields.contains_key("data") {
        return Err("受保护凭据格式无效；勿覆盖原文件。".into());
    }
    if fields.remove("scheme") != Some(Value::String("dpapi".into())) {
        return Err("不支持此凭据保护格式；请使用兼容版本和原 Windows 用户。".into());
    }
    let Some(Value::String(data)) = fields.remove("data") else {
        return Err("受保护凭据格式无效。".into());
    };
    let bytes = STANDARD
        .decode(data)
        .map_err(|_| "受保护密码数据损坏；请从备份恢复。".to_string())?;
    if bytes.is_empty() {
        return Err("受保护密码数据损坏；请从备份恢复。".into());
    }
    String::from_utf8(dpapi(&bytes, false)?)
        .map_err(|_| "受保护密码数据损坏；请从备份恢复。".into())
}
fn encode_config(cfg: &AppConfig) -> Result<Vec<u8>, String> {
    #[derive(Serialize)]
    struct Password {
        scheme: &'static str,
        data: String,
    }
    #[derive(Serialize)]
    struct DiskConfig<'a> {
        schema_version: u32,
        rclone_path: &'a str,
        gui_no_auth: bool,
        gui_user: &'a str,
        gui_pass: Password,
        gui_auto_start: bool,
        startup_enabled: bool,
        auto_mount_on_boot: bool,
        mount_delay_seconds: u32,
        theme: &'a str,
        mounts: &'a [MountConfig],
        rclone_conf: &'a str,
        reduce_motion: bool,
    }
    let stored = DiskConfig {
        schema_version: 2,
        rclone_path: &cfg.rclone_path,
        gui_no_auth: cfg.gui_no_auth,
        gui_user: &cfg.gui_user,
        gui_pass: Password {
            scheme: "dpapi",
            data: STANDARD.encode(dpapi(cfg.gui_pass.as_bytes(), true)?),
        },
        gui_auto_start: cfg.gui_auto_start,
        startup_enabled: cfg.startup_enabled,
        auto_mount_on_boot: cfg.auto_mount_on_boot,
        mount_delay_seconds: cfg.mount_delay_seconds,
        theme: &cfg.theme,
        mounts: &cfg.mounts,
        rclone_conf: &cfg.rclone_conf,
        reduce_motion: cfg.reduce_motion,
    };
    serde_json::to_vec_pretty(&stored).map_err(|_| "无法编码应用配置；原配置保持不变。".into())
}
#[cfg(not(windows))]
fn dpapi(_: &[u8], _: bool) -> Result<Vec<u8>, String> {
    Err("凭据加密需要 Windows DPAPI；无法在此系统读写受保护的密码。".into())
}
#[cfg(windows)]
fn dpapi(bytes: &[u8], encrypt: bool) -> Result<Vec<u8>, String> {
    use std::ffi::c_void;
    #[repr(C)]
    struct Blob {
        len: u32,
        data: *mut u8,
    }
    #[link(name = "crypt32")]
    extern "system" {
        fn CryptProtectData(
            source: *const Blob,
            description: *const u16,
            entropy: *const Blob,
            reserved: *mut c_void,
            prompt: *mut c_void,
            flags: u32,
            output: *mut Blob,
        ) -> i32;
        fn CryptUnprotectData(
            source: *const Blob,
            description: *mut *mut u16,
            entropy: *const Blob,
            reserved: *mut c_void,
            prompt: *mut c_void,
            flags: u32,
            output: *mut Blob,
        ) -> i32;
    }
    #[link(name = "kernel32")]
    extern "system" {
        fn LocalFree(memory: *mut c_void) -> *mut c_void;
    }
    let len = u32::try_from(bytes.len()).map_err(|_| "受保护凭据过长。".to_string())?;
    let source = Blob {
        len,
        data: bytes.as_ptr() as *mut u8,
    };
    let mut output = Blob {
        len: 0,
        data: std::ptr::null_mut(),
    };
    let description: Vec<u16> = APP_NAME.encode_utf16().chain(Some(0)).collect();
    let ok = unsafe {
        if encrypt {
            CryptProtectData(
                &source,
                description.as_ptr(),
                std::ptr::null(),
                std::ptr::null_mut(),
                std::ptr::null_mut(),
                1,
                &mut output,
            )
        } else {
            CryptUnprotectData(
                &source,
                std::ptr::null_mut(),
                std::ptr::null(),
                std::ptr::null_mut(),
                std::ptr::null_mut(),
                1,
                &mut output,
            )
        }
    };
    if ok == 0 {
        return Err(
            "Windows DPAPI 凭据保护失败。请使用保存此配置的 Windows 用户；原配置保持不变。".into(),
        );
    }
    let result = if output.len == 0 {
        Vec::new()
    } else {
        unsafe { std::slice::from_raw_parts(output.data, output.len as usize).to_vec() }
    };
    unsafe {
        LocalFree(output.data.cast());
    }
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;
    struct Directory(PathBuf);
    impl Directory {
        fn new() -> Self {
            let mut bytes = [0u8; 16];
            getrandom::getrandom(&mut bytes).unwrap();
            let name: String = bytes.iter().map(|b| format!("{b:02x}")).collect();
            let path = std::env::temp_dir().join(format!("rclone-config-test-{name}"));
            fs::create_dir(&path).unwrap();
            Self(path)
        }
        fn store(&self) -> ConfigStore {
            ConfigStore::in_dir(self.0.clone())
        }
    }
    impl Drop for Directory {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }
    #[test]
    fn corrupt_file_is_never_reset_and_errors_hide_secrets() {
        let dir = Directory::new();
        let store = dir.store();
        let original = br#"{"gui_pass":"private-value","mounts":[broken]}"#;
        fs::write(store.config_path(), original).unwrap();
        let error = store.load().unwrap_err();
        assert!(!error.contains("private-value"));
        assert!(store
            .save(&AppConfig::default(), store.fingerprint().unwrap())
            .is_err());
        assert_eq!(fs::read(store.config_path()).unwrap(), original);
    }
    #[test]
    fn missing_file_and_bom_reads_do_not_mutate_storage() {
        let dir = Directory::new();
        let store = dir.store();
        let (_, fp) = store.load().unwrap();
        assert_eq!(fp, None);
        assert!(!store.config_path().exists());
        let bytes = b"\xef\xbb\xbf{\"theme\":\"light\"}";
        fs::write(store.config_path(), bytes).unwrap();
        let (cfg, fp) = store.load().unwrap();
        assert_eq!(cfg.theme, "light");
        assert_eq!(fp, digest(Some(bytes)));
    }
    #[test]
    fn strict_schema_types_duplicates_unknowns_and_ids() {
        let dir = Directory::new();
        let store = dir.store();
        for raw in [
            r#"{"startup_enabled":"false"}"#,
            r#"{"schema_version":true}"#,
            r#"{"schema_version":1.0}"#,
            r#"{"schema_version":3}"#,
            r#"{"schema_version":2}"#,
            r#"{"future":0}"#,
            r#"{"theme":"light","theme":"dark"}"#,
            r#"{"gui_pass":{"scheme":"dpapi","scheme":"dpapi","data":"x"}}"#,
            r#"{"mounts":[{"id":"one","id":"two","name":"n","remote":"r:","mount_point":"Y:"}]}"#,
            r#"{"mounts":[{"id":"one","name":"n","remote":"r:","mount_point":"Y:","future":true}]}"#,
            r#"{"mount_delay_seconds":-1}"#,
            r#"{"mount_delay_seconds":86401}"#,
            r#"{"gui_user":null}"#,
        ] {
            fs::write(store.config_path(), raw).unwrap();
            assert!(store.load().is_err(), "accepted invalid schema boundary");
            assert!(store
                .save(&AppConfig::default(), store.fingerprint().unwrap())
                .is_err());
            assert_eq!(fs::read_to_string(store.config_path()).unwrap(), raw);
        }
        let raw =
            r#"{"mounts":[{"id":"stable_old-ID","name":"n","remote":"r:","mount_point":"Y:"}]}"#;
        fs::write(store.config_path(), raw).unwrap();
        let (cfg, _) = store.load().unwrap();
        assert_eq!(cfg.mounts[0].id, "stable_old-ID");
        let mut duplicate = cfg.clone();
        duplicate.mounts.push(cfg.mounts[0].clone());
        assert!(duplicate.validate().is_err());
    }
    #[test]
    fn explicit_bad_root_does_not_fall_back() {
        let dir = Directory::new();
        let file = dir.0.join("not-directory");
        fs::write(&file, b"x").unwrap();
        let store = ConfigStore::in_dir(file);
        assert!(store.load().is_err());
        assert!(store.fingerprint().is_err());
    }
    #[test]
    fn credential_debug_is_redacted() {
        let cfg = AppConfig {
            gui_user: "private-user".into(),
            gui_pass: "private-password".into(),
            ..Default::default()
        };
        let text = format!("{cfg:?}");
        assert!(!text.contains("private-user"));
        assert!(!text.contains("private-password"));
    }
    #[cfg(windows)]
    #[test]
    fn complete_schema_two_and_damaged_dpapi_are_never_defaulted_or_overwritten() {
        let dir = Directory::new();
        let store = dir.store();
        store.save(&AppConfig::default(), None).unwrap();
        let saved: Value = serde_json::from_slice(&fs::read(store.config_path()).unwrap()).unwrap();
        for field in FIELDS {
            let mut value = saved.clone();
            value.as_object_mut().unwrap().remove(*field);
            let raw = serde_json::to_vec(&value).unwrap();
            fs::write(store.config_path(), &raw).unwrap();
            assert!(store.load().is_err());
            assert!(store
                .save(&AppConfig::default(), store.fingerprint().unwrap())
                .is_err());
            assert_eq!(fs::read(store.config_path()).unwrap(), raw);
        }
        for password in [
            serde_json::json!("plaintext-refused"),
            serde_json::json!({"scheme":"dpapi", "data":""}),
            serde_json::json!({"scheme":"dpapi", "data":"AQIDBA=="}),
            serde_json::json!({"scheme":"other", "data":"private-token"}),
            serde_json::json!({"scheme":"dpapi", "data":"bad base64!"}),
            serde_json::json!({"scheme":"dpapi", "data":"AQIDBA==", "future":true}),
        ] {
            let mut value = saved.clone();
            value["gui_pass"] = password;
            let raw = serde_json::to_vec(&value).unwrap();
            fs::write(store.config_path(), &raw).unwrap();
            let error = store.load().unwrap_err();
            assert!(!error.contains("private-token"));
            assert!(store
                .save(&AppConfig::default(), store.fingerprint().unwrap())
                .is_err());
            assert_eq!(fs::read(store.config_path()).unwrap(), raw);
        }
    }
    #[cfg(windows)]
    #[test]
    fn dpapi_migration_stale_cas_and_atomic_failure_preserve_original() {
        use std::os::windows::fs::OpenOptionsExt;
        let dir = Directory::new();
        let store = dir.store();
        let raw = r#"{"gui_user":"legacy-user","gui_pass":"legacy-password / 中文","mounts":[{"id":"stable_old-ID","name":"n","remote":"r:","mount_point":"Y:"}]}"#;
        fs::write(store.config_path(), raw).unwrap();
        let (cfg, before) = store.load().unwrap();
        assert_eq!(cfg.gui_pass, "legacy-password / 中文");
        assert_eq!(fs::read_to_string(store.config_path()).unwrap(), raw);
        let saved = store.save(&cfg, before).unwrap();
        let encoded = fs::read_to_string(store.config_path()).unwrap();
        assert!(!encoded.contains(&cfg.gui_pass));
        let (restored, same_read) = store.load().unwrap();
        assert_eq!(restored, cfg);
        assert_eq!(same_read, saved);
        let protected: Value = serde_json::from_str(&encoded).unwrap();
        let wire = STANDARD
            .decode(protected["gui_pass"]["data"].as_str().unwrap())
            .unwrap();
        assert_eq!(
            String::from_utf8(dpapi(&wire, false).unwrap()).unwrap(),
            cfg.gui_pass
        );
        let mut external = cfg.clone();
        external.theme = "light".into();
        store.save(&external, saved).unwrap();
        let original = fs::read(store.config_path()).unwrap();
        assert!(store.save(&cfg, saved).is_err());
        assert_eq!(fs::read(store.config_path()).unwrap(), original);
        // A real Windows handle allowing reads/writes but denying deletion forces atomic replacement to fail.
        let held = OpenOptions::new()
            .read(true)
            .share_mode(0x1 | 0x2)
            .open(store.config_path())
            .unwrap();
        assert!(store.save(&cfg, store.fingerprint().unwrap()).is_err());
        assert_eq!(fs::read(store.config_path()).unwrap(), original);
        drop(held);
        assert_eq!(store.load().unwrap().0, external);
    }
    #[cfg(windows)]
    #[test]
    fn independent_transactions_and_first_creation_conflict() {
        let dir = Directory::new();
        let store = dir.store();
        let lock = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(dir.0.join(".config.lock"))
            .unwrap();
        FileExt::try_lock_exclusive(&lock).unwrap();
        assert!(store.save(&AppConfig::default(), None).is_err());
        assert!(!store.config_path().exists());
        FileExt::unlock(&lock).unwrap();
        let fp = store.save(&AppConfig::default(), None).unwrap();
        assert!(fp.is_some());
        assert!(ConfigStore::in_dir(dir.0.clone())
            .save(&AppConfig::default(), None)
            .is_err());
    }
}
