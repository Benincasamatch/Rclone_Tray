//! Embeds the source revision and the shell icon in the executable so
//! diagnostics, the update module and Explorer report what was actually built.
use std::env;
use std::path::{Path, PathBuf};
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
    if env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("windows") {
        embed_shell_icon();
    }
}

/// Compiles `assets/app.rc` into a COFF resource object and links it, which is
/// what gives the EXE its icon in Explorer, the Start menu and shortcuts plus
/// the version properties shown by the installer and file properties. The tray
/// and window icons are still created at runtime from the same ICO bytes.
fn embed_shell_icon() {
    let manifest = Path::new(env!("CARGO_MANIFEST_DIR"));
    let out_dir = PathBuf::from(env::var("OUT_DIR").expect("OUT_DIR 由 Cargo 提供"));
    println!("cargo:rerun-if-changed=assets/app.rc");
    println!("cargo:rerun-if-changed=assets/icon.ico");
    if env::var("CARGO_CFG_TARGET_ENV").as_deref() != Ok("gnu") {
        println!("cargo:warning=未嵌入 EXE 资源：仅支持 Windows GNU 工具链");
        return;
    }
    let Some(windres) = windres() else {
        panic!("找不到 windres.exe；Windows GNU 构建需要 binutils（w64devkit），或设置 WINDRES 环境变量。");
    };
    let version = env::var("CARGO_PKG_VERSION").expect("CARGO_PKG_VERSION 由 Cargo 提供");
    let parts: Vec<&str> = version.split('.').collect();
    let number = |index: usize| -> u16 {
        parts
            .get(index)
            .copied()
            .unwrap_or("")
            .chars()
            .take_while(char::is_ascii_digit)
            .collect::<String>()
            .parse()
            .unwrap_or(0)
    };
    let template = std::fs::read_to_string(manifest.join("assets/app.rc"))
        .unwrap_or_else(|error| panic!("读取 assets/app.rc 失败：{error}"));
    let source = template
        .replace(
            "@VERSION_COMMA@",
            &format!("{},{},{},0", number(0), number(1), number(2)),
        )
        .replace("@VERSION_STRING@", &version);
    let source_path = out_dir.join("app.rc");
    std::fs::write(&source_path, source)
        .unwrap_or_else(|error| panic!("写入 {} 失败：{error}", source_path.display()));
    let output = out_dir.join("app_icon.o");
    let status = Command::new(&windres)
        .current_dir(manifest)
        .args(["-i"])
        .arg(&source_path)
        .arg("-o")
        .arg(&output)
        .args(["-O", "coff"])
        .status()
        .unwrap_or_else(|error| panic!("无法运行 {}：{error}", windres.display()));
    if !status.success() {
        panic!("编译 assets/app.rc 失败（{}：{}）。", windres.display(), status);
    }
    println!("cargo:rustc-link-arg-bins={}", output.display());
}

fn windres() -> Option<PathBuf> {
    if let Some(explicit) = env::var_os("WINDRES").filter(|value| !value.is_empty()) {
        return Some(PathBuf::from(explicit));
    }
    let names = ["x86_64-w64-mingw32-windres.exe", "windres.exe"];
    let path = env::var_os("PATH")?;
    for directory in env::split_paths(&path) {
        for name in names {
            let candidate = directory.join(name);
            if candidate.is_file() {
                return Some(candidate);
            }
        }
    }
    let linker = env::var_os("CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER")?;
    let directory = Path::new(&linker).parent()?;
    names
        .iter()
        .map(|name| directory.join(name))
        .find(|candidate| candidate.is_file())
}
