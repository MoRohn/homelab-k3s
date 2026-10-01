# Labzilla tray app

Labzilla in the system tray (Windows, Linux) or menu bar (macOS). It starts at login and stays there while
your browser is closed.

| You do | It does |
|---|---|
| Click the icon (Windows, macOS) | Opens **Quick Ask**, a small box under the icon |
| Press **Ctrl+Shift+Space** (**⌘+Shift+Space** on macOS) | Opens Quick Ask from anywhere |
| Type a question and press Enter | Opens Labzilla on the Ask page and sends it (Shift+Enter adds a new line) |
| Menu → **Open Labzilla** | Opens the full console in its own window. Closing the window keeps the app in the tray |
| Menu → **Start at login** | Turns starting at login on or off (on after the first run) |
| Look at the icon | In colour: the server is reachable. Grey: it isn't. The menu shows which server |
| Look at Quick Ask's status | **Online**, **Not reachable**, or **Certificate not trusted** (install the CA, below) |

Linux AppIndicator trays don't report clicks, so on Linux use the menu's **Ask Labzilla…** or the shortcut.

## Install

1. **Trust the Labzilla Local CA once** on the computer (the same step as for its browser): install
   `secrets/labzilla-ca.crt` into the OS trust store. The app's windows use the OS web view, which trusts
   what the OS trusts.

   | OS | How |
   |---|---|
   | Windows | Double-click the `.crt` → Install Certificate → Local Machine → *Trusted Root Certification Authorities* |
   | macOS | Open it in Keychain Access (System keychain) → *Always Trust* |
   | Linux | `sudo cp labzilla-ca.crt /usr/local/share/ca-certificates/ && sudo update-ca-certificates` |

2. **Install the app** from the `tray` workflow's artifacts (GitHub → Actions → tray):

   | OS | File |
   |---|---|
   | Windows | `.msi` or the `-setup.exe` |
   | macOS | `.dmg` (universal). Unsigned for now: right-click → Open the first time |
   | Linux | `.deb`, `.rpm` or `.AppImage`. GNOME needs the *AppIndicator and KStatusNotifierItem Support* extension (Ubuntu ships it) |

3. **Sign in** with Menu → Open Labzilla. It's the console's normal sign-in, and the session lasts as in a browser.

The server defaults to `https://labzilla.local`. To use another one, click the server name at the bottom of
Quick Ask (e.g. `https://labzilla.tiny-dgx.lan`). `.local` names need mDNS, which is built into macOS and
Windows 10+; on Linux it needs `libnss-mdns`.

## How it works

| Part | What |
|---|---|
| Main window | The console itself (`https://labzilla.local`), loaded top-level, so sign-in, cookies and CSRF work as in a browser. Links to other sites open in your browser |
| Quick Ask | A local page bundled in the app (`src/quick.*`). It hands the prompt to the console's existing deep link, `/ask?q=…&send=1`, which sends it once |
| Availability | A TCP connect to the server's port 443 every 20 s: reachability only, no credentials |
| Certificate trust | Quick Ask fetches `/healthz` from its own web view, which trusts what the console window trusts. That fetch fails exactly when the console would show "Unacceptable TLS certificate" |
| Linux + NVIDIA | The app sets `WEBKIT_DISABLE_DMABUF_RENDERER=1` (unless set): WebKitGTK's DMA-BUF renderer can't allocate GBM buffers on NVIDIA drivers and leaves windows blank |
| Settings | `settings.json` in the OS app-config directory (`local.labzilla.tray`) |

**Security.** The console page in the main window has no native permissions: it is in no Tauri capability.
Only the bundled Quick Ask window can call the app's commands (ask, open, change server). The app stores no
password or token; the session is the web view's cookie, as in a browser.

## Build

The `tray` workflow builds every platform (`.github/workflows/tray.yml`). To build locally you need Rust,
Node 24 and, on Linux, `libwebkit2gtk-4.1-dev libayatana-appindicator3-dev librsvg2-dev libxdo-dev libssl-dev`:

```bash
cd lif/apps/tray
npm ci
npx tauri dev       # run it
npx tauri build     # installers in src-tauri/target/release/bundle/
```

Icons are generated from `assets/brand/labzilla-mark.png` (`icons/tray-template*.png` are the macOS menu-bar
silhouettes the system tints for light and dark menus).
