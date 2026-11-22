#!/usr/bin/env python3
"""Lumix Wi-Fi sync: pairs with a Panasonic Lumix on the LAN, copies its photos and videos into a library
folder, and serves a gallery web app to any device on the network. The camera code is core's luxcamera runtime, driven by
the drivers in core/drivers; protocol notes: core/docs/protocols/panasonic-camcgi.md.

Standard library only. Config via env: LUMIX_PORT (8420), LUMIX_LIBRARY (/photos), LUMIX_DATA (../data),
LUMIX_CAMERA (initial camera IP; later changes made in the web app are saved in DATA/config.json),
LUMIX_PORT_MAP (tests only: "80:18080,60606:18080" sends the camera's ports to tools/fakecam.py's one port).
"""
from __future__ import annotations

import json
import os
import re
import struct
import sys
import threading
import time
import traceback
import urllib.parse
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "core" / "py"))  # the core submodule (the release zip has the same layout)
import luxcamera as lux  # noqa: E402

import diagnostics  # noqa: E402

PORT = int(os.environ.get("LUMIX_PORT", "8420"))
LIBRARY = Path(os.environ.get("LUMIX_LIBRARY", "/photos"))
DATA = Path(os.environ.get("LUMIX_DATA", HERE.parent / "data"))
STATIC = HERE / "static"
APP_NAME = "lumix-hub"
MEDIA_RE = re.compile(r"P(\d+)\.(JPG|MP4)", re.I)
CTYPES = {"JPG": "image/jpeg", "MP4": "video/mp4"}
PORT_MAP = dict(map(int, kv.split(":")) for kv in os.environ.get("LUMIX_PORT_MAP", "").split(",") if kv)
DRIVERS = lux.load_drivers()
BASE = lux.match_driver(DRIVERS, {})["driver"]  # the family base: used to find a camera before we know which one it is


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def write_json(path: Path, value) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value))
    tmp.replace(path)


# ---------------------------------------------------------------- EXIF (JPEG APP1: TIFF IFD0 + ExifIFD + GPS IFD)

EXIF_HEAD = 128 * 1024  # an APP1 segment is at most 64 KB and sits right after the SOI (maybe behind a small APP0)
_TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8}
_TYPE_CODE = {1: "B", 3: "H", 4: "I", 6: "b", 7: "B", 8: "h", 9: "i"}
EMPTY_EXIF = {k: None for k in ("model", "lens", "focal", "focal35", "aperture", "shutter", "iso", "expComp", "flash",
                                "width", "height", "taken", "lat", "lon")}


def parse_exif(buf: bytes) -> dict:
    """Read camera details from the start of a JPEG. Returns {} when there is no EXIF block, else a dict with the same
    keys the Android app's exif() returns (None where a tag is missing)."""
    if buf[:2] != b"\xff\xd8":
        return {}
    pos, tiff = 2, None
    while pos + 4 <= len(buf) and buf[pos] == 0xFF:
        mk = buf[pos + 1]
        if mk == 0xFF:
            pos += 1
        elif mk in (0xD9, 0xDA):
            break
        elif mk == 0x01 or 0xD0 <= mk <= 0xD8:
            pos += 2
        else:
            ln = struct.unpack(">H", buf[pos + 2:pos + 4])[0]
            if mk == 0xE1 and buf[pos + 4:pos + 10] == b"Exif\0\0":
                tiff = buf[pos + 10:pos + 2 + ln]
                break
            pos += 2 + ln
    if not tiff or len(tiff) < 8 or tiff[:2] not in (b"II", b"MM"):
        return {}
    bo = ">" if tiff[:2] == b"MM" else "<"

    def unpack(fmt, off):
        n = struct.calcsize(bo + fmt)
        if off < 0 or off + n > len(tiff):
            raise ValueError("outside the EXIF block")
        return struct.unpack_from(bo + fmt, tiff, off)

    def read_ifd(off):
        tags = {}
        try:
            count = unpack("H", off)[0]
        except ValueError:
            return tags
        for n in range(min(count, 512)):
            e = off + 2 + 12 * n
            try:
                tag, typ, cnt = unpack("HHI", e)
                size = _TYPE_SIZE.get(typ, 0) * cnt
                if not size or cnt > 4096:
                    continue
                at = e + 8 if size <= 4 else unpack("I", e + 8)[0]
                if typ == 2:
                    tags[tag] = tiff[at:at + cnt].split(b"\0")[0].decode("latin-1").strip()
                    continue
                if typ in (5, 10):
                    f = unpack(f"{cnt * 2}{'I' if typ == 5 else 'i'}", at)
                    vals = [a / b if b else 0.0 for a, b in zip(f[0::2], f[1::2])]
                else:
                    vals = list(unpack(f"{cnt}{_TYPE_CODE[typ]}", at))
                tags[tag] = vals[0] if cnt == 1 else vals
            except (ValueError, struct.error, KeyError):
                continue
        return tags

    def first(v):
        return v[0] if isinstance(v, list) and v else v

    def num(v, nd=4):
        v = first(v)
        return round(float(v), nd) if isinstance(v, (int, float)) else None

    try:
        ifd0 = read_ifd(unpack("I", 4)[0])
    except ValueError:
        return {}
    ex = read_ifd(ifd0[0x8769]) if isinstance(ifd0.get(0x8769), int) else {}
    gps = read_ifd(ifd0[0x8825]) if isinstance(ifd0.get(0x8825), int) else {}
    out = dict(EMPTY_EXIF)
    out["model"] = ifd0.get(0x0110) or None
    out["lens"] = ex.get(0xA434) or None
    out["focal"] = num(ex.get(0x920A)) or None
    out["focal35"] = num(ex.get(0xA405), 0) or None
    if out["focal35"]:
        out["focal35"] = int(out["focal35"])
    out["aperture"] = num(ex.get(0x829D)) or None
    out["shutter"] = num(ex.get(0x829A), 6) or None
    iso = first(ex.get(0x8827))
    out["iso"] = int(iso) if isinstance(iso, (int, float)) and iso else None
    out["expComp"] = num(ex.get(0x9204), 2)
    fl = first(ex.get(0x9209))
    out["flash"] = bool(int(fl) & 1) if isinstance(fl, (int, float)) else None
    w, h = first(ex.get(0xA002)), first(ex.get(0xA003))
    if not (isinstance(w, (int, float)) and isinstance(h, (int, float)) and w and h):
        w, h = first(ifd0.get(0x0100)), first(ifd0.get(0x0101))
    if isinstance(w, (int, float)) and isinstance(h, (int, float)) and w and h:
        out["width"], out["height"] = int(w), int(h)
    out["taken"] = ex.get(0x9003) or ifd0.get(0x0132) or None

    def coord(v, ref):
        if isinstance(v, list) and len(v) == 3 and isinstance(ref, str) and ref:
            d = v[0] + v[1] / 60 + v[2] / 3600
            return round(-d if ref[0].upper() in "SW" else d, 6)
        return None
    lat, lon = coord(gps.get(2), gps.get(1)), coord(gps.get(4), gps.get(3))
    if lat is not None and lon is not None:
        out["lat"], out["lon"] = lat, lon
    return out


def exif_iso(taken):
    """EXIF '2026:10:31 21:38:14' -> '2026-10-31T21:38:14' (None if missing or zeroed)."""
    m = re.match(r"^(\d{4}):(\d\d):(\d\d)[ T](\d\d):(\d\d):(\d\d)", taken or "")
    return f"{m[1]}-{m[2]}-{m[3]}T{m[4]}:{m[5]}:{m[6]}" if m and m[1] != "0000" else None


def read_head(path: Path, n: int = EXIF_HEAD) -> bytes:
    with open(path, "rb") as f:
        return f.read(n)


# ---------------------------------------------------------------- finding the camera

SEARCH_EVERY = 60  # seconds between searches while a page is polling and the camera isn't answering
SWEEP_TIMEOUT = 1.5
SWEEP_THREADS = 32


def probe_camera(ip: str, timeout: float = SWEEP_TIMEOUT):
    """Is there a camera of the driver family at ip? 'ok', 'busy' (another app holds it), else None."""
    return lux.Camera(BASE, ip, port_map=PORT_MAP).probe(timeout)


def ssdp_search(timeout: float = 1.5) -> list[str]:
    """Multicast M-SEARCH; returns the IPs of replies that look like the driver family's cameras.
    Docker NAT blocks the replies, so this usually returns nothing there; it works on a plain LAN."""
    found: list[str] = []
    for sender, reply in lux.ssdp_search(lux.ssdp_request(BASE), timeout=timeout):
        if lux.parse_ssdp(reply, BASE)["matches"] and sender not in found:
            found.append(sender)
    return found


def sweep_subnet(ip: str):
    """Probe every host of ip's /24 (not ip itself) in parallel; returns (found_ip, kind) for the first camera."""
    parts = ip.split(".")
    if len(parts) != 4 or not all(p.isdigit() for p in parts):
        return None, None
    base = ".".join(parts[:3])
    hosts = [f"{base}.{n}" for n in range(1, 255) if f"{base}.{n}" != ip]
    ex = ThreadPoolExecutor(SWEEP_THREADS)
    try:
        for host, kind in zip(hosts, ex.map(probe_camera, hosts)):
            if kind:
                return host, kind
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    return None, None


# ---------------------------------------------------------------- camera

class Camera:
    """The one camera the server holds: luxcamera's Camera plus online/error state for the page. It starts with the
    family base driver; each time the camera comes online it asks what it is and switches to the matching driver."""

    def __init__(self, ip: str, app_id: str):
        self.app_id = app_id
        self.online = False
        self.state: dict = {}  # luxcamera's parsed getstate: ok, battery, mode, state (the raw fields)
        self.error = ""
        self.info: dict = {}  # what identify() said: capabilityModel, modelNumber, friendlyName, firmware
        self.match = {"driver": BASE, "matched": False, "by": None}
        self.lc = self._lux(BASE, ip)

    def _lux(self, driver: dict, ip: str):
        lc = lux.Camera(driver, ip, app_id=self.app_id, app_name=APP_NAME, port_map=PORT_MAP)
        self.media_slots = threading.Semaphore(driver["media"]["slots"])  # the camera's web server is small; don't flood it
        return lc

    @property
    def ip(self) -> str:
        return self.lc.ip

    def move(self, ip: str):
        """A new address (found by a search or typed in Settings): forget the connection, keep the driver until it answers."""
        self.lc = self._lux(self.lc.driver, ip)
        self.online = False

    @property
    def driver(self) -> dict:
        return self.lc.driver

    def poll(self) -> bool:
        """Keepalive and online check. Returns True when the camera has just come online."""
        try:
            came_online = not self.online
            if came_online:
                st = self.lc.connect()  # getstate, pair, media mode
                log("paired with", self.ip)
                self.identify()
            else:
                st = self.lc.state()
                if not st["ok"]:
                    self.lc.pair()
                    st = self.lc.state()
                self.ensure_play()
            self.state = dict(self.lc.last_state or st)
            self.online, self.error = True, ""
            return came_online
        except Exception as e:  # timeouts are the normal "camera off" case
            if self.online:
                log("camera offline:", e)
            self.online = False
            if isinstance(e, lux.Busy):
                self.error = "The camera is connected to another device (the phone app?). Close it there."
            elif isinstance(e, OSError):
                self.error = "Camera not reachable"
            else:
                self.error = str(e)
            return False

    def identify(self):
        """Ask the camera what it is (unicast SSDP -> device description, and the capability reply), pick the driver."""
        location = None
        try:
            ssdp = self.driver["discovery"]["ssdp"]
            for _, reply in lux.ssdp_search(lux.ssdp_request(self.driver, multicast=False), (self.ip, ssdp["port"]), 1.5):
                s = lux.parse_ssdp(reply, self.driver)
                if s["matches"] and s["location"]:
                    location = s["location"]
                    break
            self.info = self.lc.identify(location)
        except Exception as e:
            log("identify failed:", e)
            self.info = self.lc.identify(None)  # the capability reply alone
        m = lux.match_driver(DRIVERS, self.info)
        cds = self.lc.cds_url
        if m["driver"]["id"] != self.driver["id"]:
            self.lc = self._lux(m["driver"], self.ip)
            self.lc.last_state = None
        self.lc.cds_url = cds
        self.match = m
        log(f"camera: {self.info or 'unknown'} -> driver {m['driver']['id']}" + (f" (by {m['by']})" if m["matched"] else " (cautious mode)"))

    def ensure_play(self, fresh: bool = False):
        """The camera drops back to rec mode by itself after a client leaves, and media access wants playback mode.
        Non-destructive; luxcamera sends it whenever the mode isn't the driver's media mode."""
        try:
            if fresh:
                self.lc.state()
            if self.lc.ensure_media(self.lc.last_state):
                log("camera was not in media mode: switched")
        except Exception as e:
            log("ensure_play failed:", e)

    def browse_all(self) -> list[dict]:
        self.ensure_play(fresh=True)
        return self.lc.list()

    def date(self, item: dict):
        with self.media_slots:
            return self.lc.date(item)

    def open(self, item: dict, role: str = "orig", timeout: float = 30, headers: dict | None = None):
        """A streaming response for an item (guard-checked by luxcamera). headers e.g. a Range for the EXIF head."""
        req = self.lc.media_request(item, role)
        return self.lc.t.open(req["url"], timeout, {**req["headers"], **(headers or {})})

    def date_of(self, response):
        return self.lc.date_from_headers(response.headers)

    def status(self) -> dict:
        d, info = self.driver, self.info
        return {"driver": {"id": d["id"], "name": d["name"], "matched": self.match["matched"], "by": self.match["by"],
                           "features": d["features"]},
                "model": (d.get("label") if self.match["matched"] else None) or info.get("modelNumber")
                or info.get("capabilityModel") or (d.get("label") if self.state else None),
                "firmware": info.get("firmware")}


# ---------------------------------------------------------------- app state

class App:
    def __init__(self):
        for d in (DATA, DATA / "thumbs", DATA / "previews"):
            d.mkdir(parents=True, exist_ok=True)
        LIBRARY.mkdir(parents=True, exist_ok=True)
        self.cfg = read_json(DATA / "config.json", {})
        self.cfg.setdefault("camera_ip", os.environ.get("LUMIX_CAMERA", "10.0.0.220"))
        # Panasonic apps send a UUID-shaped id; keep one per install so the camera sees the same app.
        self.cfg.setdefault("app_id", "4D454930-0100-1000-8001-" + uuid.uuid4().hex[:12].upper())
        write_json(DATA / "config.json", self.cfg)
        self.cam = Camera(self.cfg["camera_ip"], self.cfg["app_id"])
        self.catalog: list[dict] = read_json(DATA / "catalog.json", [])
        self.catalog_at = 0.0
        self.catalog_error = ""
        # Capture dates (camera local time, 'YYYY-MM-DDTHH:MM:SS'), from X-REC_DATE_TIME on any media response.
        self.dates: dict[str, str] = read_json(DATA / "dates.json", {})
        self.dates_dirty, self.dates_saved = False, 0.0
        self.exif_cache: dict[str, dict] = read_json(DATA / "exif.json", {})
        self.inbox = diagnostics.Inbox(DATA / "diagnostics" / "inbox")  # reports from the phone app's "Add a camera"
        self.local_meta: dict[str, tuple] = {}  # library file name -> ((mtime_ns, size), iso date, from_exif)
        self.persist_lock = threading.Lock()
        self.last_media = 0.0  # last time a page-driven fetch (preview, original, details) used the camera
        self.lock = threading.Lock()
        self.download_lock = threading.Lock()
        self.searching = False
        self.last_search = 0.0
        self.last_activity = 0.0  # no page open yet: don't grab the camera on startup
        self.sync = {"running": False, "done": 0, "total": 0, "bytes": 0, "total_bytes": 0,
                     "current": "", "errors": [], "finished": 0, "cancel": False}

    # -- camera watcher
    def watch(self):
        # The camera serves one app at a time, so only hold it while someone has the page open (it polls
        # /api/status every few seconds) or a copy is running. Otherwise let go so the phone app can connect.
        while True:
            if self.sync["running"] or time.time() - self.last_activity < 30:
                try:
                    if self.cam.poll():
                        threading.Thread(target=self.refresh_catalog, daemon=True).start()
                    elif self.should_search():
                        self.search_camera()
                except Exception:
                    traceback.print_exc()
                time.sleep(5)
            else:
                if self.cam.online:
                    log("idle: releasing the camera")
                self.cam.online, self.cam.error = False, "Idle (not connected)"
                time.sleep(1)

    # -- capture dates
    def record_date(self, iid: str, value) -> None:
        m = re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d", value or "")
        if not m or self.dates.get(iid) == m.group(0):
            return
        self.dates[iid] = m.group(0)
        self.dates_dirty = True
        self.flush_dates()

    def flush_dates(self, force: bool = False) -> None:
        if not self.dates_dirty or (not force and time.time() - self.dates_saved < 3):
            return
        with self.persist_lock:
            self.dates_dirty, self.dates_saved = False, time.time()
            write_json(DATA / "dates.json", dict(self.dates))

    def fetch_date(self, item: dict) -> bool:
        """One tiny request (the driver says which file and header) for the capture date."""
        v = self.cam.date(item)
        if v:
            self.record_date(item["id"], v)
        return bool(v)

    def fill_dates(self):
        """Background: while the camera is held and nothing else is using it, look up the capture date of items that
        have none, newest first, one request at a time. Stops after 3 failures in a row and tries again a minute later."""
        fails, pause_until, bad = 0, 0.0, {}
        while True:
            time.sleep(0.15 if fails == 0 else 1.5)
            try:
                self.flush_dates()
                now = time.time()
                if (not self.cam.online or self.searching or self.sync["running"] or now < pause_until
                        or now - self.last_media < 5):
                    time.sleep(1.2)
                    continue
                with self.lock:
                    todo = [i for i in self.catalog if i["id"] not in self.dates and i.get("thumb") and bad.get(i["id"], 0) < 3]
                if not todo:
                    time.sleep(3)
                    continue
                item = max(todo, key=lambda i: int(i["id"]))
                try:
                    ok = self.fetch_date(item)
                except Exception:
                    ok = False
                if ok:
                    fails = 0
                else:
                    fails += 1
                    bad[item["id"]] = bad.get(item["id"], 0) + 1
                    if fails >= 3:
                        fails, pause_until = 0, time.time() + 60
                        log("dates: 3 failures in a row, pausing for a minute")
            except Exception:
                traceback.print_exc()
                time.sleep(5)

    def should_search(self) -> bool:
        # Only when the camera didn't answer at all (not "busy": then it is right there), at most once a minute,
        # and only on behalf of an open page or a running copy (watch() only gets here in those cases).
        return (not self.cam.online and not self.searching and self.cam.error == "Camera not reachable"
                and time.time() - self.last_search >= SEARCH_EVERY)

    def search_camera(self) -> bool:
        """The configured address stopped answering: look for the camera on the network (the router may have given
        it a new address). Multicast SSDP first (works off Docker), then a sweep of the /24. Saves what it finds."""
        self.searching, self.last_search = True, time.time()
        old = self.cam.ip
        try:
            log(f"searching for the camera (not at {old})")
            ip, how = None, None
            for cand in ssdp_search():
                if cand != old and probe_camera(cand):
                    ip, how = cand, "ssdp"
                    break
            if not ip:
                ip, kind = sweep_subnet(old)
                how = f"sweep ({kind})" if ip else None
            if not ip:
                log(f"search: no camera found around {old} ({time.time() - self.last_search:.1f}s)")
                return False
            log(f"search: found the camera at {ip} via {how} ({time.time() - self.last_search:.1f}s)")
            self.cfg["camera_ip"] = ip
            write_json(DATA / "config.json", self.cfg)
            self.cam.move(ip)
            if self.cam.poll():
                self.refresh_catalog()
            return True
        finally:
            self.searching = False

    def set_camera_ip(self, ip: str):
        self.cfg["camera_ip"] = ip
        write_json(DATA / "config.json", self.cfg)
        self.cam.move(ip)
        self.last_search = 0.0  # a manual address is tried first; searching resumes if it doesn't answer
        threading.Thread(target=lambda: self.cam.poll() and self.refresh_catalog(), daemon=True).start()

    def refresh_catalog(self):
        try:
            try:
                items = self.cam.browse_all()
            except Exception:  # e.g. HTTP 500 right after a mode switch: make sure of playmode and retry once
                self.cam.ensure_play(fresh=True)
                items = self.cam.browse_all()
            with self.lock:
                self.catalog, self.catalog_at, self.catalog_error = items, time.time(), ""
            write_json(DATA / "catalog.json", items)
            log(f"catalog: {len(items)} items on camera")
        except Exception as e:
            self.catalog_error = f"Couldn't list the camera's photos: {e}"
            log(self.catalog_error)

    # -- library
    def local_files(self) -> dict[str, Path]:
        out = {}
        for p in LIBRARY.iterdir():
            m = MEDIA_RE.fullmatch(p.name)
            if m and p.is_file():
                out[m.group(1)] = p
        return out

    def by_id(self, iid: str):
        with self.lock:
            return next((i for i in self.catalog if i["id"] == iid), None)

    def is_local(self, item: dict, path) -> bool:
        return path is not None and path.stat().st_size == item["size"]

    def items(self) -> list[dict]:
        with self.lock:
            cat = {i["id"]: i for i in self.catalog}
        local = self.local_files()
        rows = []
        for iid in sorted(set(cat) | set(local), key=int, reverse=True):
            c, p = cat.get(iid), local.get(iid)
            name = c["name"] if c else p.name
            is_loc = self.is_local(c, p) if c else True
            date = self.dates.get(iid)
            if not date and p and is_loc:
                date = self.local_date(iid, p)
            ex = self.exif_cache.get(iid) or {}
            rows.append({
                "id": iid, "name": name,
                "kind": c["kind"] if c else ("video" if name.upper().endswith(".MP4") else "photo"),
                "size": c["size"] if c else p.stat().st_size,
                "camera": c is not None,
                "local": is_loc,
                "date": date, "dur": (c or {}).get("dur") or None,
                "w": ex.get("width"), "h": ex.get("height"),
            })
        return rows

    def local_date(self, iid: str, path: Path):
        """Capture date of a library file: JPEG EXIF DateTimeOriginal (the same value as the camera's header, so it is
        kept like any other date); for MP4 the file's mtime, shown but not stored (it is the copy time)."""
        try:
            st = path.stat()
            key = (st.st_mtime_ns, st.st_size)
            hit = self.local_meta.get(path.name)
            if not hit or hit[0] != key:
                iso = None
                if path.suffix.upper() == ".JPG":
                    iso = exif_iso(parse_exif(read_head(path)).get("taken"))
                from_exif = bool(iso)
                iso = iso or time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(st.st_mtime))
                hit = self.local_meta[path.name] = (key, iso, from_exif)
            if hit[2]:
                self.record_date(iid, hit[1])
            return hit[1]
        except OSError:
            return None

    # -- EXIF details
    def exif_for(self, iid: str):
        """(body, http status). Library file first; otherwise only the first 128 KB of the camera's original is read."""
        if iid in self.exif_cache:
            return self.exif_cache[iid], 200
        item, path = self.by_id(iid), self.local_files().get(iid)
        name = item["name"] if item else (path.name if path else None)
        if not name:
            return {"error": "not found"}, 404
        if name.upper().endswith(".MP4"):
            return {}, 200
        if path and (not item or self.is_local(item, path)):
            head = read_head(path)
        elif not self.cam.online:
            return {"error": "Camera is offline and this photo isn't on the PC yet"}, 409
        else:
            self.last_media = time.time()
            try:
                with self.cam.media_slots, self.cam.open(item, "orig", 20, {"Range": f"bytes=0-{EXIF_HEAD - 1}"}) as r:
                    head = r.read(EXIF_HEAD)  # a camera that ignores Range just gets closed after the first 128 KB
            finally:
                self.last_media = time.time()
        ex = parse_exif(head)
        if not ex:
            return {"error": "No camera details in this file"}, 404
        self.exif_cache[iid] = ex
        if ex.get("taken") and not self.dates.get(iid):
            self.record_date(iid, exif_iso(ex["taken"]))
        with self.persist_lock:
            write_json(DATA / "exif.json", dict(self.exif_cache))
        return ex, 200

    def download(self, item: dict, progress=None) -> Path:
        """Copy one item from the camera into the library (no-op if already there). Never deletes anything."""
        dest = LIBRARY / item["name"]
        with self.download_lock:
            if dest.exists() and dest.stat().st_size == item["size"]:
                return dest
            part = dest.with_name(dest.name + ".part")
            with self.cam.media_slots, self.cam.open(item, "orig", 60) as r, open(part, "wb") as f:
                self.record_date(item["id"], self.cam.date_of(r))
                while chunk := r.read(256 * 1024):
                    if self.sync["cancel"] and progress:
                        raise InterruptedError("cancelled")
                    f.write(chunk)
                    if progress:
                        progress(len(chunk))
            if part.stat().st_size != item["size"]:
                raise IOError(f"{item['name']}: got {part.stat().st_size} of {item['size']} bytes")
            part.replace(dest)
            self.last_media = time.time()
            return dest

    # -- sync
    def start_sync(self, ids=None) -> bool:
        if self.sync["running"]:
            return False
        local = self.local_files()
        with self.lock:
            todo = [i for i in self.catalog if not self.is_local(i, local.get(i["id"]))
                    and (ids is None or i["id"] in ids)]
        todo.sort(key=lambda i: int(i["id"]))
        self.sync.update(running=True, done=0, total=len(todo), bytes=0, cancel=False, current="",
                         total_bytes=sum(i["size"] for i in todo), errors=[], finished=0)
        threading.Thread(target=self._sync, args=(todo,), daemon=True).start()
        return True

    def _sync(self, todo):
        def progress(n):
            self.sync["bytes"] += n
        try:
            self.cam.ensure_play(fresh=True)
            for item in todo:
                if self.sync["cancel"]:
                    break
                self.sync["current"] = item["name"]
                for attempt in range(3):
                    try:
                        self.download(item, progress)
                        break
                    except InterruptedError:
                        break
                    except Exception as e:
                        if attempt == 2 or not self.cam.online:
                            self.sync["errors"].append(f"{item['name']}: {e}")
                            log("sync error:", item["name"], e)
                            break
                        time.sleep(3)
                self.sync["done"] += 1
                if not self.cam.online and self.sync["errors"]:
                    self.sync["errors"].append("Stopped: the camera went offline.")
                    break
        finally:
            for p in LIBRARY.glob("*.part"):
                p.unlink(missing_ok=True)
            self.sync.update(running=False, current="", finished=time.time())
            log(f"sync finished: {self.sync['done']}/{self.sync['total']}, {len(self.sync['errors'])} errors")

    # -- small JPEGs from the camera, cached on disk so the gallery works when the camera is off
    def cached_image(self, iid: str, kind: str):
        path = DATA / ("thumbs" if kind == "thumb" else "previews") / f"{iid}.jpg"
        if path.exists():
            return path
        item = self.by_id(iid)
        if not item or not self.cam.online or not item.get(kind):
            return None
        self.last_media = time.time()
        try:
            with self.cam.media_slots, self.cam.open(item, kind, 20) as r:
                data = r.read()
                self.record_date(iid, self.cam.date_of(r))
        finally:
            if kind == "preview":  # grid thumbnails don't hold the date filler back, a viewer's preview does
                self.last_media = time.time()
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
        return path


APP: App


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "lumix/1"

    def log_message(self, fmt, *args):
        pass

    def route(self):
        APP.last_activity = time.time()
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        p = u.path
        try:
            if self.command == "GET":
                if p in ("/", "/index.html"):
                    return self.send_file(STATIC / "index.html", "text/html; charset=utf-8", cache=False)
                if p == "/manifest.webmanifest":
                    return self.send_file(STATIC / "manifest.webmanifest", "application/manifest+json")
                if p == "/lumix.apk":  # the Android app (lumix-lux-android's build.sh); LUMIX_APK points at it
                    return self.send_file(Path(os.environ.get("LUMIX_APK", HERE.parent / "dist" / "lumix.apk")),
                                          "application/vnd.android.package-archive", cache=False, download_name="lumix.apk")
                if p == "/icon.svg":
                    return self.send_file(STATIC / "icon.svg", "image/svg+xml")
                if p == "/diagnostics":
                    return self.send_file(STATIC / "diagnostics.html", "text/html; charset=utf-8", cache=False)
                if p == "/api/diagnostics":
                    return self.json({"reports": APP.inbox.list()})
                if m := re.fullmatch(r"/api/diagnostics/([^/]+)", p):
                    path = APP.inbox.path(urllib.parse.unquote(m.group(1)))
                    return self.send_file(path, "application/json", cache=False) if path else self.json({"error": "not found"}, 404)
                if p == "/api/status":
                    return self.json(self.status())
                if p == "/api/items":
                    return self.json({"items": APP.items()})
                if m := re.fullmatch(r"/api/exif/(\d+)", p):
                    body, code = APP.exif_for(m.group(1))
                    return self.json(body, code)
                if m := re.fullmatch(r"/(thumb|preview)/(\d+)", p):
                    return self.image(m.group(1), m.group(2))
                if m := re.fullmatch(r"/media/(\d+)", p):
                    return self.media(m.group(1), download="dl" in q)
                if p == "/zip":
                    return self.zip([i for i in q.get("ids", [""])[0].split(",") if i.isdigit()])
            elif self.command == "POST":
                if p == "/api/diagnostics":  # a report from a phone; raw bytes, capped before reading
                    n = int(self.headers.get("Content-Length") or 0)
                    if n > diagnostics.MAX_BYTES:
                        return self.json({"error": "The report is too large"}, 413)
                    try:
                        return self.json(APP.inbox.save(self.rfile.read(n)))
                    except ValueError as e:
                        return self.json({"error": str(e)}, 400)
                body = self.body()
                if p == "/api/sync":
                    ids = body.get("ids")
                    return self.json({"started": APP.start_sync(set(ids) if ids else None)})
                if p == "/api/sync/cancel":
                    APP.sync["cancel"] = True
                    return self.json({"ok": True})
                if p == "/api/refresh":
                    if not APP.cam.online:
                        APP.cam.poll()
                    if not APP.cam.online and APP.cam.error == "Camera not reachable" and not APP.searching:
                        APP.search_camera()  # the refresh button is a deliberate ask: search now
                    if APP.cam.online:
                        APP.refresh_catalog()
                    return self.json(self.status())
                if p == "/api/find":
                    return self.json(self.find_camera())
                if p == "/api/settings":
                    ip = str(body.get("camera_ip", "")).strip()
                    if not re.fullmatch(r"[\w.-]+", ip):
                        return self.json({"error": "Enter an IP address like 10.0.0.220"}, 400)
                    APP.set_camera_ip(ip)
                    return self.json({"ok": True})
            self.json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            traceback.print_exc()
            try:
                self.json({"error": str(e)}, 500)
            except Exception:
                pass

    do_GET = do_HEAD = do_POST = route

    def body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}") if n else {}
        except ValueError:
            return {}

    def status(self) -> dict:
        c = APP.cam
        s = {k: v for k, v in APP.sync.items() if k != "cancel"}
        local = APP.local_files()
        with APP.lock:
            missing = sum(1 for i in APP.catalog if not APP.is_local(i, local.get(i["id"])))
            on_camera = len(APP.catalog)
        return {
            "camera": {"ip": c.ip, "online": c.online, "searching": APP.searching, "error": c.error or APP.catalog_error,
                       "battery": c.state.get("battery"), "mode": c.state.get("mode"), **c.status()},
            "library": {"path": os.environ.get("LUMIX_LIBRARY_LABEL", str(LIBRARY)), "count": len(local)},
            "on_camera": on_camera, "missing": missing, "sync": s,
        }

    def find_camera(self) -> dict:
        """The Find camera button: never disturbs a working connection; searches only when the camera isn't answering."""
        c = APP.cam
        if not c.online and not APP.searching:
            c.poll()
        if c.online:
            return {"online": True, "ip": c.ip, "message": f"Connected at {c.ip}"}
        if APP.searching:
            return {"online": False, "ip": c.ip, "message": "Already searching..."}
        if c.error == "Camera not reachable":
            if APP.search_camera():
                return {"online": c.online, "ip": c.ip, "message": f"Found the camera at {c.ip}"}
            return {"online": False, "ip": c.ip, "message": "No camera found on the network. Is it on the Wi-Fi waiting screen?"}
        return {"online": False, "ip": c.ip, "message": c.error or "Camera not reachable"}

    def json(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def send_file(self, path: Path, ctype: str, cache=True, download_name=None):
        if not path.exists():
            return self.json({"error": "not found"}, 404)
        size = path.stat().st_size
        start, end = 0, size - 1
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)) if m.group(2) else size - 1, size - 1)
            else:
                start = max(size - int(m.group(2)), 0)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "max-age=86400" if cache else "no-cache")
        if download_name:
            self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0 and (chunk := f.read(min(left, 256 * 1024))):
                self.wfile.write(chunk)
                left -= len(chunk)

    def image(self, kind: str, iid: str):
        path = APP.cached_image(iid, kind)
        if path:
            return self.send_file(path, "image/jpeg")
        # Camera offline and nothing cached: fall back to the full photo if it's in the library.
        local = APP.local_files().get(iid)
        if local and local.suffix.upper() == ".JPG":
            return self.send_file(local, "image/jpeg")
        self.json({"error": "not available"}, 404)

    def media(self, iid: str, download: bool):
        path = APP.local_files().get(iid)
        item = APP.by_id(iid)
        if item and not APP.is_local(item, path):
            if not APP.cam.online:
                return self.json({"error": "Camera is offline and this file isn't on the PC yet"}, 409)
            path = APP.download(item)  # fetching for a device also saves it to the library
        if not path:
            return self.json({"error": "not found"}, 404)
        ext = path.suffix[1:].upper()
        self.send_file(path, CTYPES.get(ext, "application/octet-stream"),
                       download_name=path.name if download else None)

    def zip(self, ids: list[str]):
        local, paths = APP.local_files(), []
        for iid in ids:
            item, path = APP.by_id(iid), local.get(iid)
            if item and not APP.is_local(item, path):
                if not APP.cam.online:
                    return self.json({"error": "Camera is offline and some files aren't on the PC yet"}, 409)
                path = APP.download(item)
            if path:
                paths.append(path)
        if not paths:
            return self.json({"error": "nothing to download"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", f'attachment; filename="lumix-{time.strftime("%Y%m%d-%H%M")}.zip"')
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        with zipfile.ZipFile(self.wfile, "w", zipfile.ZIP_STORED) as z:  # JPEG/MP4 don't compress
            for p in paths:
                z.write(p, p.name)


def main():
    global APP
    APP = App()
    log(f"library {LIBRARY}, camera {APP.cam.ip}, http://0.0.0.0:{PORT}")
    threading.Thread(target=APP.watch, daemon=True).start()
    threading.Thread(target=APP.fill_dates, daemon=True).start()
    ThreadingHTTPServer.daemon_threads = True
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
