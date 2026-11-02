//! Embeds the source revision in the executable so diagnostics and the update
//! module report what was actually built, not just the Cargo version.
use std::process::Command;
use std::time::{SystemTime, UNIX_EPOCH};

fn git(args: &[&str]) -> Option<String> {
    let output = Command::new("git")
        .args(args)
        .current_dir(env!("CARGO_MANIFEST_DIR"))
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let text = String::from_utf8_lossy(&output.stdout).trim().to_string();
    (!text.is_empty()).then_some(text)
}

fn main() {
    println!("cargo:rerun-if-changed=build.rs");
    println!("cargo:rerun-if-changed=.git/HEAD");
    println!("cargo:rerun-if-changed=.git/index");
    let commit = git(&["rev-parse", "--short=8", "HEAD"]).unwrap_or_else(|| "unknown".into());
    let branch = git(&["symbolic-ref", "--short", "-q", "HEAD"]).unwrap_or_else(|| "detached".into());
    let dirty = git(&["status", "--porcelain", "--untracked-files=no"])
        .is_some_and(|status| !status.is_empty());
    let epoch = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|elapsed| elapsed.as_secs())
        .unwrap_or_default();
    println!("cargo:rustc-env=RT_BUILD_COMMIT={commit}");
    println!("cargo:rustc-env=RT_BUILD_BRANCH={branch}");
    println!("cargo:rustc-env=RT_BUILD_DIRTY={}", u8::from(dirty));
    println!("cargo:rustc-env=RT_BUILD_EPOCH={epoch}");
}
