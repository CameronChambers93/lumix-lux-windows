"""Diagnostics inbox: camera reports sent by the phone app (POST /api/diagnostics) are stored as JSON files."""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

MAX_BYTES = 20 * 1024 * 1024
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+\.json$")


def _dig(obj, *keys):
    for k in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(k)
    return obj


def _pick_model(report: dict) -> str:
    for keys in (("camera", "model"), ("camera", "modelNumber"), ("model",), ("device", "modelNumber")):
        v = _dig(report, *keys)
        if isinstance(v, (str, int, float)) and not isinstance(v, bool) and str(v).strip():
            clean = re.sub(r"[^A-Za-z0-9._-]", "", re.sub(r"\s+", "-", str(v).strip()))[:40].strip(".")
            if clean:
                return clean
    return "unknown"


class Inbox:
    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)

    def save(self, raw: bytes) -> dict:
        if len(raw) > MAX_BYTES:
            raise ValueError("That report is too large (over %d MB)." % (MAX_BYTES // (1024 * 1024)))
        try:
            report = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ValueError("That report is not valid JSON.")
        if not isinstance(report, dict):
            raise ValueError("A report must be a JSON object.")
        self.folder.mkdir(parents=True, exist_ok=True)
        base = "%s-%s" % (_pick_model(report), time.strftime("%Y-%m-%d-%H%M%S"))
        data = json.dumps(report, ensure_ascii=False, indent=1).encode("utf-8")
        n = 1
        while True:
            name = base + (".json" if n == 1 else "-%d.json" % n)
            dest = self.folder / name
            try:
                # exclusive create reserves the name, then the content is swapped in atomically
                fd = os.open(str(dest), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                break
            except FileExistsError:
                n += 1
        tmp = self.folder / (name + ".tmp")
        try:
            tmp.write_bytes(data)
            os.replace(str(tmp), str(dest))
        except BaseException:
            for p in (tmp, dest):
                try:
                    p.unlink()
                except OSError:
                    pass
            raise
        return self._summary(dest, report)

    def _summary(self, p: Path, report: dict) -> dict:
        st = p.stat()
        app = report.get("app")
        reqs = report.get("requests")
        return {
            "name": p.name,
            "model": _model_from_name(p.name),
            "received": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
            "size": st.st_size,
            "app": app.get("version") if isinstance(app, dict) else None,
            "requests": len(reqs) if isinstance(reqs, list) else None,
        }

    def list(self) -> list:
        rows = []
        for p in self.folder.glob("*.json"):
            if not _NAME_RE.match(p.name):
                continue
            try:
                report = json.loads(p.read_text(encoding="utf-8"))
                if not isinstance(report, dict):
                    continue
                s = self._summary(p, report)
                rows.append((p.stat().st_mtime, s))
            except (OSError, ValueError):
                continue
        rows.sort(key=lambda r: (r[0], r[1]["name"]), reverse=True)
        return [s for _, s in rows]

    def path(self, name: str) -> Optional[Path]:
        if not isinstance(name, str) or not _NAME_RE.match(name):
            return None
        p = self.folder / name
        return p if p.is_file() else None


def _model_from_name(name: str) -> str:
    # <model>-YYYY-MM-DD-HHMMSS[-N].json
    m = re.match(r"^(.*)-\d{4}-\d{2}-\d{2}-\d{6}(?:-\d+)?\.json$", name)
    return m.group(1) if m else name[:-5]
