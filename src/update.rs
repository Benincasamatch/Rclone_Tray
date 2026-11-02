//! GitHub Releases update check over WinHTTP. Read-only: never downloads or
//! replaces the executable; the UI offers the release download link instead.
use std::ffi::c_void;
use std::ptr::{null, null_mut};

use windows_sys::Win32::Foundation::GetLastError;
use windows_sys::Win32::Networking::WinHttp::*;

use crate::version::{self, Version};

pub const RELEASES_API: &str =
    "https://api.github.com/repos/Benincasamatch/Rclone_Tray/releases?per_page=10";
pub const RELEASES_PAGE: &str = "https://github.com/Benincasamatch/Rclone_Tray/releases";
const ASSET_PREFIX: &str = "https://github.com/Benincasamatch/Rclone_Tray/releases/download/";
const MAX_RESPONSE: usize = 2 * 1024 * 1024;
const MAX_NOTES: usize = 1200;
const CONNECT_TIMEOUT_MS: i32 = 4_000;
const SEND_TIMEOUT_MS: i32 = 4_000;
const RECEIVE_TIMEOUT_MS: i32 = 8_000;

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum UpdateState {
    #[default]
    Idle,
    Checking,
    UpToDate,
    Available,
    Failed,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct UpdateInfo {
    pub state: UpdateState,
    pub current: String,
    pub latest: String,
    pub name: String,
    pub notes: String,
    /// Release page; the only URL the UI may open when no asset is published.
    pub page_url: String,
    /// Installer/executable asset when published, otherwise the release page.
    pub download_url: String,
    pub asset_name: String,
    pub asset_bytes: u64,
    pub published_at: String,
    pub checked_at: String,
    pub message: String,
}

impl Default for UpdateInfo {
    fn default() -> Self {
        Self {
            state: UpdateState::Idle,
            current: version::CURRENT.into(),
            latest: String::new(),
            name: String::new(),
            notes: String::new(),
            page_url: RELEASES_PAGE.into(),
            download_url: String::new(),
            asset_name: String::new(),
            asset_bytes: 0,
            published_at: String::new(),
            checked_at: String::new(),
            message: "尚未检查更新。".into(),
        }
    }
}

/// Reviews published non-draft, non-prerelease releases and reports the newest.
pub fn check(current: &str) -> Result<UpdateInfo, String> {
    let current = Version::parse(current)?;
    let (status, body) = http_get(RELEASES_API)?;
    match status {
        200 => parse_releases(&body, &current, &version::now_stamp()),
        403 | 429 => Err("GitHub API 请求受限（未认证每小时 60 次）；请稍后重试。".into()),
        other => Err(format!("GitHub 返回 HTTP {other}；无法读取版本清单。")),
    }
}

fn parse_releases(bytes: &[u8], current: &Version, checked_at: &str) -> Result<UpdateInfo, String> {
    let root: serde_json::Value =
        serde_json::from_slice(bytes).map_err(|_| "GitHub 版本清单不是有效 JSON。".to_string())?;
    let releases = root
        .as_array()
        .ok_or_else(|| "GitHub 版本清单格式无效。".to_string())?;
    struct Release {
        version: Version,
        name: String,
        notes: String,
        page: String,
        published: String,
        asset_name: String,
        asset_url: String,
        asset_bytes: u64,
    }
    let mut newest: Option<Release> = None;
    for release in releases {
        if release.get("draft").and_then(|v| v.as_bool()) == Some(true)
            || release.get("prerelease").and_then(|v| v.as_bool()) == Some(true)
        {
            continue;
        }
        let Some(tag) = release.get("tag_name").and_then(|v| v.as_str()) else {
            continue;
        };
        let Ok(parsed) = Version::parse(tag) else {
            continue;
        };
        if newest
            .as_ref()
            .is_some_and(|existing| parsed <= existing.version)
        {
            continue;
        }
        let page = release
            .get("html_url")
            .and_then(|v| v.as_str())
            .filter(|url| url.starts_with("https://github.com/"))
            .unwrap_or(RELEASES_PAGE)
            .to_string();
        let published = release
            .get("published_at")
            .and_then(|v| v.as_str())
            .unwrap_or_default()
            .to_string();
        let notes = release
            .get("body")
            .and_then(|v| v.as_str())
            .unwrap_or_default()
            .trim()
            .chars()
            .take(MAX_NOTES)
            .collect::<String>();
        let mut asset_name = String::new();
        let mut asset_url = String::new();
        let mut asset_bytes = 0;
        if let Some(assets) = release.get("assets").and_then(|v| v.as_array()) {
            let mut candidates = assets.iter().filter_map(|asset| {
                let name = asset.get("name").and_then(|v| v.as_str())?;
                let url = asset.get("browser_download_url").and_then(|v| v.as_str())?;
                (url.starts_with(ASSET_PREFIX) && name.to_ascii_lowercase().ends_with(".exe"))
                    .then(|| {
                        (
                            name,
                            url,
                            asset.get("size").and_then(|v| v.as_u64()).unwrap_or(0),
                        )
                    })
            });
            // Prefer the installer, then any rclone_tray executable.
            let preferred = candidates
                .find(|(name, _, _)| name.eq_ignore_ascii_case("rclone_tray_setup.exe"))
                .or_else(|| {
                    assets.iter().find_map(|asset| {
                        let name = asset.get("name")?.as_str()?;
                        let url = asset.get("browser_download_url")?.as_str()?;
                        (url.starts_with(ASSET_PREFIX)
                            && name.eq_ignore_ascii_case("rclone_tray.exe"))
                        .then(|| {
                            (
                                name,
                                url,
                                asset.get("size").and_then(|v| v.as_u64()).unwrap_or(0),
                            )
                        })
                    })
                });
            if let Some((name, url, size)) = preferred {
                asset_name = name.to_string();
                asset_url = url.to_string();
                asset_bytes = size;
            }
        }
        newest = Some(Release {
            version: parsed,
            name: release
                .get("name")
                .and_then(|v| v.as_str())
                .unwrap_or_default()
                .to_string(),
            notes,
            page,
            published,
            asset_name,
            asset_url,
            asset_bytes,
        });
    }
    let Some(newest) = newest else {
        return Err("GitHub 未发布任何可用的正式版本。".into());
    };
    let available = newest.version > *current;
    let latest = newest.version.to_string();
    let mut info = UpdateInfo {
        state: if available {
            UpdateState::Available
        } else {
            UpdateState::UpToDate
        },
        current: current.to_string(),
        latest: latest.clone(),
        name: newest.name,
        notes: newest.notes,
        page_url: newest.page.clone(),
        download_url: if available && !newest.asset_url.is_empty() {
            newest.asset_url.clone()
        } else {
            newest.page.clone()
        },
        asset_name: newest.asset_name,
        asset_bytes: newest.asset_bytes,
        published_at: newest.published,
        checked_at: checked_at.to_string(),
        message: String::new(),
    };
    info.message = if available {
        format!("发现新版本 {latest}（当前 {}）。", info.current)
    } else if newest.version < *current {
        format!("本地版本 {} 高于最新发布 {latest}。", info.current)
    } else {
        format!("已是最新版本 {latest}。")
    };
    Ok(info)
}

fn wide(text: &str) -> Vec<u16> {
    text.encode_utf16().chain(std::iter::once(0)).collect()
}

struct Internet(*mut c_void);
impl Drop for Internet {
    fn drop(&mut self) {
        if !self.0.is_null() {
            unsafe {
                WinHttpCloseHandle(self.0);
            }
        }
    }
}

fn last_error(action: &str) -> String {
    format!("{action}失败（Windows 错误 {}）。", unsafe { GetLastError() })
}

fn split_url(url: &str) -> Result<(bool, String, u16, String), String> {
    let (secure, rest) = if let Some(rest) = url.strip_prefix("https://") {
        (true, rest)
    } else if let Some(rest) = url.strip_prefix("http://") {
        (false, rest)
    } else {
        return Err("更新地址必须使用 http 或 https。".into());
    };
    if rest.contains('@') || rest.contains('#') {
        return Err("更新地址包含不允许的字符。".into());
    }
    let (authority, path) = match rest.split_once('/') {
        Some((authority, path)) => (authority, format!("/{path}")),
        None => (rest, "/".to_string()),
    };
    let (host, port) = match authority.rsplit_once(':') {
        Some((host, port)) => (
            host,
            port.parse::<u16>()
                .map_err(|_| "更新地址端口无效。".to_string())?,
        ),
        None => (authority, if secure { 443 } else { 80 }),
    };
    if host.is_empty()
        || host.len() > 253
        || !host
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'.'))
        || !path.bytes().all(|byte| byte.is_ascii_graphic())
    {
        return Err("更新地址格式无效。".into());
    }
    Ok((secure, host.to_string(), port, path))
}

/// Minimal HTTP GET with bounded time and body; used for the JSON release list.
fn http_get(url: &str) -> Result<(u32, Vec<u8>), String> {
    let (secure, host, port, path) = split_url(url)?;
    let agent = wide(&format!("rclone_tray/{}", version::CURRENT));
    let session = Internet(unsafe {
        WinHttpOpen(
            agent.as_ptr(),
            WINHTTP_ACCESS_TYPE_DEFAULT_PROXY,
            null(),
            null(),
            0,
        )
    });
    if session.0.is_null() {
        return Err(last_error("初始化 WinHTTP"));
    }
    unsafe {
        WinHttpSetTimeouts(
            session.0,
            CONNECT_TIMEOUT_MS,
            CONNECT_TIMEOUT_MS,
            SEND_TIMEOUT_MS,
            RECEIVE_TIMEOUT_MS,
        );
    }
    let host = wide(&host);
    let connect = Internet(unsafe {
        WinHttpConnect(session.0, host.as_ptr(), port, 0)
    });
    if connect.0.is_null() {
        return Err(last_error("连接 GitHub"));
    }
    let verb = wide("GET");
    let target = wide(&path);
    let request = Internet(unsafe {
        WinHttpOpenRequest(
            connect.0,
            verb.as_ptr(),
            target.as_ptr(),
            null(),
            null(),
            null(),
            if secure { WINHTTP_FLAG_SECURE } else { 0 },
        )
    });
    if request.0.is_null() {
        return Err(last_error("创建更新请求"));
    }
    let headers = wide("Accept: application/vnd.github+json\r\n");
    if unsafe {
        WinHttpSendRequest(
            request.0,
            headers.as_ptr(),
            headers.len() as u32 - 1,
            null(),
            0,
            0,
            0,
        )
    } == 0
    {
        return Err(last_error("发送更新请求"));
    }
    if unsafe { WinHttpReceiveResponse(request.0, null_mut()) } == 0 {
        return Err(last_error("接收更新响应"));
    }
    let mut status = 0u32;
    let mut length = std::mem::size_of::<u32>() as u32;
    if unsafe {
        WinHttpQueryHeaders(
            request.0,
            WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER,
            null(),
            (&mut status as *mut u32).cast::<c_void>(),
            &mut length,
            null_mut(),
        )
    } == 0
    {
        return Err(last_error("读取更新响应状态"));
    }
    let mut body = Vec::new();
    loop {
        let mut available = 0u32;
        if unsafe { WinHttpQueryDataAvailable(request.0, &mut available) } == 0 {
            return Err(last_error("读取更新响应长度"));
        }
        if available == 0 {
            break;
        }
        let mut buffer = [0u8; 8 * 1024];
        let mut read = 0u32;
        let limit = (buffer.len() as u32).min(available);
        if unsafe {
            WinHttpReadData(
                request.0,
                buffer.as_mut_ptr().cast::<c_void>(),
                limit,
                &mut read,
            )
        } == 0
        {
            return Err(last_error("读取更新响应"));
        }
        if read == 0 {
            break;
        }
        if body.len() + read as usize > MAX_RESPONSE {
            return Err("GitHub 版本清单过大；检查已中止。".into());
        }
        body.extend_from_slice(&buffer[..read as usize]);
    }
    Ok((status, body))
}

#[cfg(test)]
mod tests {
    use super::*;

    const FIXTURE: &str = r#"[
      {"tag_name":"v2.0.0","name":"legacy","body":"old","html_url":"https://github.com/Benincasamatch/Rclone_Tray/releases/tag/v2.0.0","draft":false,"prerelease":false,"published_at":"2026-11-01T09:10:18Z","assets":[{"name":"rclone_tray.exe","size":47746152,"browser_download_url":"https://github.com/Benincasamatch/Rclone_Tray/releases/download/v2.0.0/rclone_tray.exe"}]},
      {"tag_name":"v2.1.0","name":"2.1.0","body":"  - 更新检查\n  - 安装程序  ","html_url":"https://github.com/Benincasamatch/Rclone_Tray/releases/tag/v2.1.0","draft":false,"prerelease":false,"published_at":"2026-12-01T00:00:00Z","assets":[{"name":"rclone_tray.exe","size":10,"browser_download_url":"https://github.com/Benincasamatch/Rclone_Tray/releases/download/v2.1.0/rclone_tray.exe"},{"name":"rclone_tray_setup.exe","size":2048,"browser_download_url":"https://github.com/Benincasamatch/Rclone_Tray/releases/download/v2.1.0/rclone_tray_setup.exe"}]},
      {"tag_name":"v2.2.0-rc.1","name":"rc","body":"","html_url":"https://github.com/Benincasamatch/Rclone_Tray/releases/tag/v2.2.0-rc.1","draft":false,"prerelease":true,"published_at":"2026-12-02T00:00:00Z","assets":[]},
      {"tag_name":"v9.0.0","name":"draft","body":"","html_url":"https://github.com/Benincasamatch/Rclone_Tray/releases/tag/v9.0.0","draft":true,"prerelease":false,"published_at":"","assets":[]}
    ]"#;

    #[test]
    fn picks_newest_stable_release_and_installer_asset() {
        let info = parse_releases(FIXTURE.as_bytes(), &Version::parse("2.0.0").unwrap(), "now")
            .unwrap();
        assert_eq!(info.state, UpdateState::Available);
        assert_eq!(info.latest, "2.1.0");
        assert_eq!(info.asset_name, "rclone_tray_setup.exe");
        assert_eq!(info.asset_bytes, 2048);
        assert!(info.download_url.ends_with("/v2.1.0/rclone_tray_setup.exe"));
        assert_eq!(info.notes, "- 更新检查\n  - 安装程序");
        assert_eq!(info.checked_at, "now");
    }

    #[test]
    fn reports_up_to_date_and_ignores_foreign_asset_urls() {
        let current = Version::parse("2.1.0").unwrap();
        let info = parse_releases(FIXTURE.as_bytes(), &current, "now").unwrap();
        assert_eq!(info.state, UpdateState::UpToDate);
        assert_eq!(info.latest, "2.1.0");
        assert_eq!(info.download_url, info.page_url);
        assert!(info.message.contains("已是最新版本"));
    }

    #[test]
    fn rejects_unknown_payloads_and_urls() {
        assert!(parse_releases(b"{}", &Version::parse("1.0.0").unwrap(), "now").is_err());
        assert!(parse_releases(b"[]", &Version::parse("1.0.0").unwrap(), "now").is_err());
        assert!(split_url("https://user@host/").is_err());
        assert!(split_url("ftp://host/").is_err());
        assert!(split_url("https://host/path with space").is_err());
        assert_eq!(
            split_url("https://api.github.com/repos/x?y=1").unwrap(),
            (true, "api.github.com".into(), 443, "/repos/x?y=1".into())
        );
    }

    #[test]
    fn http_get_reads_a_local_response() {
        use std::io::{Read, Write};
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let server = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut buffer = [0u8; 2048];
            let _ = stream.read(&mut buffer);
            let body = br#"{"ok":true}"#;
            write!(
                stream,
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                body.len()
            )
            .unwrap();
            stream.write_all(body).unwrap();
        });
        let (status, body) =
            http_get(&format!("http://127.0.0.1:{}/releases", address.port())).unwrap();
        assert_eq!(status, 200);
        assert_eq!(body, br#"{"ok":true}"#);
        server.join().unwrap();
    }
}
