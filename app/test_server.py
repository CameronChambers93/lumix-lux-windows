"""End-to-end regression test: the real app/server.py against core's fakecam. Run: python3 app/test_server.py"""
import glob, io, json, os, socket, subprocess, sys, tempfile, time, unittest, urllib.error, urllib.request, zipfile
from pathlib import Path

APP = Path(__file__).resolve().parent
ROOT = APP.parent
FAKECAM = ROOT / "core" / "tools" / "fakecam.py"
DRIVER = ROOT / "core" / "drivers" / "panasonic-zs100.json"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def find_photo():
    for pat in ("/photos/P*.JPG", str(ROOT / "core" / "docs" / "media" / "*.jpg")):
        for f in sorted(glob.glob(pat)):
            with open(f, "rb") as fh:
                if b"Exif" in fh.read(70000):
                    return f
    return None


def wait_for(fn, timeout, what):
    end, last = time.time() + timeout, None
    while time.time() < end:
        try:
            last = fn()
            if last:
                return last
        except Exception as e:  # server not up yet
            last = e
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what} (last: {last!r})")


class Env(unittest.TestCase):
    """One fakecam + one server per class. Override the class attributes to vary the setup."""
    CAM_IP, CAM_PORT = "127.0.0.6", 18086
    SERVER_CAMERA = "127.0.0.6"      # LUMIX_CAMERA given to the server
    CAM_MODE, ITEMS, CAM_ENV = "ok", 30, {}
    WAIT_ONLINE = True

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        t = Path(cls.tmp.name)
        cls.data, cls.library = t / "data", t / "library"
        cls.data.mkdir(); cls.library.mkdir()
        cls.cam_log, cls.srv_log = open(t / "fakecam.log", "w+"), open(t / "server.log", "w+")
        cls.cam = subprocess.Popen(
            [sys.executable, str(FAKECAM), cls.CAM_IP, cls.CAM_MODE, str(cls.CAM_PORT), "--driver", str(DRIVER), "--items", str(cls.ITEMS)],
            env={**os.environ, **cls.CAM_ENV}, stdout=cls.cam_log, stderr=subprocess.STDOUT)
        cls.port = free_port()
        cls.srv = subprocess.Popen(
            [sys.executable, str(APP / "server.py")], stdout=cls.srv_log, stderr=subprocess.STDOUT,
            env={**os.environ, "LUMIX_PORT": str(cls.port), "LUMIX_DATA": str(cls.data), "LUMIX_LIBRARY": str(cls.library),
                 "LUMIX_CAMERA": cls.SERVER_CAMERA, "PYTHONUNBUFFERED": "1",
                 "LUMIX_PORT_MAP": f"80:{cls.CAM_PORT},60606:{cls.CAM_PORT}"})
        cls.failed = False
        try:
            wait_for(lambda: cls.cam_stats() is not None, 10, "fakecam")
            wait_for(lambda: cls.get("/api/status") is not None, 10, "server")
            if cls.WAIT_ONLINE:
                cls.wait_status(lambda s: s["camera"]["online"] and s["on_camera"] == cls.ITEMS, 20, "camera online with a catalog")
        except Exception:
            cls.dump_logs()
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        if cls.failed:
            cls.dump_logs()
        for p in (cls.srv, cls.cam):
            p.terminate()
        for p in (cls.srv, cls.cam):
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
        for f in (cls.cam_log, cls.srv_log):
            f.close()
        cls.tmp.cleanup()

    def run(self, result=None):
        before = len(result.failures) + len(result.errors) if result else 0
        super().run(result)
        if result and len(result.failures) + len(result.errors) > before:
            type(self).failed = True

    @classmethod
    def dump_logs(cls):
        for name, f in (("server", cls.srv_log), ("fakecam", cls.cam_log)):
            f.flush(); f.seek(0)
            print(f"----- {name} log -----\n{f.read()}-----", file=sys.stderr)

    # -- http helpers
    @classmethod
    def request(cls, path, body=None, method=None, raw=False):
        data = body if isinstance(body, bytes) else (None if body is None else json.dumps(body).encode())
        req = urllib.request.Request(f"http://127.0.0.1:{cls.port}{path}", data=data, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    @classmethod
    def get(cls, path):
        code, _, body = cls.request(path)
        return json.loads(body) if code == 200 else None

    @classmethod
    def post(cls, path, body=None):
        code, _, data = cls.request(path, {} if body is None else body, "POST")
        return code, json.loads(data)

    @classmethod
    def status(cls):
        return cls.get("/api/status")

    @classmethod
    def wait_status(cls, pred, timeout, what):
        """Poll /api/status (which also keeps the server holding the camera) until pred(status)."""
        return wait_for(lambda: (lambda s: s if pred(s) else None)(cls.status()), timeout, what)

    @classmethod
    def cam_stats(cls):
        try:
            with urllib.request.urlopen(f"http://{cls.CAM_IP}:{cls.CAM_PORT}/_stats", timeout=3) as r:
                return json.load(r)
        except urllib.error.HTTPError:  # busy mode answers 503 to everything: it is up, but has no stats
            return {"denied": []}

    def items(self):
        return self.get("/api/items")["items"]

    def assertNoDenied(self):
        self.assertEqual(self.cam_stats()["denied"], [], "server sent a denylisted request")

    def tearDown(self):
        self.assertNoDenied()


class CameraTests(Env):
    """Normal ZS100: status, items, dates, thumbnails, sync, media, zip."""

    def test_1_status(self):
        s = self.status()
        self.assertTrue(s["camera"]["online"], s)
        self.assertEqual(s["camera"]["driver"]["id"], "panasonic-zs100")
        self.assertTrue(s["camera"]["driver"]["matched"])
        self.assertEqual(s["camera"]["model"], "ZS100")
        self.assertIsNotNone(s["camera"]["battery"])
        self.assertEqual(s["on_camera"], 30)

    def test_2_items_and_dates(self):
        items = self.items()
        self.assertEqual(len(items), 30)
        ids = [int(i["id"]) for i in items]
        self.assertEqual(ids, sorted(ids, reverse=True), "newest first")
        self.assertEqual({i["kind"] for i in items}, {"photo", "video"})
        self.assertTrue(all(i["camera"] and not i["local"] for i in items))
        # the background filler HEADs thumbnails while the camera is held (keep polling status)
        def filled():
            self.status()
            rows = self.items()
            return rows if all(r.get("date") for r in rows) else None
        rows = wait_for(filled, 25, "all dates filled")
        by_n = {int(r["id"]) - 1070000: r["date"] for r in rows}
        for n, d in by_n.items():
            self.assertRegex(d, r"^2026-11-01T1[2-3]:\d\d:00")
        self.assertEqual(by_n[0], "2026-11-01T12:00:00")
        self.assertEqual(by_n[29], "2026-11-01T12:29:00")

    def test_3_thumbs_cached(self):
        item = next(i for i in self.items() if i["kind"] == "photo")
        for kind in ("thumb", "preview"):
            code, h, body = self.request(f"/{kind}/{item['id']}")
            self.assertEqual(code, 200)
            self.assertEqual(h["Content-Type"], "image/jpeg")
            self.assertTrue(body.startswith(b"\xff\xd8"))
            cached = list((self.data / {"thumb": "thumbs", "preview": "previews"}[kind]).iterdir())
            self.assertTrue(any(item["id"] in f.name for f in cached), (kind, cached))

    def test_4_sync(self):  # after the others alphabetically; changes the library
        self.assertEqual(self.status()["missing"], 30)
        code, body = self.post("/api/sync", {})
        self.assertEqual((code, body), (200, {"started": True}))
        s = self.wait_status(lambda s: not s["sync"]["running"] and s["library"]["count"] > 0, 30, "sync to finish")
        self.assertEqual(s["sync"]["errors"], [])
        files = {p.name: p.stat().st_size for p in self.library.iterdir() if not p.name.endswith(".part")}
        self.assertEqual(len(files), 30, sorted(files))
        for n in range(30):
            name = f"P{1070000 + n}." + ("MP4" if n % 7 == 3 else "JPG")
            self.assertEqual(files.get(name), 300000 + n * 17, name)
        self.assertEqual(self.status()["missing"], 0)
        rows = self.items()
        self.assertTrue(all(r["local"] and r["camera"] for r in rows))
        # media + zip
        a, b = rows[0], rows[1]
        code, _, data = self.request(f"/media/{a['id']}")
        self.assertEqual((code, len(data)), (200, files[a["name"]]))
        code, h, data = self.request(f"/zip?ids={a['id']},{b['id']}")
        self.assertEqual(code, 200)
        self.assertEqual(sorted(zipfile.ZipFile(io.BytesIO(data)).namelist()), sorted([a["name"], b["name"]]))
        self.assertEqual(self.request("/media/9999999")[0], 404)


class ExifTests(Env):
    """EXIF of a real photo in the library. (fakecam ignores FAKE_PHOTO when run with --driver, since its driver branch
    answers /D*.JPG first, so the camera-side Range read can't be given real EXIF here.)"""
    ITEMS, PHOTO = 5, find_photo()

    def test_exif(self):
        if not self.PHOTO:
            self.skipTest("no JPEG with EXIF found in /photos or core/docs/media")
        (self.library / "P1099999.JPG").write_bytes(Path(self.PHOTO).read_bytes())
        code, _, body = self.request("/api/exif/1099999")
        self.assertEqual(code, 200, body)
        ex = json.loads(body)
        self.assertTrue(ex.get("taken"), ex)
        self.assertTrue(ex.get("model"), ex)
        self.assertEqual(self.request("/api/exif/1070004")[0], 404)  # fake camera bytes carry no EXIF


class FindCameraTests(Env):
    CAM_IP, CAM_PORT, SERVER_CAMERA, ITEMS = "127.0.0.7", 18087, "127.0.0.250", 6
    WAIT_ONLINE = False

    def test_find(self):
        self.assertFalse(self.status()["camera"]["online"])
        code, body = self.post("/api/find")
        self.assertEqual(code, 200, body)
        self.assertTrue(body["online"], body)
        self.assertEqual(body["ip"], "127.0.0.7")
        s = self.wait_status(lambda s: s["camera"]["online"] and s["on_camera"] == 6, 15, "camera online after find")
        self.assertEqual(s["camera"]["ip"], "127.0.0.7")
        self.assertEqual(json.loads((self.data / "config.json").read_text())["camera_ip"], "127.0.0.7")


class BusyTests(Env):
    CAM_MODE, WAIT_ONLINE = "busy", False

    def test_busy(self):
        s = self.wait_status(lambda s: "another device" in (s["camera"]["error"] or ""), 15, "the busy error")
        self.assertFalse(s["camera"]["online"])


class CautiousTests(Env):
    CAM_ENV, ITEMS = {"FAKE_MODEL": "GH5"}, 8

    def test_cautious(self):
        s = self.status()
        self.assertEqual(s["camera"]["driver"]["id"], "panasonic-camcgi")
        self.assertFalse(s["camera"]["driver"]["matched"])
        self.assertEqual(len(self.items()), 8)


class DiagnosticsTests(Env):
    WAIT_ONLINE = False
    CAM_ENV = {}

    def test_inbox(self):
        code, body = self.post("/api/diagnostics", {"camera": {"model": "DC-GH5"}, "requests": []})
        self.assertEqual(code, 200, body)
        name = body["name"]
        self.assertIn(name, [r["name"] for r in self.get("/api/diagnostics")["reports"]])
        self.assertEqual(self.get(f"/api/diagnostics/{name}")["camera"]["model"], "DC-GH5")
        self.assertEqual(self.request("/api/diagnostics", b"not json", "POST")[0], 400)
        code, h, _ = self.request("/diagnostics")
        self.assertEqual(code, 200)
        self.assertIn("text/html", h["Content-Type"])


if __name__ == "__main__":
    unittest.main()
