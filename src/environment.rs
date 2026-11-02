//! Explicit-path-aware rclone discovery, bounded native probes, and strict remote enumeration.
use crate::config::{self, AppConfig};
use crate::contracts::EnvironmentInfo;
use std::collections::HashSet;
use std::fs;
use std::io::{Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

const VERSION_TIMEOUT: Duration = Duration::from_secs(15);
const OUTPUT_LIMIT: u64 = 64 * 1024;

/// Expand the same user/environment syntax accepted by the previous Windows implementation.
/// Unknown variables remain literal, rather than silently selecting a different path.
fn expand_with(value: &str, env: impl Fn(&str) -> Option<String>, home: Option<&Path>) -> PathBuf {
    let mut expanded = String::with_capacity(value.len());
    let mut offset = 0;
    while offset < value.len() {
        let remaining = &value[offset..];
        // ntpath.expandvars leaves single-quoted spans literal and recognizes %%/$$ escapes.
        if remaining.starts_with('\'') {
            let length = remaining[1..]
                .find('\'')
                .map(|end| end + 2)
                .unwrap_or(remaining.len());
            expanded.push_str(&remaining[..length]);
            offset += length;
            continue;
        }
        if remaining.starts_with("%%") {
            expanded.push('%');
            offset += 2;
            continue;
        }
        if remaining.starts_with("$$") {
            expanded.push('$');
            offset += 2;
            continue;
        }
        if remaining.starts_with('%') {
            if let Some(end) = remaining[1..].find('%') {
                let end = end + 1;
                let key = &remaining[1..end];
                if let Some(replacement) = env(key) {
                    expanded.push_str(&replacement);
                } else {
                    expanded.push_str(&remaining[..=end]);
                }
                offset += end + 1;
                continue;
            }
        }
        if remaining.starts_with('$') {
            let (key, consumed) = if remaining.starts_with("${") {
                match remaining[2..].find('}') {
                    Some(end) => (&remaining[2..end + 2], end + 3),
                    None => ("", 0),
                }
            } else {
                let length = remaining[1..]
                    .chars()
                    .take_while(|c| c.is_ascii_alphanumeric() || *c == '_' || *c == '-')
                    .map(char::len_utf8)
                    .sum::<usize>();
                (&remaining[1..1 + length], 1 + length)
            };
            if !key.is_empty() {
                if let Some(replacement) = env(key) {
                    expanded.push_str(&replacement);
                } else {
                    expanded.push_str(&remaining[..consumed]);
                }
                offset += consumed;
                continue;
            }
        }
        let ch = remaining.chars().next().unwrap();
        expanded.push(ch);
        offset += ch.len_utf8();
    }
    if let Some(home) = home {
        if expanded == "~" {
            return home.to_path_buf();
        }
        if expanded.starts_with("~/") || expanded.starts_with("~\\") {
            return home.join(&expanded[2..]);
        }
    }
    PathBuf::from(expanded)
}
fn expand(value: &str) -> PathBuf {
    expand_with(
        value,
        |key| std::env::var(key).ok(),
        config::home_dir().ok().as_deref(),
    )
}
fn regular_file(path: &Path, action: &str) -> Result<bool, String> {
    match fs::metadata(path) {
        Ok(metadata) => Ok(metadata.is_file()),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(false),
        Err(e) => Err(config::io_error(action, &e)),
    }
}
fn resolve_executable(path: &Path) -> Result<PathBuf, String> {
    if !regular_file(path, "检查 rclone 可执行文件")? {
        return Err("指定的 rclone 可执行文件不存在或不是文件；不会回退到其他程序。".into());
    }
    path.canonicalize()
        .map_err(|e| config::io_error("解析 rclone 可执行文件", &e))
}
fn candidate_paths(
    executable: &Path,
    source: &Path,
    path: Option<&std::ffi::OsStr>,
    env: impl Fn(&str) -> Option<String>,
    home: Option<&Path>,
) -> Vec<PathBuf> {
    let mut candidates = Vec::new();
    if let Some(directory) = executable.parent() {
        candidates.push(directory.join("rclone.exe"));
        candidates.push(directory.join("rclone").join("rclone.exe"));
    }
    candidates.push(source.join("rclone").join("rclone.exe"));
    candidates.push(source.join("rclone.exe"));
    if let Some(path) = path {
        for directory in std::env::split_paths(path) {
            candidates.push(directory.join("rclone.exe"));
            #[cfg(not(windows))]
            candidates.push(directory.join("rclone"));
        }
    }
    for template in [
        "%ProgramFiles%\\rclone",
        "%ProgramFiles(x86)%\\rclone",
        "%LocalAppData%\\rclone",
        "%LocalAppData%\\Programs\\rclone",
        "%USERPROFILE%\\rclone",
        "C:\\rclone",
        "C:\\tools\\rclone",
        "%USERPROFILE%\\scoop\\apps\\rclone\\current",
        "%LOCALAPPDATA%\\Microsoft\\WinGet\\Links",
    ] {
        candidates.push(expand_with(template, &env, home).join("rclone.exe"));
    }
    let mut seen = HashSet::new();
    candidates.retain(|candidate| seen.insert(candidate.clone()));
    candidates
}
pub fn find_rclone(preferred: &str) -> Result<PathBuf, String> {
    if !preferred.is_empty() {
        return resolve_executable(&expand(preferred));
    }
    let executable =
        std::env::current_exe().map_err(|e| config::io_error("确定应用可执行文件位置", &e))?;
    let path = std::env::var_os("PATH");
    let home = config::home_dir().ok();
    let candidates = candidate_paths(
        &executable,
        Path::new(env!("CARGO_MANIFEST_DIR")),
        path.as_deref(),
        |key| std::env::var(key).ok(),
        home.as_deref(),
    );
    for candidate in candidates {
        if regular_file(&candidate, "检查 rclone 可执行文件")? {
            return resolve_executable(&candidate);
        }
    }
    Err("未找到 rclone；请选择 rclone.exe 或将其置于应用旁边。".into())
}
fn parse_version(text: &str) -> Option<String> {
    static PATTERN: std::sync::LazyLock<regex::Regex> = std::sync::LazyLock::new(|| {
        regex::Regex::new(r"(?m)^rclone v(\d+\.\d+(?:\.\d+)?)(?:[-+][^\s]+)?\s*$")
            .expect("constant version pattern")
    });
    PATTERN
        .captures(text)
        .map(|captures| captures[1].to_string())
}
struct VersionChild {
    child: std::process::Child,
    reaped: bool,
    #[cfg(windows)]
    job: version_job::Job,
}
impl Drop for VersionChild {
    fn drop(&mut self) {
        if !self.reaped {
            let _ = self.child.kill();
            let _ = self.child.try_wait();
        }
    }
}
pub fn get_version(exe: &Path) -> Result<String, String> {
    if !regular_file(exe, "检查 rclone 版本程序")? {
        return Err("rclone 版本程序不存在或不是文件。".into());
    }
    let directory = config::temp_dir()?;
    let mut command = Command::new(exe);
    command.arg("version");
    probe_command(command, &directory, VERSION_TIMEOUT)
}
// File-backed output avoids unbounded pipe-reader joins when a child inherits stdout.
fn probe_command(
    mut command: Command,
    directory: &Path,
    timeout: Duration,
) -> Result<String, String> {
    let (mut output, temporary) = config::create_temporary(directory, ".version-", ".tmp")?;
    let child_output = output
        .try_clone()
        .map_err(|e| config::io_error("准备版本输出", &e))?;
    command
        .stdin(Stdio::null())
        .stdout(Stdio::from(child_output))
        .stderr(Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000 | 0x00000004);
    }
    #[cfg(windows)]
    let job = version_job::Job::new()?;
    let child = command
        .spawn()
        .map_err(|e| config::io_error("启动 rclone 版本检查", &e))?;
    let mut child = VersionChild {
        child,
        reaped: false,
        #[cfg(windows)]
        job,
    };
    #[cfg(windows)]
    child.job.attach_and_resume(&child.child)?;
    let deadline = Instant::now() + timeout;
    let status = loop {
        if output
            .metadata()
            .map_err(|e| config::io_error("检查版本输出", &e))?
            .len()
            > OUTPUT_LIMIT
        {
            return Err("rclone 版本输出异常过大；检查已终止。".into());
        }
        if let Some(status) = child
            .child
            .try_wait()
            .map_err(|e| config::io_error("等待 rclone 版本检查", &e))?
        {
            child.reaped = true;
            break status;
        }
        if Instant::now() >= deadline {
            return Err("rclone 版本检查超时；检查已终止。".into());
        }
        std::thread::sleep(Duration::from_millis(20));
    };
    if !status.success() {
        return Err("rclone 版本检查返回失败；未接受输出。".into());
    }
    output
        .seek(SeekFrom::Start(0))
        .map_err(|e| config::io_error("读取版本输出", &e))?;
    let mut bytes = Vec::new();
    output
        .by_ref()
        .take(OUTPUT_LIMIT + 1)
        .read_to_end(&mut bytes)
        .map_err(|e| config::io_error("读取版本输出", &e))?;
    if bytes.len() as u64 > OUTPUT_LIMIT {
        return Err("rclone 版本输出异常过大；未接受输出。".into());
    }
    let version = parse_version(&String::from_utf8_lossy(&bytes))
        .ok_or("程序未返回有效 rclone 版本；请检查所选可执行文件。")?;
    drop(output);
    drop(temporary);
    Ok(version)
}

#[cfg(windows)]
mod version_job {
    use std::ffi::c_void;
    use std::os::windows::io::AsRawHandle;
    use std::ptr;
    #[repr(C)]
    #[derive(Default)]
    struct BasicLimits {
        user_time: i64,
        job_time: i64,
        flags: u32,
        minimum_working_set: usize,
        maximum_working_set: usize,
        active_process_limit: u32,
        affinity: usize,
        priority: u32,
        scheduling: u32,
    }
    #[repr(C)]
    #[derive(Default)]
    struct ExtendedLimits {
        basic: BasicLimits,
        io: [u64; 6],
        process_memory: usize,
        job_memory: usize,
        peak_process_memory: usize,
        peak_job_memory: usize,
    }
    #[repr(C)]
    struct ThreadEntry {
        size: u32,
        usage: u32,
        thread_id: u32,
        owner_pid: u32,
        base_priority: i32,
        delta_priority: i32,
        flags: u32,
    }
    struct Handle(*mut c_void);
    impl Drop for Handle {
        fn drop(&mut self) {
            unsafe {
                CloseHandle(self.0);
            }
        }
    }
    #[link(name = "kernel32")]
    extern "system" {
        fn CreateJobObjectW(attributes: *const c_void, name: *const u16) -> *mut c_void;
        fn SetInformationJobObject(
            job: *mut c_void,
            class: u32,
            information: *const c_void,
            length: u32,
        ) -> i32;
        fn AssignProcessToJobObject(job: *mut c_void, process: *mut c_void) -> i32;
        fn CloseHandle(handle: *mut c_void) -> i32;
        fn CreateToolhelp32Snapshot(flags: u32, pid: u32) -> *mut c_void;
        fn Thread32First(snapshot: *mut c_void, entry: *mut ThreadEntry) -> i32;
        fn Thread32Next(snapshot: *mut c_void, entry: *mut ThreadEntry) -> i32;
        fn OpenThread(access: u32, inherit: i32, thread_id: u32) -> *mut c_void;
        fn ResumeThread(thread: *mut c_void) -> u32;
    }
    pub(super) struct Job(*mut c_void);
    impl Job {
        pub(super) fn new() -> Result<Self, String> {
            let handle = unsafe { CreateJobObjectW(ptr::null(), ptr::null()) };
            if handle.is_null() {
                return Err(crate::config::io_error(
                    "创建版本检查进程作业",
                    &std::io::Error::last_os_error(),
                ));
            }
            let job = Self(handle);
            let mut limits = ExtendedLimits::default();
            limits.basic.flags = 0x2000;
            if unsafe {
                SetInformationJobObject(
                    job.0,
                    9,
                    (&limits as *const ExtendedLimits).cast(),
                    std::mem::size_of::<ExtendedLimits>() as u32,
                )
            } == 0
            {
                return Err(crate::config::io_error(
                    "限制版本检查进程作业",
                    &std::io::Error::last_os_error(),
                ));
            }
            Ok(job)
        }
        pub(super) fn attach_and_resume(&self, child: &std::process::Child) -> Result<(), String> {
            if unsafe { AssignProcessToJobObject(self.0, child.as_raw_handle()) } == 0 {
                return Err(crate::config::io_error(
                    "管理版本检查进程",
                    &std::io::Error::last_os_error(),
                ));
            }
            // Launch suspended, assign first, then resume the sole initial thread. Descendants
            // cannot escape job ownership in the interval between CreateProcess and assignment.
            let snapshot = unsafe { CreateToolhelp32Snapshot(0x00000004, 0) };
            if snapshot == (-1isize) as *mut c_void {
                return Err(crate::config::io_error(
                    "查找版本进程线程",
                    &std::io::Error::last_os_error(),
                ));
            }
            let snapshot = Handle(snapshot);
            let mut entry = ThreadEntry {
                size: std::mem::size_of::<ThreadEntry>() as u32,
                usage: 0,
                thread_id: 0,
                owner_pid: 0,
                base_priority: 0,
                delta_priority: 0,
                flags: 0,
            };
            let mut present = unsafe { Thread32First(snapshot.0, &mut entry) } != 0;
            while present {
                if entry.owner_pid == child.id() {
                    let thread = unsafe { OpenThread(0x0002, 0, entry.thread_id) };
                    if thread.is_null() {
                        return Err(crate::config::io_error(
                            "打开版本进程线程",
                            &std::io::Error::last_os_error(),
                        ));
                    }
                    let thread = Handle(thread);
                    if unsafe { ResumeThread(thread.0) } == u32::MAX {
                        return Err(crate::config::io_error(
                            "启动版本进程线程",
                            &std::io::Error::last_os_error(),
                        ));
                    }
                    return Ok(());
                }
                entry.size = std::mem::size_of::<ThreadEntry>() as u32;
                present = unsafe { Thread32Next(snapshot.0, &mut entry) } != 0;
            }
            Err("未找到版本检查进程的初始线程；检查已终止。".into())
        }
    }
    impl Drop for Job {
        fn drop(&mut self) {
            unsafe {
                CloseHandle(self.0);
            }
        }
    }
}

fn conf_candidates(
    explicit: &str,
    env: impl Fn(&str) -> Option<String>,
    home: Option<&Path>,
    isolated: Option<&Path>,
) -> (Vec<PathBuf>, bool) {
    if !explicit.is_empty() {
        return (vec![expand_with(explicit, &env, home)], true);
    }
    if let Some(value) = env("RCLONE_CONFIG").filter(|value| !value.is_empty()) {
        return (vec![expand_with(&value, &env, home)], true);
    }
    if let Some(directory) = isolated {
        return (vec![directory.join("rclone.conf")], false);
    }
    let mut candidates = Vec::new();
    for key in ["APPDATA", "XDG_CONFIG_HOME"] {
        if let Some(value) = env(key).filter(|value| !value.is_empty()) {
            candidates.push(PathBuf::from(value).join("rclone").join("rclone.conf"));
        }
    }
    if let Some(home) = home {
        candidates.push(home.join(".config").join("rclone").join("rclone.conf"));
        candidates.push(home.join(".rclone.conf"));
    }
    (candidates, false)
}
pub fn find_rclone_conf(explicit: &str) -> Result<Option<PathBuf>, String> {
    let env_explicit = if explicit.is_empty() {
        std::env::var_os("RCLONE_CONFIG")
            .filter(|value| !value.is_empty())
            .map(|value| {
                value.into_string().map_err(|_| {
                    "指定的 RCLONE_CONFIG 路径含无效 Unicode；不会回退到其他配置。".to_string()
                })
            })
            .transpose()?
    } else {
        None
    };
    let effective = if explicit.is_empty() {
        env_explicit.as_deref().unwrap_or("")
    } else {
        explicit
    };
    let isolated = if effective.is_empty()
        && std::env::var_os("RCLONE_TRAY_DATA_DIR").is_some_and(|value| !value.is_empty())
    {
        Some(config::try_app_data_dir()?)
    } else {
        None
    };
    let home = config::home_dir().ok();
    let (candidates, required) = conf_candidates(
        effective,
        |key| std::env::var(key).ok(),
        home.as_deref(),
        isolated.as_deref(),
    );
    for candidate in candidates {
        if regular_file(&candidate, "检查 rclone 配置")? {
            return Ok(Some(candidate));
        }
    }
    if required {
        Err("指定的 rclone 配置文件不存在或不是文件；请修正路径，不会回退到其他配置。".into())
    } else {
        Ok(None)
    }
}
fn decode_conf(raw: &[u8]) -> Result<std::borrow::Cow<'_, str>, String> {
    let utf8 = raw.strip_prefix(&[0xef, 0xbb, 0xbf]).unwrap_or(raw);
    if let Ok(text) = std::str::from_utf8(utf8) {
        return Ok(std::borrow::Cow::Borrowed(text));
    }
    // Python's strict GBK rejects the Windows-936 standalone euro byte and undefined PUA ranges.
    let mut offset = 0;
    while offset < raw.len() {
        let lead = raw[offset];
        if lead <= 0x7f {
            offset += 1;
            continue;
        }
        if !(0x81..=0xfe).contains(&lead) || offset + 1 >= raw.len() {
            return Err("rclone 配置编码无法识别；请保存为 UTF-8。".into());
        }
        let trail = raw[offset + 1];
        if !(0x40..=0xfe).contains(&trail)
            || trail == 0x7f
            || ((0xaa..=0xaf).contains(&lead) && trail >= 0xa1)
            || (lead >= 0xf8 && trail >= 0xa1)
            || ((0xa1..=0xa7).contains(&lead) && trail <= 0xa0)
        {
            return Err("rclone 配置编码无法识别；请保存为 UTF-8。".into());
        }
        offset += 2;
    }
    encoding_rs::GBK
        .decode_without_bom_handling_and_without_replacement(raw)
        .ok_or_else(|| "rclone 配置编码无法识别；请保存为 UTF-8。".into())
}
fn parse_remotes(text: &str) -> Result<Vec<String>, String> {
    if text.starts_with("RCLONE_ENCRYPT_V") {
        return Err(
            "rclone 配置已加密；无法直接枚举 remote，请在 rclone 中解锁配置或手动输入已有 remote。"
                .into(),
        );
    }
    let invalid = || "rclone 配置格式无效；请通过 rclone config 修复。".to_string();
    let mut sections = HashSet::new();
    let mut options = HashSet::new();
    let mut remotes = Vec::new();
    let mut section: Option<String> = None;
    let mut previous_option = false;
    let mut previous_indent = 0;
    for raw_line in text.lines() {
        let line = raw_line.trim();
        if line.is_empty() || line.starts_with('#') || line.starts_with(';') {
            continue;
        }
        let indent = raw_line.chars().take_while(|ch| ch.is_whitespace()).count();
        if previous_option && indent > previous_indent {
            continue;
        }
        previous_indent = indent;
        if line.starts_with('[') {
            if let Some(end) = line.rfind(']') {
                let name = &line[1..end];
                if name.is_empty() {
                    return Err(invalid());
                }
                if name != "DEFAULT" && !sections.insert(name.to_string()) {
                    return Err(invalid());
                }
                if name != "DEFAULT" && !name.trim().is_empty() {
                    remotes.push(format!("{name}:"));
                }
                section = Some(name.to_string());
                previous_option = false;
                continue;
            }
        }
        let Some(section) = &section else {
            return Err(invalid());
        };
        let Some(delimiter) = line.find(['=', ':']) else {
            return Err(invalid());
        };
        let option = line[..delimiter].trim().to_lowercase();
        if option.is_empty() || !options.insert((section.clone(), option)) {
            return Err(invalid());
        }
        previous_option = true;
    }
    Ok(remotes)
}
pub fn list_remotes(conf: Option<&Path>) -> Result<Vec<String>, String> {
    let Some(path) = conf else {
        return Ok(Vec::new());
    };
    let raw = fs::read(path).map_err(|e| config::io_error("读取 rclone 配置", &e))?;
    parse_remotes(&decode_conf(&raw)?)
}
pub fn winfsp_installed() -> bool {
    #[cfg(not(windows))]
    {
        false
    }
    #[cfg(windows)]
    {
        let mut roots = Vec::new();
        for view in [0x0100, 0x0200] {
            if let Ok(Some(directory)) = crate::startup::winfsp_install_dir(view) {
                if !directory.is_empty() {
                    roots.push(expand(&directory));
                }
            }
        }
        for variable in ["ProgramFiles(x86)", "ProgramFiles"] {
            if let Some(directory) = std::env::var_os(variable).filter(|value| !value.is_empty()) {
                roots.push(PathBuf::from(directory).join("WinFsp"));
            }
        }
        let runtime = if cfg!(target_pointer_width = "64") {
            "winfsp-x64.dll"
        } else {
            "winfsp-x86.dll"
        };
        roots
            .into_iter()
            .any(|directory| directory.join("bin").join(runtime).is_file())
    }
}
pub fn inspect(cfg: &AppConfig) -> EnvironmentInfo {
    let mut info = EnvironmentInfo {
        winfsp: winfsp_installed(),
        ..EnvironmentInfo::default()
    };
    let mut errors = Vec::new();
    match find_rclone(&cfg.rclone_path) {
        Ok(exe) => {
            match get_version(&exe) {
                Ok(version) => info.version = version,
                Err(error) => errors.push(error),
            };
            info.exe = Some(exe);
        }
        Err(error) => errors.push(error),
    }
    match find_rclone_conf(&cfg.rclone_conf) {
        Ok(conf) => {
            match list_remotes(conf.as_deref()) {
                Ok(remotes) => info.remotes = remotes,
                Err(error) => errors.push(error),
            };
            info.conf = conf;
        }
        Err(error) => errors.push(error),
    }
    info.error = errors.join("\n");
    info
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
            let path = std::env::temp_dir().join(format!("rclone-environment-test-{name}"));
            fs::create_dir(&path).unwrap();
            Self(path)
        }
    }
    impl Drop for Directory {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }
    #[test]
    fn explicit_invalid_never_selects_another_executable() {
        let dir = Directory::new();
        assert!(find_rclone(dir.0.join("missing-rclone.exe").to_str().unwrap()).is_err());
        assert!(find_rclone(dir.0.to_str().unwrap()).is_err());
    }
    #[test]
    fn environment_override_and_isolation_do_not_fall_back_to_user_conf() {
        let root = Path::new("isolated");
        let home = Path::new("home");
        let (paths, required) = conf_candidates("", |_| None, Some(home), Some(root));
        assert_eq!(paths, vec![root.join("rclone.conf")]);
        assert!(!required);
        let (paths, required) = conf_candidates(
            "",
            |key| {
                if key == "RCLONE_CONFIG" {
                    Some("env.conf".into())
                } else {
                    None
                }
            },
            Some(home),
            Some(root),
        );
        assert_eq!(paths, vec![PathBuf::from("env.conf")]);
        assert!(required);
        let (paths, required) = conf_candidates(
            "chosen.conf",
            |_| Some("ignored.conf".into()),
            Some(home),
            Some(root),
        );
        assert_eq!(paths, vec![PathBuf::from("chosen.conf")]);
        assert!(required);
        assert_eq!(
            expand_with(
                "~/中文/%ROOT%/${LEAF}",
                |key| match key {
                    "ROOT" => Some("nested".into()),
                    "LEAF" => Some("file".into()),
                    _ => None,
                },
                Some(home)
            ),
            home.join("中文/nested/file")
        );
        assert_eq!(
            expand_with(
                "'%ROOT%'/%MISSING%/%%/$$",
                |key| if key == "ROOT" {
                    Some("private-value".into())
                } else {
                    None
                },
                None
            ),
            Path::new("'%ROOT%'/%MISSING%/%/$")
        );
    }
    #[test]
    fn real_config_files_accept_bom_gbk_and_reject_corruption_without_secrets() {
        let dir = Directory::new();
        let path = dir.0.join("rclone.conf");
        fs::write(&path, b"\xef\xbb\xbf[remote]\ntype = local\n[DEFAULT]\nsecret = untouched\n[other]\ntoken: protected\n  continuation\n").unwrap();
        assert_eq!(
            list_remotes(Some(&path)).unwrap(),
            vec!["remote:", "other:"]
        );
        let (encoded, _, errors) = encoding_rs::GBK.encode("[中文]\ntype = local\n");
        assert!(!errors);
        fs::write(&path, &encoded).unwrap();
        assert_eq!(list_remotes(Some(&path)).unwrap(), vec!["中文:"]);
        for bytes in [
            b"not a section private-token".as_slice(),
            b"[r]\ntype=x\nTYPE=y".as_slice(),
            b"[r]\ntype=x\n[r]\ntype=y".as_slice(),
            b"RCLONE_ENCRYPT_V0:private-token".as_slice(),
            b"[r]\ntype=\x80".as_slice(),
            b"[r]\ntype=\x81".as_slice(),
        ] {
            fs::write(&path, bytes).unwrap();
            let error = list_remotes(Some(&path)).unwrap_err();
            assert!(!error.contains("private-token"));
            assert_eq!(fs::read(&path).unwrap(), bytes);
        }
        assert_eq!(list_remotes(None).unwrap(), Vec::<String>::new());
    }
    #[test]
    fn real_success_without_version_is_not_rclone() {
        let dir = Directory::new();
        let path = dir.0.join("not-program.exe");
        fs::write(&path, b"not executable").unwrap();
        assert!(probe_command(Command::new(&path), &dir.0, Duration::from_millis(100)).is_err());
        assert_eq!(
            parse_version("rclone v1.72.3\n- os/version: windows\n"),
            Some("1.72.3".into())
        );
        assert_eq!(
            parse_version("rclone v1.73.0-beta.123+abc\n"),
            Some("1.73.0".into())
        );
        for text in [
            "rclone v1",
            "not rclone v1.72",
            "rclone v1.72 trailing",
            "rclone v1.2.3.4",
        ] {
            assert_eq!(parse_version(text), None);
        }
    }
    #[cfg(windows)]
    #[test]
    fn native_version_probe_checks_exit_output_bounds_and_deadline() {
        let dir = Directory::new();
        let shell = PathBuf::from(std::env::var_os("SystemRoot").unwrap())
            .join("System32")
            .join("cmd.exe");
        let run = |script: &str, timeout: Duration| {
            let mut command = Command::new(&shell);
            command.args(["/D", "/C", script]);
            probe_command(command, &dir.0, timeout)
        };
        assert!(run("echo rclone v1.72.3 & exit /b 5", Duration::from_secs(2)).is_err());
        let started = Instant::now();
        let _error = run("ping -n 20 127.0.0.1 >nul", Duration::from_millis(100)).unwrap_err();
        assert!(started.elapsed() < Duration::from_secs(3));
        assert!(run(
            "for /L %i in (1,1,20000) do @echo rclone v1.72.3",
            Duration::from_secs(2)
        )
        .is_err());
        assert_eq!(fs::read_dir(&dir.0).unwrap().count(), 0);
    }
}
