# lumix-lux-windows

PC web app that copies photos and videos off a Panasonic Lumix over Wi-Fi into `%USERPROFILE%\Pictures\Lumix` and lets any
device on the home network browse and download them. Today it supports the ZS100 only; the multi-camera redesign plan is
`core/docs/REDESIGN.md` (read it before changing camera code). Protocol: `core/docs/protocols/panasonic-camcgi.md`.

- **Shared code is in `core/`** (git submodule, repo `lumix-lux-core`): drivers, protocol doc, `core/tools/fakecam.py`.
  Edit it in `core/`, commit and push there, then commit the submodule bump here. If `core/` is empty: `git submodule update --init`.
- **URL:** http://10.0.0.129:8420 (the PC's LAN address; port published by this dc container).
- **Camera:** joins the home Wi-Fi "Via Network" (Wi-Fi → Remote Shooting & View) and is at `10.0.0.220`
  (changeable in the app's settings, saved in `data/config.json`). It only answers while on that waiting screen.
- **Code:** `app/server.py` (standard-library Python, no deps) + `app/static/index.html` (single page, no build step).
  `app/start.sh` (re)starts it in the background; `postStartCommand` runs it on every container start. Log: `/tmp/lumix.log`.
  After editing the server: `sh app/start.sh`. Static files are read per request, so no restart for those.
- **Paths in the container:** library `/photos` (bind mount of `Pictures\Lumix`), app state in `data/` (gitignored:
  config, cached catalog, thumbnail/preview cache so the gallery works with the camera off).
- **Never deletes from the camera.** Files are named as on the card (`P1060871.JPG`); "on the PC" means a file with that
  name and the camera's byte size exists. Downloads go to `*.part` then rename.
- **The camera serves one app at a time** (others get 503 / `err_already_connected`). The server only polls it while
  a page polled `/api/status` in the last 30 s or a copy is running, then lets go so the phone app can connect.
- **The server finds the camera itself:** when the configured IP stops answering (and a page is polling), it sweeps the /24 with
  `getstate` (multicast SSDP first; blocked by Docker NAT) at most once a minute and saves the new IP. Restart as `-u vscode` (`/tmp/lumix.log`).
- **The camera drops back to `rec` mode** some seconds after its last client leaves; the server sends `camcmd&value=playmode`
  before listing or downloading when `getstate` says otherwise.
- **UI mirrors the Android gallery** (all but Remote): day groups, search, Photos/Videos filter, sort, long-press/drag select,
  pull-to-refresh, fast scroll, zoomable viewer with Full size, Details panel, theme, Find camera. Server additions: capture dates in
  `data/dates.json` (from `X-REC_DATE_TIME` on any camera fetch, plus a background filler thread `fill_dates` that HEADs thumbnails
  newest-first when the camera is held and idle; library JPEGs use EXIF), `GET /api/exif/<id>` (pure-Python parser; local file or the first
  128 KB of the camera original; cached in `data/exif.json`), `POST /api/find`; `/api/items` rows have `date`, `dur`, `w`, `h`.
  `python3 app/test_exif.py` checks the EXIF parser.
- The camera's web server is tiny: camera fetches go through a 2-slot semaphore, and the full-file download is serial.
- **`/lumix.apk`** serves the Android app from `$LUMIX_APK` (default `dist/lumix.apk`); the APK is built in lumix-lux-android,
  copy it here to offer it on the LAN.
- **Release:** `sh pc/release.sh <version>` → `dist/lumix-pc-<version>.zip` (the app, `lumix.bat`, the protocol doc). Users unzip it, install
  Python 3.9+ and double-click `lumix.bat`.

History: split from `lumix-zs100-controller` with history kept (before the split, paths were `app/`, `pc/` at the same place).
