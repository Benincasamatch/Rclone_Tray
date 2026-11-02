//! Semantic version parsing plus build provenance embedded by `build.rs`.
use std::cmp::Ordering;

/// Cargo package version, e.g. `2.0.0`.
pub const CURRENT: &str = env!("CARGO_PKG_VERSION");
pub const BUILD_COMMIT: &str = env!("RT_BUILD_COMMIT");
pub const BUILD_BRANCH: &str = env!("RT_BUILD_BRANCH");
pub const BUILD_DIRTY: bool = matches!(env!("RT_BUILD_DIRTY").as_bytes(), b"1");

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Version {
    pub major: u32,
    pub minor: u32,
    pub patch: u32,
    /// Dot-separated pre-release identifiers; empty for a final release.
    pre: Vec<String>,
}

impl Version {
    /// Accepts `2`, `2.1`, `v2.1.0`, `2.1.0-beta.1`; build metadata is ignored.
    pub fn parse(text: &str) -> Result<Self, String> {
        let text = text.trim();
        let text = text
            .strip_prefix('v')
            .or_else(|| text.strip_prefix('V'))
            .unwrap_or(text);
        if text.is_empty() || text.len() > 64 {
            return Err("版本号为空或过长。".into());
        }
        let text = text.split_once('+').map_or(text, |(core, _)| core);
        let (core, pre) = match text.split_once('-') {
            Some((core, pre)) if !pre.is_empty() => (core, pre),
            Some(_) => return Err("版本预览标识为空。".into()),
            None => (text, ""),
        };
        let mut numbers = core.split('.');
        let mut part = || -> Result<u32, String> {
            numbers
                .next()
                .ok_or_else(|| "版本号缺少数字段。".to_string())?
                .parse::<u32>()
                .map_err(|_| "版本号数字段无效。".to_string())
        };
        let major = part()?;
        let minor = numbers.next().map_or(Ok(0), |value| {
            value.parse::<u32>().map_err(|_| "版本号数字段无效。".to_string())
        })?;
        let patch = numbers.next().map_or(Ok(0), |value| {
            value.parse::<u32>().map_err(|_| "版本号数字段无效。".to_string())
        })?;
        if numbers.next().is_some() {
            return Err("版本号只能包含主、次、修订三个数字段。".into());
        }
        let pre = if pre.is_empty() {
            Vec::new()
        } else {
            pre.split('.')
                .map(|identifier| {
                    if identifier.is_empty()
                        || !identifier
                            .bytes()
                            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-')
                    {
                        return Err("版本预览标识无效。".to_string());
                    }
                    Ok(identifier.to_string())
                })
                .collect::<Result<Vec<_>, _>>()?
        };
        Ok(Self {
            major,
            minor,
            patch,
            pre,
        })
    }

    pub fn is_prerelease(&self) -> bool {
        !self.pre.is_empty()
    }

    pub fn is_newer_than(&self, other: &Self) -> bool {
        self > other
    }
}

impl std::fmt::Display for Version {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}.{}.{}", self.major, self.minor, self.patch)?;
        if !self.pre.is_empty() {
            write!(f, "-{}", self.pre.join("."))?;
        }
        Ok(())
    }
}

impl PartialOrd for Version {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for Version {
    fn cmp(&self, other: &Self) -> Ordering {
        (self.major, self.minor, self.patch)
            .cmp(&(other.major, other.minor, other.patch))
            .then_with(|| match (self.pre.is_empty(), other.pre.is_empty()) {
                (true, true) => Ordering::Equal,
                // A pre-release sorts before the corresponding final release.
                (true, false) => Ordering::Greater,
                (false, true) => Ordering::Less,
                (false, false) => compare_prerelease(&self.pre, &other.pre),
            })
    }
}

fn compare_prerelease(left: &[String], right: &[String]) -> Ordering {
    for (a, b) in left.iter().zip(right) {
        let ordering = match (a.parse::<u64>(), b.parse::<u64>()) {
            (Ok(a), Ok(b)) => a.cmp(&b),
            (Ok(_), Err(_)) => Ordering::Less,
            (Err(_), Ok(_)) => Ordering::Greater,
            (Err(_), Err(_)) => a.cmp(b),
        };
        if ordering != Ordering::Equal {
            return ordering;
        }
    }
    left.len().cmp(&right.len())
}

fn civil_from_days(days: i64) -> (i64, u32, u32) {
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = (z - era * 146_097) as u64;
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096) / 365;
    let year = yoe as i64 + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let month = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    (if month <= 2 { year + 1 } else { year }, month, day)
}

/// Formats a Unix timestamp as `YYYY-MM-DD HH:MM UTC` without a time crate.
pub fn utc_stamp(epoch_seconds: u64) -> String {
    let seconds = epoch_seconds.min(i64::MAX as u64) as i64;
    let days = seconds.div_euclid(86_400);
    let remainder = seconds.rem_euclid(86_400);
    let (year, month, day) = civil_from_days(days);
    format!(
        "{year:04}-{month:02}-{day:02} {:02}:{:02} UTC",
        remainder / 3_600,
        (remainder % 3_600) / 60
    )
}

pub fn build_epoch() -> u64 {
    env!("RT_BUILD_EPOCH").parse().unwrap_or_default()
}

pub fn build_stamp() -> String {
    utc_stamp(build_epoch())
}

pub fn now_stamp() -> String {
    let epoch = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|elapsed| elapsed.as_secs())
        .unwrap_or_default();
    utc_stamp(epoch)
}

/// One-line provenance for diagnostics and the About page.
pub fn summary() -> String {
    format!(
        "{CURRENT} · {BUILD_COMMIT} · {BUILD_BRANCH} · {}{}",
        build_stamp(),
        if BUILD_DIRTY { " · 含未提交修改" } else { "" }
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_and_orders_releases() {
        let version = |text: &str| Version::parse(text).unwrap();
        assert!(version("2.0.1") > version("2.0.0"));
        assert!(version("2.1") > version("2.0.9"));
        assert!(version("v2.0.0") == version("2.0.0"));
        assert!(version("2.0.0+build.7") == version("2.0.0"));
        assert!(version("2.0.0-rc.1") < version("2.0.0"));
        assert!(version("2.0.0-beta.2") > version("2.0.0-beta.1"));
        assert!(version("2.0.0-beta.10") > version("2.0.0-beta.9"));
        assert!(version("2.0.0-alpha.1") < version("2.0.0-beta"));
        assert!(version("2.0.0-1") < version("2.0.0-alpha"));
        assert!(version("2.0.0-alpha.1") < version("2.0.0-alpha.1.1"));
    }

    #[test]
    fn rejects_malformed_versions() {
        for text in ["", "v", "2.", "2..0", "2.0.0.0", "x.0.0", "2.0.0-", "2.0.0-a..b", "2.0.0-a_b"] {
            assert!(Version::parse(text).is_err(), "{text}");
        }
    }

    #[test]
    fn formats_utc_without_overflow() {
        assert_eq!(utc_stamp(0), "1970-01-01 00:00 UTC");
        assert_eq!(utc_stamp(951_782_400), "2000-02-29 00:00 UTC");
        assert_eq!(utc_stamp(1_767_225_599), "2025-12-31 23:59 UTC");
        assert_eq!(utc_stamp(1_767_225_600), "2026-01-01 00:00 UTC");
    }

    #[test]
    fn current_version_is_parseable() {
        assert_eq!(Version::parse(CURRENT).unwrap().to_string(), CURRENT);
    }
}
