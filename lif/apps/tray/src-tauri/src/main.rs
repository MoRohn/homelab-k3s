// No console window behind the tray app on Windows release builds.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    // WebKitGTK's DMA-BUF renderer can't allocate GBM buffers on NVIDIA drivers ("Failed to create GBM
    // buffer … Permission denied"), leaving every window blank. Seen on the DGX Spark's GNOME desktop.
    #[cfg(target_os = "linux")]
    if std::env::var_os("WEBKIT_DISABLE_DMABUF_RENDERER").is_none() {
        std::env::set_var("WEBKIT_DISABLE_DMABUF_RENDERER", "1");
    }
    labzilla_tray_lib::run()
}
