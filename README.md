# Lumix Lux for Windows

A PC web app that copies photos and videos off a Panasonic Lumix over Wi-Fi into a folder on the PC and serves a gallery to
any device on the home network: day groups, search/filter/sort, zoomable viewer with EXIF details, and finding the camera by
itself. Standard-library Python, no dependencies, no build step. Files are never deleted from the camera.

Supported cameras: Lumix ZS100 / TZ100 today; more are on the way (see
[lumix-lux-core](https://github.com/CameronChambers93/lumix-lux-core), `docs/REDESIGN.md`). The camera serves one app at a
time, so close the [Android app](https://github.com/CameronChambers93/lumix-lux-android) or other Lumix apps first.

## Camera setup

Wi-Fi menu → Remote Shooting & View, and leave it on the waiting screen. Join your home network ("Via Network"; the camera
is then usually at a LAN address such as `10.0.0.220`). The app finds the camera by itself; the address can also be set in Settings.

## Install

Download `lumix-pc-<version>.zip` from [Releases](../../releases), unzip, install [Python 3.9+](https://www.python.org/downloads/)
(tick "Add to PATH") and double-click `lumix.bat`. Open http://localhost:8420, or `http://<pc-ip>:8420` from a phone.
Photos go to `Pictures\Lumix` by default.

## Run from source

```sh
git clone --recurse-submodules https://github.com/CameronChambers93/lumix-lux-windows.git
python3 app/server.py
```

Environment: `LUMIX_PORT` (8420), `LUMIX_LIBRARY` (`/photos`), `LUMIX_DATA` (`./data`), `LUMIX_CAMERA` (initial camera IP),
`LUMIX_APK` (an Android APK to offer at `/lumix.apk`). Release zip: `sh pc/release.sh <version>`.

Docs: [CLAUDE.md](CLAUDE.md) (architecture and dev setup), `core/docs/protocols/panasonic-camcgi.md` (the camera protocol).

History: split from [lumix-zs100-controller](https://github.com/CameronChambers93/lumix-zs100-controller) with history kept.
