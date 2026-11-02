//! Native per-user startup registration. No registry operation is performed at module initialization.
use std::path::Path;

pub const AUTOSTART_FLAG: &str = "--autostart";
const RUN_KEY: &str = "Software\\Microsoft\\Windows\\CurrentVersion\\Run";
const VALUE_NAME: &str = "rclone_tray";

pub fn read_entry() -> Result<Option<String>, String> {
    platform::read_user_string(RUN_KEY, VALUE_NAME)
}
pub fn is_enabled() -> Result<bool, String> {
    Ok(read_entry()?.is_some())
}
pub fn enable(exe: &Path) -> Result<(), String> {
    let command = startup_command(exe)?;
    // Refuse unsupported preexisting data before mutation: Option<String> cannot restore its type.
    read_entry()?;
    platform::write_user_string(RUN_KEY, VALUE_NAME, Some(&command))?;
    verify(Some(&command))
}
pub fn disable() -> Result<(), String> {
    read_entry()?;
    platform::write_user_string(RUN_KEY, VALUE_NAME, None)?;
    verify(None)
}
pub fn restore(entry: Option<&str>) -> Result<(), String> {
    if let Some(value) = entry {
        validate_registry_string(value)?;
    }
    if read_entry()?.as_deref() == entry {
        return Ok(());
    }
    platform::write_user_string(RUN_KEY, VALUE_NAME, entry)?;
    verify(entry)
}
fn verify(expected: Option<&str>) -> Result<(), String> {
    if read_entry()?.as_deref() == expected {
        Ok(())
    } else {
        Err("自启动注册表回读与写入不一致；请恢复此前设置。".into())
    }
}
fn validate_registry_string(value: &str) -> Result<(), String> {
    if value.contains('\0') {
        Err("自启动注册表内容含无效字符；未覆盖。".into())
    } else {
        Ok(())
    }
}
fn startup_command(exe: &Path) -> Result<String, String> {
    let absolute = exe
        .canonicalize()
        .map_err(|e| crate::config::io_error("检查自启动可执行文件", &e))?;
    if !absolute.is_file() {
        return Err("自启动目标不是可执行文件；未写入注册表。".into());
    }
    let path = absolute.to_str().ok_or("自启动路径含无效 Unicode 字符。")?;
    #[cfg(windows)]
    let path = path
        .strip_prefix("\\\\?\\UNC\\")
        .map(|p| format!("\\\\{p}"))
        .unwrap_or_else(|| path.strip_prefix("\\\\?\\").unwrap_or(path).to_string());
    #[cfg(not(windows))]
    let path = path.to_string();
    validate_registry_string(&path)?;
    if path.contains('"') {
        return Err("自启动可执行文件路径含无效引号。".into());
    }
    Ok(format!("\"{path}\" {AUTOSTART_FLAG}"))
}

pub(crate) fn winfsp_install_dir(view: u32) -> Result<Option<String>, String> {
    platform::read_machine_string("SOFTWARE\\WinFsp", "InstallDir", view)
}

#[cfg(windows)]
mod platform {
    use std::ffi::c_void;
    use std::ptr;
    type Hkey = *mut c_void;
    const KEY_QUERY_VALUE: u32 = 0x0001;
    const KEY_SET_VALUE: u32 = 0x0002;
    const REG_SZ: u32 = 1;
    const REG_EXPAND_SZ: u32 = 2;
    const NOT_FOUND: u32 = 2;
    const MORE_DATA: u32 = 234;
    const HKCU: Hkey = 0x80000001u32 as i32 as isize as Hkey;
    const HKLM: Hkey = 0x80000002u32 as i32 as isize as Hkey;
    #[link(name = "advapi32")]
    extern "system" {
        fn RegOpenKeyExW(
            key: Hkey,
            subkey: *const u16,
            options: u32,
            access: u32,
            result: *mut Hkey,
        ) -> u32;
        fn RegCreateKeyExW(
            key: Hkey,
            subkey: *const u16,
            reserved: u32,
            class: *mut u16,
            options: u32,
            access: u32,
            security: *const c_void,
            result: *mut Hkey,
            disposition: *mut u32,
        ) -> u32;
        fn RegQueryValueExW(
            key: Hkey,
            name: *const u16,
            reserved: *mut u32,
            kind: *mut u32,
            data: *mut u8,
            length: *mut u32,
        ) -> u32;
        fn RegSetValueExW(
            key: Hkey,
            name: *const u16,
            reserved: u32,
            kind: u32,
            data: *const u8,
            length: u32,
        ) -> u32;
        fn RegDeleteValueW(key: Hkey, name: *const u16) -> u32;
        fn RegCloseKey(key: Hkey) -> u32;
    }
    struct Key(Hkey);
    impl Drop for Key {
        fn drop(&mut self) {
            unsafe {
                RegCloseKey(self.0);
            }
        }
    }
    fn wide(value: &str) -> Vec<u16> {
        value.encode_utf16().chain(Some(0)).collect()
    }
    fn error(action: &str, code: u32) -> String {
        format!("{action}失败（注册表系统错误 {code}）。未静默重置自启动设置。")
    }
    fn open(root: Hkey, subkey: &str, access: u32) -> Result<Option<Key>, String> {
        let subkey = wide(subkey);
        let mut key = ptr::null_mut();
        let result = unsafe { RegOpenKeyExW(root, subkey.as_ptr(), 0, access, &mut key) };
        match result {
            0 => Ok(Some(Key(key))),
            NOT_FOUND => Ok(None),
            code => Err(error("读取注册表项", code)),
        }
    }
    fn query(
        root: Hkey,
        subkey: &str,
        name: &str,
        view: u32,
        allow_expand: bool,
    ) -> Result<Option<String>, String> {
        let Some(key) = open(root, subkey, KEY_QUERY_VALUE | view)? else {
            return Ok(None);
        };
        let name = wide(name);
        // Query size and bytes with a bounded retry for a value changing concurrently.
        for _ in 0..3 {
            let mut kind = 0;
            let mut length = 0;
            let result = unsafe {
                RegQueryValueExW(
                    key.0,
                    name.as_ptr(),
                    ptr::null_mut(),
                    &mut kind,
                    ptr::null_mut(),
                    &mut length,
                )
            };
            if result == NOT_FOUND {
                return Ok(None);
            }
            if result != 0 {
                return Err(error("读取注册表值大小", result));
            }
            if kind != REG_SZ && !(allow_expand && kind == REG_EXPAND_SZ) {
                return Err(
                    "自启动或安装目录注册表值不是支持的字符串类型；为保留原值，未覆盖。".into(),
                );
            }
            if length > 1024 * 1024 || length % 2 != 0 {
                return Err("注册表字符串数据损坏或过长；未覆盖。".into());
            }
            let mut units = vec![0u16; (length as usize / 2).max(1)];
            let result = unsafe {
                RegQueryValueExW(
                    key.0,
                    name.as_ptr(),
                    ptr::null_mut(),
                    &mut kind,
                    units.as_mut_ptr().cast(),
                    &mut length,
                )
            };
            if result == MORE_DATA {
                continue;
            }
            if result == NOT_FOUND {
                return Ok(None);
            }
            if result != 0 {
                return Err(error("读取注册表值", result));
            }
            if kind != REG_SZ && !(allow_expand && kind == REG_EXPAND_SZ) {
                return Err("注册表值类型在读取期间发生变化；未覆盖。".into());
            }
            if length % 2 != 0 || length as usize / 2 > units.len() {
                return Err("注册表字符串数据损坏；未覆盖。".into());
            }
            units.truncate(length as usize / 2);
            if units.last() == Some(&0) {
                units.pop();
            }
            if units.contains(&0) {
                return Err("注册表字符串含无效字符；未覆盖。".into());
            }
            return String::from_utf16(&units)
                .map(Some)
                .map_err(|_| "注册表字符串含无效 Unicode 字符；未覆盖。".into());
        }
        Err("注册表值在读取期间持续变化；请稍后重试。".into())
    }
    pub(super) fn read_user_string(subkey: &str, name: &str) -> Result<Option<String>, String> {
        query(HKCU, subkey, name, 0, false)
    }
    pub(super) fn read_machine_string(
        subkey: &str,
        name: &str,
        view: u32,
    ) -> Result<Option<String>, String> {
        query(HKLM, subkey, name, view, true)
    }
    pub(super) fn write_user_string(
        subkey: &str,
        name: &str,
        value: Option<&str>,
    ) -> Result<(), String> {
        let key = if value.is_some() {
            let subkey = wide(subkey);
            let mut key = ptr::null_mut();
            let result = unsafe {
                RegCreateKeyExW(
                    HKCU,
                    subkey.as_ptr(),
                    0,
                    ptr::null_mut(),
                    0,
                    KEY_SET_VALUE,
                    ptr::null(),
                    &mut key,
                    ptr::null_mut(),
                )
            };
            if result != 0 {
                return Err(error("打开自启动注册表写入项", result));
            }
            Key(key)
        } else {
            let Some(key) = open(HKCU, subkey, KEY_SET_VALUE)? else {
                return Ok(());
            };
            key
        };
        let name = wide(name);
        let result = if let Some(value) = value {
            let units = wide(value);
            let length = u32::try_from(units.len() * 2)
                .map_err(|_| "自启动注册表字符串过长。".to_string())?;
            unsafe {
                RegSetValueExW(
                    key.0,
                    name.as_ptr(),
                    0,
                    REG_SZ,
                    units.as_ptr().cast(),
                    length,
                )
            }
        } else {
            unsafe { RegDeleteValueW(key.0, name.as_ptr()) }
        };
        if result == 0 || (value.is_none() && result == NOT_FOUND) {
            Ok(())
        } else {
            Err(error("写入自启动注册表值", result))
        }
    }
}
#[cfg(not(windows))]
mod platform {
    fn unsupported<T>() -> Result<T, String> {
        Err("自启动注册表操作仅支持 Windows。".into())
    }
    pub(super) fn read_user_string(_: &str, _: &str) -> Result<Option<String>, String> {
        unsupported()
    }
    pub(super) fn read_machine_string(_: &str, _: &str, _: u32) -> Result<Option<String>, String> {
        unsupported()
    }
    pub(super) fn write_user_string(_: &str, _: &str, _: Option<&str>) -> Result<(), String> {
        unsupported()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn invalid_executable_and_invalid_registry_string_are_rejected_without_mutation() {
        assert!(startup_command(Path::new("\0")).is_err());
        assert!(validate_registry_string("old\0hidden-command").is_err());
        assert!(validate_registry_string("\"C:\\Program Files\\app.exe\" --autostart").is_ok());
    }
}
