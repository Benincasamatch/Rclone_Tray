//! Windows mount-point leases. Never recursively delete or follow reparse points.
use std::fs::{self, File, OpenOptions};
use std::io;
use std::os::windows::ffi::OsStrExt;
use std::os::windows::fs::{MetadataExt, OpenOptionsExt};
use std::os::windows::io::AsRawHandle;
use std::path::{Component, Path, PathBuf, Prefix};
use std::ptr;
use std::thread;
use std::time::{Duration, Instant};

use windows_sys::Win32::Foundation::{
    ERROR_BAD_NET_NAME, ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND,
};
use windows_sys::Win32::NetworkManagement::NetManagement::NetApiBufferFree;
use windows_sys::Win32::NetworkManagement::WNet::{
    WNetGetUniversalNameW, UNIVERSAL_NAME_INFOW, UNIVERSAL_NAME_INFO_LEVEL,
};
use windows_sys::Win32::Storage::FileSystem::{
    FileDispositionInfo, GetFileAttributesW, GetFileInformationByHandle, GetLogicalDrives,
    NetShareGetInfo, QueryDosDeviceW, SetFileInformationByHandle, BY_HANDLE_FILE_INFORMATION,
    FILE_ATTRIBUTE_DIRECTORY, FILE_ATTRIBUTE_REPARSE_POINT, FILE_DISPOSITION_INFO,
    FILE_FLAG_BACKUP_SEMANTICS, FILE_FLAG_OPEN_REPARSE_POINT, FILE_READ_ATTRIBUTES,
    FILE_SHARE_READ, INVALID_FILE_ATTRIBUTES,
};

use super::io_message;
use crate::config::MountConfig;

const DELETE_ACCESS: u32 = 0x0001_0000;
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct Identity {
    volume: u32,
    index: u64,
}
struct Placeholder {
    file: File,
    identity: Identity,
}

pub(super) struct Lease {
    target: PathBuf,
    drive: bool,
    // Pins every ordinary ancestor against write/reparse mutation and
    // rename/deletion for the lease's lifetime.
    _ancestors: Vec<File>,
    placeholder: Option<Placeholder>,
    restoration: Option<Placeholder>,
    removed: bool,
    spawned: bool,
}
impl Lease {
    pub(super) fn remove_placeholder(&mut self) -> Result<(), String> {
        if self.drive {
            if drive_present(&self.target.to_string_lossy())? {
                return Err("盘符在启动前已被外部占用；未修改任何对象。".into());
            }
            return Ok(());
        }
        let Some(placeholder) = &self.placeholder else {
            match fs::symlink_metadata(&self.target) {
                Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(()),
                Err(error) => return Err(io_message("重新确认挂载点", &error)),
                Ok(_) => return Err("挂载点在启动前被其他对象占用；未删除任何对象。".into()),
            }
        };
        let (identity, attributes) = information(&placeholder.file)?;
        if identity != placeholder.identity
            || attributes & FILE_ATTRIBUTE_REPARSE_POINT != 0
            || attributes & FILE_ATTRIBUTE_DIRECTORY == 0
        {
            return Err("挂载点在启动前被外部替换；未删除任何对象。".into());
        }
        // The retained handle disallows FILE_SHARE_DELETE. Delete-by-handle
        // removes precisely the checked identity, never a raced replacement.
        ensure_empty(&self.target)?;
        let disposition = FILE_DISPOSITION_INFO { DeleteFile: true };
        let success = unsafe {
            SetFileInformationByHandle(
                placeholder.file.as_raw_handle(),
                FileDispositionInfo,
                &disposition as *const FILE_DISPOSITION_INFO as *const _,
                std::mem::size_of_val(&disposition) as u32,
            )
        };
        if success == 0 {
            return Err(io_message(
                "非递归删除原空目录",
                &io::Error::last_os_error(),
            ));
        }
        self.removed = true;
        self.placeholder = None; // Closing the checked handle completes deletion.
        Ok(())
    }
    pub(super) fn mark_spawned(&mut self) {
        self.spawned = true;
    }
    pub(super) fn cleanup(&mut self) -> Result<(), String> {
        if self.spawned {
            let deadline = Instant::now() + Duration::from_secs(7);
            loop {
                let present = if self.drive {
                    drive_present(&self.target.to_string_lossy())?
                } else {
                    directory_mount_present(&self.target)?
                };
                if !present {
                    break;
                }
                if Instant::now() >= deadline {
                    return Err(
                        "进程已退出但挂载仍存在；请关闭占用文件并重试停止，勿修改或删除此配置。"
                            .into(),
                    );
                }
                thread::sleep(Duration::from_millis(100));
            }
            self.spawned = false;
        }
        if self.removed {
            // Never accept, empty, or delete an externally-created replacement.
            // Restoration is creation-only; an occupied name retains this lease.
            if self.restoration.is_none() {
                fs::create_dir(&self.target).map_err(|error| {
                    if error.kind() == io::ErrorKind::AlreadyExists {
                        "挂载已卸载，但原空目录路径被其他对象占用；不会修改该对象，请处理后重试停止。".into()
                    } else { io_message("恢复原空目录", &error) }
                })?;
                let file = open_directory(&self.target, false)?;
                let (identity, _) = information(&file)?;
                self.restoration = Some(Placeholder { file, identity });
            }
            let restored = self.restoration.as_ref().expect("restoration identity");
            let (identity, attributes) = information(&restored.file)?;
            let verification = open_directory(&self.target, false)?;
            if identity != restored.identity
                || information(&verification)?.0 != identity
                || attributes & FILE_ATTRIBUTE_REPARSE_POINT != 0
                || attributes & FILE_ATTRIBUTE_DIRECTORY == 0
            {
                return Err("恢复空目录的身份发生变化；仍保留控制权，不会删除任何对象。".into());
            }
            ensure_empty(&self.target)?;
            self.removed = false;
            self.restoration = None;
        }
        Ok(())
    }
}
fn wide(value: &std::ffi::OsStr) -> Vec<u16> {
    value.encode_wide().chain(Some(0)).collect()
}
fn open_directory(path: &Path, delete: bool) -> Result<File, String> {
    let file = OpenOptions::new()
        .access_mode(FILE_READ_ATTRIBUTES | if delete { DELETE_ACCESS } else { 0 })
        .share_mode(FILE_SHARE_READ)
        .custom_flags(FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT)
        .open(path)
        .map_err(|e| io_message("锁定普通目录身份", &e))?;
    let (_, attributes) = information(&file)?;
    if attributes & FILE_ATTRIBUTE_DIRECTORY == 0 || attributes & FILE_ATTRIBUTE_REPARSE_POINT != 0
    {
        return Err("挂载目录及其父目录不能是文件、符号链接或重解析点；未删除任何对象。".into());
    }
    Ok(file)
}
fn information(file: &File) -> Result<(Identity, u32), String> {
    let mut info: BY_HANDLE_FILE_INFORMATION = unsafe { std::mem::zeroed() };
    if unsafe { GetFileInformationByHandle(file.as_raw_handle(), &mut info) } == 0 {
        return Err(io_message("读取目录身份", &io::Error::last_os_error()));
    }
    Ok((
        Identity {
            volume: info.dwVolumeSerialNumber,
            index: u64::from(info.nFileIndexHigh) << 32 | u64::from(info.nFileIndexLow),
        },
        info.dwFileAttributes,
    ))
}
fn ensure_empty(path: &Path) -> Result<(), String> {
    let mut entries = fs::read_dir(path).map_err(|e| io_message("检查空目录", &e))?;
    match entries.next() {
        None => Ok(()),
        Some(Ok(_)) => Err("目录挂载点必须为空；不会删除其中的文件。".into()),
        Some(Err(error)) => Err(io_message("读取目录内容", &error)),
    }
}
fn drive_letter(target: &str) -> Option<u8> {
    let bytes = target.as_bytes();
    if bytes.len() == 2 && bytes[0].is_ascii_alphabetic() && bytes[1] == b':' {
        Some(bytes[0].to_ascii_uppercase())
    } else {
        None
    }
}
fn drive_present(target: &str) -> Result<bool, String> {
    let letter = drive_letter(target).ok_or("挂载盘符格式无效。")?;
    let mask = unsafe { GetLogicalDrives() };
    if mask == 0 {
        return Err(io_message("检查已占用盘符", &io::Error::last_os_error()));
    }
    if mask & (1 << (letter - b'A')) != 0 {
        return Ok(true);
    }
    let target = [letter as u16, b':' as u16, 0];
    let mut destination = vec![0u16; 32768];
    if unsafe {
        QueryDosDeviceW(
            target.as_ptr(),
            destination.as_mut_ptr(),
            destination.len() as u32,
        )
    } != 0
    {
        return Ok(true);
    }
    let error = io::Error::last_os_error();
    if error.raw_os_error() == Some(ERROR_FILE_NOT_FOUND as i32) {
        Ok(false)
    } else {
        Err(io_message("检查 DOS 盘符映射", &error))
    }
}
fn directory_mount_present(path: &Path) -> Result<bool, String> {
    let path = wide(path.as_os_str());
    let attributes = unsafe { GetFileAttributesW(path.as_ptr()) };
    if attributes != INVALID_FILE_ATTRIBUTES {
        return Ok(attributes & FILE_ATTRIBUTE_REPARSE_POINT != 0);
    }
    let error = io::Error::last_os_error();
    if matches!(error.raw_os_error(), Some(code) if code == ERROR_FILE_NOT_FOUND as i32 || code == ERROR_PATH_NOT_FOUND as i32)
    {
        Ok(false)
    } else {
        Err(io_message("确认目录挂载是否卸载", &error))
    }
}
pub(super) fn ready_root(target: &str) -> Result<bool, String> {
    let root = if drive_letter(target).is_some() {
        PathBuf::from(format!("{target}\\"))
    } else {
        PathBuf::from(target)
    };
    let present = if drive_letter(target).is_some() {
        drive_present(target)?
    } else {
        directory_mount_present(&root)?
    };
    if !present {
        return Ok(false);
    }
    // Directory access is on this bounded-wait thread, never on the frontend
    // or the control worker. A network provider need not supply volume metadata.
    let mut entries = match fs::read_dir(&root) {
        Ok(entries) => entries,
        Err(_) => return Ok(false),
    };
    match entries.next() {
        Some(Err(_)) => Ok(false),
        _ => Ok(true),
    }
}
pub(super) fn prepare(mount: &MountConfig) -> Result<Lease, String> {
    mount.validate()?;
    let drive = drive_letter(&mount.mount_point).is_some();
    let target = PathBuf::from(&mount.mount_point);
    if (mount.network_mode || mount.volname.starts_with("\\\\")) && !drive {
        return Err("Windows 网络模式只能挂载到明确盘符；请修改挂载点。".into());
    }
    if let Some(share) = share_name(mount) {
        ensure_share_free(&share)?;
    }
    if drive {
        if drive_present(&mount.mount_point)? {
            return Err("挂载盘符已被占用；不会接管、卸载或修改外部实例。".into());
        }
        return Ok(Lease {
            target,
            drive,
            _ancestors: Vec::new(),
            placeholder: None,
            restoration: None,
            removed: false,
            spawned: false,
        });
    }
    let target = absolute_local_directory(&target)?;
    let parent = target.parent().ok_or("挂载点父目录无效。")?;
    let mut ancestors = Vec::new();
    let mut path = PathBuf::new();
    for component in parent.components() {
        path.push(component);
        if matches!(component, Component::Prefix(_)) {
            continue;
        }
        ancestors.push(open_directory(&path, false)?);
    }
    let placeholder = match fs::symlink_metadata(&target) {
        Ok(metadata) => {
            if !metadata.is_dir() || metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0
            {
                return Err("目录挂载点不能是文件、符号链接或重解析点；请选择普通空目录。".into());
            }
            let file = open_directory(&target, true)?;
            let (identity, _) = information(&file)?;
            ensure_empty(&target)?;
            Some(Placeholder { file, identity })
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => None,
        Err(error) => return Err(io_message("检查目录挂载点", &error)),
    };
    Ok(Lease {
        target,
        drive,
        _ancestors: ancestors,
        placeholder,
        restoration: None,
        removed: false,
        spawned: false,
    })
}
fn absolute_local_directory(target: &Path) -> Result<PathBuf, String> {
    // Device namespace, UNC, dot navigation, and implicit drive roots can bypass
    // ordinary directory identity checks or trigger arbitrary remote probes.
    let mut components = target.components();
    match components.next() {
        Some(Component::Prefix(prefix)) if matches!(prefix.kind(), Prefix::Disk(_)) => {}
        _ => {
            return Err(
                "目录挂载点必须是本地盘符上的绝对普通目录；不支持 UNC、设备路径或自动盘符。".into(),
            )
        }
    }
    if !matches!(components.next(), Some(Component::RootDir)) {
        return Err("挂载点必须是明确盘符（如 X:）或本地绝对目录路径。".into());
    }
    let mut count = 0;
    for component in components {
        match component {
            Component::Normal(name) => {
                let name = name.to_string_lossy();
                let stem = name.split('.').next().unwrap_or("").to_ascii_uppercase();
                let reserved = matches!(stem.as_str(), "CON" | "PRN" | "AUX" | "NUL")
                    || (stem.len() == 4
                        && (stem.starts_with("COM") || stem.starts_with("LPT"))
                        && matches!(stem.as_bytes()[3], b'1'..=b'9'));
                if name.ends_with(['.', ' ']) || name.contains(':') || reserved {
                    return Err(
                        "挂载目录包含 Windows 设备名、流名称或有歧义的尾随字符；请选择普通目录。"
                            .into(),
                    );
                }
                count += 1;
            }
            _ => return Err("挂载目录不能包含父目录跳转或设备路径。".into()),
        }
    }
    if count == 0 {
        return Err("不能将现有磁盘根目录用作目录挂载点；请填写空目录或未使用的明确盘符。".into());
    }
    Ok(target.to_owned())
}
pub(super) fn share_name(mount: &MountConfig) -> Option<String> {
    let unc = if mount.volname.starts_with("\\\\") {
        mount.volname.replace('/', "\\")
    } else if mount.network_mode {
        if mount.volname.is_empty() {
            format!("\\\\rclone-tray\\{}", mount.id)
        } else {
            format!("\\\\server\\{}", mount.volname.replace('/', "\\"))
        }
    } else {
        return None;
    };
    // WinFsp accepts a suffix below the UNC share. It still occupies the same
    // server/share namespace, including for bare labels in network mode.
    Some(unc_share_root(&unc))
}
fn unc_share_root(unc: &str) -> String {
    let mut parts = unc.trim_start_matches('\\').split('\\');
    format!(
        "\\\\{}\\{}",
        parts.next().unwrap_or(""),
        parts.next().unwrap_or("")
    )
    .to_lowercase()
}
fn ensure_share_free(unc: &str) -> Result<(), String> {
    let parts: Vec<_> = unc.trim_start_matches('\\').split('\\').collect();
    if parts.len() != 2
        || parts
            .iter()
            .any(|part| part.is_empty() || part.contains([':', '/', '\0']))
    {
        return Err("网络卷名必须是明确的 \\\\server\\share，不能包含目录或设备路径。".into());
    }
    // Only the LOCAL server is queried. Never ask an arbitrary volname server
    // for SMB data, and never adopt or remove any discovered share.
    let name = wide(std::ffi::OsStr::new(parts[1]));
    let mut buffer = ptr::null_mut();
    let status = unsafe { NetShareGetInfo(ptr::null(), name.as_ptr(), 0, &mut buffer) };
    if !buffer.is_null() {
        unsafe {
            NetApiBufferFree(buffer as *const _);
        }
    }
    if status == 0 {
        return Err("网络共享名与本机现有共享冲突；不会修改外部共享。".into());
    }
    // NERR_NetNameNotFound = 2310. Other failures are not evidence of vacancy.
    if status != 2310 && status != ERROR_BAD_NET_NAME {
        return Err(format!(
            "无法安全确认本机共享名是否占用（系统错误 {status}）；未创建挂载进程。"
        ));
    }
    let mask = unsafe { GetLogicalDrives() };
    if mask == 0 {
        return Err(io_message("检查本地盘符映射", &io::Error::last_os_error()));
    }
    for index in 0..26 {
        if mask & (1 << index) == 0 {
            continue;
        }
        let root = [b'A' as u16 + index as u16, b':' as u16, b'\\' as u16, 0];
        // Local connection metadata only; no WNetEnumResource/network browsing.
        // Alignment is that of usize, because UNIVERSAL_NAME_INFOW contains a pointer.
        let mut buffer = vec![0usize; 8192];
        let mut size = (buffer.len() * std::mem::size_of::<usize>()) as u32;
        let result = unsafe {
            WNetGetUniversalNameW(
                root.as_ptr(),
                UNIVERSAL_NAME_INFO_LEVEL,
                buffer.as_mut_ptr() as *mut _,
                &mut size,
            )
        };
        if result == 0 {
            let info = unsafe { &*(buffer.as_ptr() as *const UNIVERSAL_NAME_INFOW) };
            let start = buffer.as_ptr() as usize;
            let end = start + buffer.len() * std::mem::size_of::<usize>();
            let pointer = info.lpUniversalName as usize;
            if pointer < start || pointer >= end || pointer % 2 != 0 {
                return Err("本地网络盘符映射返回无效名称。".into());
            }
            let maximum = (end - pointer) / 2;
            let text = unsafe { std::slice::from_raw_parts(info.lpUniversalName, maximum) };
            let length = text
                .iter()
                .position(|value| *value == 0)
                .ok_or("网络映射名称未终止。")?;
            let mapping =
                String::from_utf16(&text[..length]).map_err(|_| "网络映射名称编码无效。")?;
            if unc_share_root(&mapping.replace('/', "\\")) == unc {
                return Err(
                    "网络卷名与本机已映射的外部网络挂载冲突；不会接管或卸载该实例。".into(),
                );
            }
        } else if !matches!(result, 2250 | 1200 | 50 | 1203) {
            // NOT_CONNECTED, BAD_DEVICE, NOT_SUPPORTED, NO_NET_OR_BAD_PATH:
            // local disks have no universal network name. Anything else is
            // uncertain and therefore cannot establish safe share vacancy.
            return Err(format!(
                "无法确认本地网络盘符映射（系统错误 {result}）；未创建挂载进程。"
            ));
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    fn unique_directory() -> PathBuf {
        let mut random = [0u8; 12];
        getrandom::getrandom(&mut random).unwrap();
        let suffix: String = random.iter().map(|byte| format!("{byte:02x}")).collect();
        let path = std::env::temp_dir().join(format!("rclone-runtime-safety-{suffix}"));
        fs::create_dir(&path).unwrap();
        path
    }
    fn mount(path: &Path) -> MountConfig {
        MountConfig::new(
            "boundary".into(),
            "sample:".into(),
            path.to_string_lossy().into_owned(),
        )
        .unwrap()
    }
    #[test]
    fn nonrecursive_removal_preserves_concurrent_new_file() {
        let base = unique_directory();
        let path = base.join("placeholder");
        fs::create_dir(&path).unwrap();
        let mut lease = prepare(&mount(&path)).unwrap();
        fs::write(path.join("external.txt"), b"user data").unwrap();
        assert!(lease.remove_placeholder().is_err());
        assert_eq!(fs::read(path.join("external.txt")).unwrap(), b"user data");
        drop(lease);
        fs::remove_file(path.join("external.txt")).unwrap();
        fs::remove_dir(path).unwrap();
        fs::remove_dir(base).unwrap();
    }
    #[test]
    fn cleanup_never_adopts_replacement_and_retains_restoration_obligation() {
        let base = unique_directory();
        let path = base.join("placeholder");
        fs::create_dir(&path).unwrap();
        let mut lease = prepare(&mount(&path)).unwrap();
        lease.remove_placeholder().unwrap();
        assert!(!path.exists());
        fs::create_dir(&path).unwrap();
        fs::write(path.join("external.txt"), b"preserve").unwrap();
        assert!(lease.cleanup().is_err());
        assert!(lease.removed);
        assert_eq!(fs::read(path.join("external.txt")).unwrap(), b"preserve");
        fs::remove_file(path.join("external.txt")).unwrap();
        fs::remove_dir(&path).unwrap();
        lease.cleanup().unwrap();
        assert!(!lease.removed);
        assert!(path.is_dir());
        drop(lease);
        fs::remove_dir(path).unwrap();
        fs::remove_dir(base).unwrap();
    }
    #[test]
    fn placeholder_identity_is_pinned_against_replacement() {
        let base = unique_directory();
        let path = base.join("placeholder");
        fs::create_dir(&path).unwrap();
        let lease = prepare(&mount(&path)).unwrap();
        assert!(fs::rename(&path, base.join("external-replacement")).is_err());
        assert!(fs::remove_dir(&path).is_err());
        drop(lease);
        fs::remove_dir(path).unwrap();
        fs::remove_dir(base).unwrap();
    }
    #[test]
    fn network_labels_and_unc_suffixes_share_one_collision_namespace() {
        let mut mount = MountConfig::new("network".into(), "sample:".into(), "Y:".into()).unwrap();
        mount.network_mode = true;
        mount.volname = "Projects".into();
        assert_eq!(share_name(&mount).as_deref(), Some("\\\\server\\projects"));
        mount.volname = "\\\\SERVER\\Projects\\subdirectory".into();
        assert_eq!(share_name(&mount).as_deref(), Some("\\\\server\\projects"));
        mount.network_mode = false;
        assert_eq!(share_name(&mount).as_deref(), Some("\\\\server\\projects"));
    }
    #[test]
    fn rejects_device_unc_root_and_ambiguous_directory_paths() {
        for path in [
            "C:\\",
            "C:relative",
            "\\\\server\\share\\folder",
            "\\\\?\\C:\\folder",
            "C:\\a\\..\\b",
            "C:\\a\\NUL",
            "C:\\a\\b.",
            "C:\\a\\file:stream",
        ] {
            assert!(absolute_local_directory(Path::new(path)).is_err(), "{path}");
        }
        assert!(absolute_local_directory(Path::new("C:\\ordinary\\folder")).is_ok());
    }
}
