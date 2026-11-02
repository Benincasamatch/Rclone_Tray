#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use rclone_tray::host::{self, Launch};

fn main() {
    let mut launch = Launch::default();
    for argument in std::env::args_os().skip(1) {
        match argument.to_str() {
            Some("--autostart") => launch.autostart = true,
            Some("--show") => launch.show = true,
            Some("--help" | "-h") => {
                println!("rclone_tray [--show] [--autostart]\n\n手动启动显示挂载管理；关窗销毁前台资源但保留托盘和挂载。\n--autostart 仅供系统登录启动；--show 显式打开窗口。");
                return;
            }
            _ => {
                host::show_error(
                    std::ptr::null_mut(),
                    "参数无效；仅支持 --show、--autostart 和 --help。",
                );
                std::process::exit(2);
            }
        }
    }
    if let Err(error) = host::run(launch) {
        host::show_error(std::ptr::null_mut(), &error);
        std::process::exit(1);
    }
}
