//! Labzilla tray / menu-bar companion.
//!
//! - The tray icon is always there while the app runs (it starts at login). It is in colour when the
//!   Labzilla server answers and grey when it doesn't.
//! - Left click (Windows, macOS), the "Ask Labzilla…" menu item, or Ctrl/Cmd+Shift+Space opens Quick Ask:
//!   a small local popover. Enter hands the prompt to the console's own deep link
//!   (`/ask?q=…&send=1`) in the main window, which sends it once.
//! - The main window *is* the console (`https://labzilla.local`), loaded top-level, so sign-in, cookies
//!   and CSRF work exactly as in a browser. That remote page gets no native capabilities. Closing the
//!   window hides it; the app keeps running in the tray.
//!
//! Linux: AppIndicator trays deliver no click events, so the menu and the shortcut are the way in.

use std::fs;
use std::net::{TcpStream, ToSocketAddrs};
use std::path::PathBuf;
use std::sync::Mutex;
use std::thread;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use tauri::image::Image;
use tauri::menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Emitter, Manager, Url, WebviewUrl, WebviewWindowBuilder, WindowEvent, Wry};
use tauri_plugin_autostart::{MacosLauncher, ManagerExt};
use tauri_plugin_global_shortcut::ShortcutState;
use tauri_plugin_positioner::{Position, WindowExt};

const DEFAULT_URL: &str = "https://labzilla.local";
const QUICK_SHORTCUT: &str = "CommandOrControl+Shift+Space";
const TRAY_ID: &str = "labzilla";
const QUICK_WIDTH: f64 = 440.0; // logical px, as in tauri.conf.json
const PROBE_EVERY: Duration = Duration::from_secs(20);
const PROBE_TIMEOUT: Duration = Duration::from_secs(3);

#[derive(Clone, Serialize, Deserialize)]
struct Settings {
    /// The console's origin, e.g. https://labzilla.local (or https://labzilla.tiny-dgx.lan).
    url: String,
    /// Start at login is switched on once, on first run; after that the menu toggle decides.
    #[serde(default)]
    autostart_initialized: bool,
}

impl Default for Settings {
    fn default() -> Self {
        Self { url: DEFAULT_URL.into(), autostart_initialized: false }
    }
}

#[derive(Clone, Serialize)]
struct Status {
    url: String,
    host: String,
    /// None until the first probe finishes.
    online: Option<bool>,
}

struct AppState {
    settings: Mutex<Settings>,
    online: Mutex<Option<bool>>,
    status_item: MenuItem<Wry>,
    /// Linux: where the tray icon is, horizontally (physical px), learned from the pointer when the
    /// tray menu's "Ask Labzilla…" is clicked. AppIndicator never reports the icon's position.
    anchor_x: Mutex<Option<f64>>,
}

// ── settings ──────────────────────────────────────────────────────────────────────────────────

fn settings_path(app: &AppHandle) -> Option<PathBuf> {
    app.path().app_config_dir().ok().map(|d| d.join("settings.json"))
}

fn load_settings(app: &AppHandle) -> Settings {
    settings_path(app)
        .and_then(|p| fs::read_to_string(p).ok())
        .and_then(|s| serde_json::from_str::<Settings>(&s).ok())
        .filter(|s| console_origin(&s.url).is_some())
        .unwrap_or_default()
}

fn save_settings(app: &AppHandle, s: &Settings) {
    if let Some(p) = settings_path(app) {
        if let Some(dir) = p.parent() {
            let _ = fs::create_dir_all(dir);
        }
        if let Ok(text) = serde_json::to_string_pretty(s) {
            let _ = fs::write(p, text);
        }
    }
}

/// An https origin with a host: the only thing the main window is allowed to show.
fn console_origin(raw: &str) -> Option<Url> {
    let u = Url::parse(raw.trim()).ok()?;
    if u.scheme() != "https" || u.host_str().is_none() {
        return None;
    }
    Url::parse(&u.origin().ascii_serialization()).ok()
}

fn current_url(app: &AppHandle) -> Url {
    let st = app.state::<AppState>();
    let raw = st.settings.lock().unwrap().url.clone();
    console_origin(&raw).unwrap_or_else(|| Url::parse(DEFAULT_URL).unwrap())
}

fn status(app: &AppHandle) -> Status {
    let url = current_url(app);
    let online = *app.state::<AppState>().online.lock().unwrap();
    Status { host: url.host_str().unwrap_or_default().to_string(), url: url.to_string(), online }
}

// ── windows ───────────────────────────────────────────────────────────────────────────────────

/// Show the console, optionally at a path (e.g. the Ask deep link). Created on first use.
fn show_main(app: &AppHandle, target: Option<Url>) -> tauri::Result<()> {
    if let Some(w) = app.get_webview_window("main") {
        if let Some(u) = target {
            w.navigate(u)?;
        }
        w.show()?;
        w.unminimize()?;
        return w.set_focus();
    }
    let origin = current_url(app);
    let start = target.unwrap_or_else(|| origin.clone());
    let nav_origin = origin.origin();
    let opener = app.clone();
    let w = WebviewWindowBuilder::new(app, "main", WebviewUrl::External(start))
        .title("Labzilla")
        .inner_size(1180.0, 820.0)
        .min_inner_size(380.0, 520.0)
        // Stay on the console. Anything else (docs, model cards, GitHub) opens in the system browser.
        .on_navigation(move |u| {
            if u.origin() == nav_origin {
                return true;
            }
            if matches!(u.scheme(), "https" | "http") {
                let _ = tauri_plugin_opener::OpenerExt::opener(&opener).open_url(u.as_str(), None::<&str>);
            }
            false
        })
        .build()?;
    let hide = w.clone();
    w.on_window_event(move |e| {
        if let WindowEvent::CloseRequested { api, .. } = e {
            api.prevent_close(); // keep running in the tray
            let _ = hide.hide();
        }
    });
    Ok(())
}

/// How Quick Ask was opened: it decides where the popover goes.
#[derive(Clone, Copy, PartialEq)]
enum Opener {
    /// A click on the tray icon (Windows, macOS): its position is known.
    TrayClick,
    /// The tray menu's "Ask Labzilla…": on Linux the pointer is in the menu that dropped from the icon.
    Menu,
    Shortcut,
}

/// Toggle Quick Ask, dropping it from just beneath the tray icon.
fn toggle_quick(app: &AppHandle, opener: Opener) {
    let Some(w) = app.get_webview_window("quick") else { return };
    if w.is_visible().unwrap_or(false) {
        let _ = w.hide();
        return;
    }
    place_quick(app, &w, opener);
    let _ = w.emit("status", status(app));
    let _ = w.show();
    let _ = w.set_focus();
    let _ = w.emit("focus-input", ());
}

fn place_quick(app: &AppHandle, w: &tauri::WebviewWindow, opener: Opener) {
    // Windows / macOS: the positioner knows the tray icon's rectangle after any tray event (click,
    // hover), and puts the popover under it (menu bar) or above it (taskbar at the bottom).
    if !cfg!(target_os = "linux") && w.move_window(Position::TrayCenter).is_ok() {
        return;
    }
    let anchor = *app.state::<AppState>().anchor_x.lock().unwrap();
    let anchor = if cfg!(target_os = "linux") && opener == Opener::Menu {
        let x = w.cursor_position().ok().map(|p| p.x);
        if x.is_some() {
            *app.state::<AppState>().anchor_x.lock().unwrap() = x;
        }
        x.or(anchor)
    } else {
        anchor
    };
    if place_under_top_bar(w, anchor).is_err() {
        let _ = w.move_window(Position::TopRight);
    }
}

/// Just beneath the top bar (the monitor's work area starts below it), centred on `anchor_x`, or at
/// the right-hand end where trays sit when the icon's position isn't known yet.
fn place_under_top_bar(w: &tauri::WebviewWindow, anchor_x: Option<f64>) -> tauri::Result<()> {
    let cursor = w.cursor_position()?;
    let monitor = match w.monitor_from_point(cursor.x, cursor.y)? {
        Some(m) => m,
        None => w.primary_monitor()?.ok_or(tauri::Error::WindowNotFound)?,
    };
    let area = monitor.work_area();
    let scale = monitor.scale_factor();
    let (left, top) = (area.position.x as f64, area.position.y as f64);
    // Not outer_size(): GTK reports a placeholder size for a window that hasn't been shown yet.
    let (width, popover) = (area.size.width as f64, QUICK_WIDTH * scale);
    let margin = 8.0 * scale;
    let max_x = left + width - popover - margin;
    let x = anchor_x.map(|a| a - popover / 2.0).unwrap_or(max_x).clamp(left + margin, max_x.max(left + margin));
    w.set_position(tauri::PhysicalPosition::new(x.round() as i32, (top + 4.0 * scale).round() as i32))
}

// ── availability ──────────────────────────────────────────────────────────────────────────────

/// Is the server reachable? A TCP connect to the console's https port: no HTTP or TLS client needed,
/// and it uses the OS resolver, so `.local` names resolve over mDNS where the OS supports it.
fn probe(url: &Url) -> bool {
    let (Some(host), Some(port)) = (url.host_str(), url.port_or_known_default()) else { return false };
    let Ok(addrs) = (host, port).to_socket_addrs() else { return false };
    addrs.into_iter().any(|a| TcpStream::connect_timeout(&a, PROBE_TIMEOUT).is_ok())
}

fn apply_status(app: &AppHandle, online: bool) {
    let st = app.state::<AppState>();
    let changed = st.online.lock().unwrap().replace(online) != Some(online);
    if !changed {
        return;
    }
    let s = status(app);
    let label = if online { format!("● Online · {}", s.host) } else { format!("○ Not reachable · {}", s.host) };
    let _ = st.status_item.set_text(label);
    if let Some(tray) = app.tray_by_id(TRAY_ID) {
        let _ = tray.set_icon(Some(tray_icon(online)));
        let _ = tray.set_tooltip(Some(if online { "Labzilla" } else { "Labzilla (not reachable)" }));
    }
    let _ = app.emit("status", s);
}

fn start_probe(app: AppHandle) {
    thread::spawn(move || loop {
        let online = probe(&current_url(&app));
        apply_status(&app, online);
        thread::sleep(PROBE_EVERY);
    });
}

fn tray_icon(online: bool) -> Image<'static> {
    #[cfg(target_os = "macos")]
    let bytes: &[u8] = if online {
        include_bytes!("../icons/tray-template.png")
    } else {
        include_bytes!("../icons/tray-template-offline.png")
    };
    #[cfg(not(target_os = "macos"))]
    let bytes: &[u8] = if online { include_bytes!("../icons/tray.png") } else { include_bytes!("../icons/tray-offline.png") };
    Image::from_bytes(bytes).expect("bundled tray icon is a valid PNG")
}

// ── commands (Quick Ask window only; the remote console has no capabilities) ──────────────────

#[tauri::command]
fn get_status(app: AppHandle) -> Status {
    status(&app)
}

#[tauri::command]
fn ask(app: AppHandle, text: String) -> Result<(), String> {
    let text = text.trim();
    let mut target = current_url(&app).join("/ask").map_err(|e| e.to_string())?;
    if !text.is_empty() {
        target.query_pairs_mut().append_pair("q", text).append_pair("send", "1");
    }
    if let Some(q) = app.get_webview_window("quick") {
        let _ = q.hide();
    }
    show_main(&app, Some(target)).map_err(|e| e.to_string())
}

#[tauri::command]
fn open_console(app: AppHandle) -> Result<(), String> {
    if let Some(q) = app.get_webview_window("quick") {
        let _ = q.hide();
    }
    show_main(&app, None).map_err(|e| e.to_string())
}

#[tauri::command]
fn hide_quick(app: AppHandle) {
    if let Some(q) = app.get_webview_window("quick") {
        let _ = q.hide();
    }
}

#[tauri::command]
fn set_server(app: AppHandle, url: String) -> Result<Status, String> {
    let origin = console_origin(&url).ok_or("Use an https address, e.g. https://labzilla.local")?;
    {
        let st = app.state::<AppState>();
        let mut s = st.settings.lock().unwrap();
        s.url = origin.to_string();
        save_settings(&app, &s);
        *st.online.lock().unwrap() = None;
    }
    // The open console belongs to the old server; the next open loads the new one.
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.destroy();
    }
    let probe_app = app.clone();
    thread::spawn(move || {
        let online = probe(&current_url(&probe_app));
        apply_status(&probe_app, online);
    });
    Ok(status(&app))
}

// ── app ───────────────────────────────────────────────────────────────────────────────────────

pub fn run() {
    tauri::Builder::default()
        // A second launch (e.g. from the Start menu) shows the running app instead of a second tray icon.
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            let _ = show_main(app, None);
        }))
        .plugin(tauri_plugin_autostart::init(MacosLauncher::LaunchAgent, None))
        .plugin(tauri_plugin_positioner::init())
        .plugin(tauri_plugin_opener::init())
        .plugin(
            tauri_plugin_global_shortcut::Builder::new()
                .with_shortcuts([QUICK_SHORTCUT])
                .expect("valid shortcut")
                .with_handler(|app, _shortcut, event| {
                    if event.state == ShortcutState::Pressed {
                        toggle_quick(app, Opener::Shortcut);
                    }
                })
                .build(),
        )
        .invoke_handler(tauri::generate_handler![get_status, ask, open_console, hide_quick, set_server])
        .setup(|app| {
            #[cfg(target_os = "macos")]
            app.set_activation_policy(tauri::ActivationPolicy::Accessory); // menu-bar app: no Dock icon

            let handle = app.handle().clone();
            let mut settings = load_settings(&handle);
            if !settings.autostart_initialized {
                let _ = app.autolaunch().enable();
                settings.autostart_initialized = true;
                save_settings(&handle, &settings);
            }

            let ask_item = MenuItem::with_id(app, "ask", "Ask Labzilla…", true, Some(QUICK_SHORTCUT))?;
            let open_item = MenuItem::with_id(app, "open", "Open Labzilla", true, None::<&str>)?;
            let status_item = MenuItem::with_id(app, "status", "Checking…", false, None::<&str>)?;
            let login = app.autolaunch().is_enabled().unwrap_or(false);
            let login_item = CheckMenuItem::with_id(app, "autostart", "Start at login", true, login, None::<&str>)?;
            let quit_item = MenuItem::with_id(app, "quit", "Quit Labzilla", true, None::<&str>)?;
            let menu = Menu::with_items(
                app,
                &[
                    &ask_item,
                    &open_item,
                    &PredefinedMenuItem::separator(app)?,
                    &status_item,
                    &login_item,
                    &PredefinedMenuItem::separator(app)?,
                    &quit_item,
                ],
            )?;
            app.manage(AppState {
                settings: Mutex::new(settings),
                online: Mutex::new(None),
                status_item: status_item.clone(),
                anchor_x: Mutex::new(None),
            });

            let login_toggle = login_item.clone();
            let tray = TrayIconBuilder::with_id(TRAY_ID)
                .icon(tray_icon(true))
                .tooltip("Labzilla")
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_menu_event(move |app, event| match event.id().as_ref() {
                    "ask" => toggle_quick(app, Opener::Menu),
                    "open" => {
                        let _ = show_main(app, None);
                    }
                    "autostart" => {
                        let al = app.autolaunch();
                        let on = al.is_enabled().unwrap_or(false);
                        let _ = if on { al.disable() } else { al.enable() };
                        let _ = login_toggle.set_checked(al.is_enabled().unwrap_or(!on));
                    }
                    "quit" => app.exit(0),
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    tauri_plugin_positioner::on_tray_event(tray.app_handle(), &event);
                    if let TrayIconEvent::Click { button: MouseButton::Left, button_state: MouseButtonState::Up, .. } = event {
                        toggle_quick(tray.app_handle(), Opener::TrayClick);
                    }
                });
            #[cfg(target_os = "macos")]
            let tray = tray.icon_as_template(true);
            tray.build(app)?;

            // Quick Ask hides when it loses focus, like any tray popover.
            if let Some(q) = app.get_webview_window("quick") {
                let hide = q.clone();
                q.on_window_event(move |e| {
                    if let WindowEvent::Focused(false) = e {
                        let _ = hide.hide();
                    }
                });
            }

            start_probe(handle);
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building Labzilla tray")
        // Closing every window must not quit: the tray is the app.
        .run(|_app, event| {
            if let tauri::RunEvent::ExitRequested { api, code, .. } = event {
                if code.is_none() {
                    api.prevent_exit();
                }
            }
        });
}
