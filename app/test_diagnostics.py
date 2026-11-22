"""Unit check for the diagnostics inbox. Run: python3 app/test_diagnostics.py"""
import json, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import diagnostics
from diagnostics import Inbox


def j(o):
    return json.dumps(o).encode()


class InboxTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.folder = Path(self._tmp.name) / "diagnostics" / "inbox"
        self.inbox = Inbox(self.folder)

    def test_round_trip(self):
        rep = {"camera": {"model": "DC-G9"}, "app": {"version": "1.2"}, "requests": [1, 2, 3]}
        s = self.inbox.save(j(rep))
        self.assertTrue(s["name"].startswith("DC-G9-") and s["name"].endswith(".json"))
        self.assertEqual((s["model"], s["app"], s["requests"]), ("DC-G9", "1.2", 3))
        rows = self.inbox.list()
        self.assertEqual(rows, [s])
        self.assertEqual(json.loads(self.inbox.path(s["name"]).read_text()), rep)
        self.assertEqual(list(self.folder.glob("*.tmp")), [])

    def test_optional_fields(self):
        s = self.inbox.save(j({"x": 1}))
        self.assertEqual((s["model"], s["app"], s["requests"]), ("unknown", None, None))

    def test_model_picking_and_sanitising(self):
        for rep, want in [
            ({"camera": {"model": "A1", "modelNumber": "A2"}, "model": "A3"}, "A1"),
            ({"camera": {"modelNumber": "A2"}, "model": "A3"}, "A2"),
            ({"model": "A3", "device": {"modelNumber": "A4"}}, "A3"),
            ({"device": {"modelNumber": "A4"}}, "A4"),
            ({"camera": {"model": "../../etc/pa ss/wd"}}, None),
        ]:
            m = self.inbox.save(j(rep))["model"]
            if "camera" in rep and "../" in rep["camera"].get("model", ""):
                self.assertRegex(m, r"^[A-Za-z0-9._-]+$")
                self.assertNotIn("/", m)
            else:
                self.assertEqual(m, want)
        long = self.inbox.save(j({"model": "x" * 100}))["model"]
        self.assertEqual(len(long), 40)
        self.assertEqual(self.inbox.save(j({"model": "///"}))["model"], "unknown")

    def test_collision(self):
        names = [self.inbox.save(j({"model": "Z"}))["name"] for _ in range(3)]
        self.assertEqual(len(set(names)), 3)
        self.assertTrue(names[1].endswith("-2.json") and names[2].endswith("-3.json"))
        self.assertEqual({r["model"] for r in self.inbox.list()}, {"Z"})

    def test_rejections(self):
        for raw in (b"not json", b"\xff\xfe", b"[1,2]", b"3", b"null"):
            with self.assertRaises(ValueError):
                self.inbox.save(raw)
        old = diagnostics.MAX_BYTES
        diagnostics.MAX_BYTES = 10
        try:
            with self.assertRaises(ValueError):
                self.inbox.save(j({"model": "big"}))
        finally:
            diagnostics.MAX_BYTES = old
        self.assertEqual(self.inbox.list(), [])

    def test_list_skips_bad_and_orders_newest_first(self):
        import os
        a = self.inbox.save(j({"model": "A"}))["name"]
        b = self.inbox.save(j({"model": "B"}))["name"]
        os.utime(self.folder / a, (1000, 1000))
        (self.folder / "broken.json").write_text("{nope")
        self.assertEqual([r["name"] for r in self.inbox.list()], [b, a])

    def test_path(self):
        s = self.inbox.save(j({"model": "P"}))
        self.assertEqual(self.inbox.path(s["name"]), self.folder / s["name"])
        for bad in ("../x.json", "..\\x.json", "a/b.json", "nope.json", "x.txt", "", ".."):
            self.assertIsNone(self.inbox.path(bad))
        (Path(self._tmp.name) / "x.json").write_text("{}")
        self.assertIsNone(self.inbox.path("../x.json"))


if __name__ == "__main__":
    unittest.main()
