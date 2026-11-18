"""Local web UI (D-014): `shortpost ui` serves a page on 127.0.0.1 only.

Standard library only. No login and no secrets: the page never sees a token. Every POST needs the
`X-ShortPost: 1` header and the Host header must be localhost, which blocks other web pages from
driving the server (CSRF / DNS rebinding).
"""

from __future__ import annotations

import json
import re
import threading
import traceback
import uuid
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .. import config, db, library, pipeline, preview, remove, runner

PAGE = Path(__file__).with_name("index.html")
EDITABLE_FIELDS = ("text", "title")


class App:
    """State shared by the request threads: config, drafts held in memory, background jobs."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.drafts: dict[str, pipeline.Draft] = {}
        self.jobs: dict[str, dict] = {}
        self.approve_lock = threading.Lock()  # approvals/uploads run one at a time, in click order
        self.synced = False  # set once a Sync YouTube job finished in this server run
        self.load_notes: list[str] = []  # problems found while loading saved drafts (shown in the page)
        self.load_drafts()

    # ------------------------------------------------------------ draft files (data/drafts/<id>.json)
    # Every draft is written to disk after each change, so a server restart loses nothing.
    def draft_dir(self) -> Path:
        return config.resolve(self.cfg["general"]["db_path"]).parent / "drafts"

    def save_draft(self, did: str) -> None:
        d = self.drafts.get(did)
        if d is None:
            return
        doc = {"version": 1, "id": did, "path": str(d.path), "day": str(d.day), "sha1": d.sha1, "compat": d.compat,
               "warnings": d.warnings, "only": sorted(d.only) if d.only else None, "skip": sorted(d.skip),
               "meta": d.meta, "rendered": d.rendered, "black_s": d.black_s, "pre": d.pre}
        try:
            folder = self.draft_dir()
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / f"{did}.json"
            tmp = folder / f"{did}.json.tmp"
            tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(target)  # atomic: a crash never leaves half a file
        except Exception as e:  # noqa: BLE001 - editing must keep working; the page shows the warning
            self.load_notes.append(f"could not save draft {did} to disk: {type(e).__name__}: {e}")

    def delete_draft_file(self, did: str) -> None:
        try:
            (self.draft_dir() / f"{did}.json").unlink(missing_ok=True)
        except OSError as e:
            self.load_notes.append(f"could not delete the saved draft {did}: {e}")

    def load_drafts(self) -> None:
        folder = self.draft_dir()
        if not folder.is_dir():
            return
        from .. import affiliates, media, naming

        conn = self.conn()
        for f in sorted(folder.glob("*.json")):
            try:
                doc = json.loads(f.read_text(encoding="utf-8"))
                path = Path(doc["path"])
                if db.video_by_sha1(conn, doc["sha1"]):
                    f.unlink()  # already approved and saved to the library before the restart: nothing to recover
                    continue
                if not path.is_file():
                    self.load_notes.append(f"saved draft {f.name}: the video {path.name} is no longer there (file kept in data/drafts)")
                    continue
                d = pipeline.Draft(path, naming.load_video(path, self.cfg["series"].get("detect", True)), media.probe(path),
                                   date.fromisoformat(doc["day"]), doc["sha1"], doc["compat"], doc["warnings"], affiliates.load_catalog(),
                                   set(doc["only"]) if doc["only"] else None, set(doc["skip"]), doc["meta"], doc["rendered"],
                                   doc.get("black_s", 0.0), bool(doc.get("pre")))
                self.drafts[doc["id"]] = d
            except Exception as e:  # noqa: BLE001 - one bad file must not block the others
                self.load_notes.append(f"saved draft {f.name} could not be loaded ({type(e).__name__}: {e}); file kept in data/drafts")

    def conn(self):
        return db.connect(self.cfg)  # sqlite connections are per thread

    # ------------------------------------------------------------ jobs
    def start_job(self, label: str, fn, serial: threading.Lock | None = None) -> str:
        """Run fn in a thread. With `serial`, jobs sharing that lock run one after another (a queue)."""
        jid = uuid.uuid4().hex[:8]
        job = {"id": jid, "label": label, "state": "running", "log": [], "result": None, "error": None}
        self.jobs[jid] = job

        def run():
            try:
                if serial is not None:
                    if not serial.acquire(blocking=False):
                        job["log"].append("queued: waiting for the previous upload to finish")
                        serial.acquire()
                try:
                    job["result"] = fn(lambda m: job["log"].append(m))
                finally:
                    if serial is not None:
                        serial.release()
                job["state"] = "done"
            except Exception as e:  # noqa: BLE001 - shown in the page
                job["state"] = "error"
                job["error"] = f"{type(e).__name__}: {e}"
                job["log"].append(traceback.format_exc(limit=3))

        threading.Thread(target=run, daemon=True).start()
        return jid

    # ------------------------------------------------------------ drafts
    def draft_json(self, did: str) -> dict:
        d = self.drafts[did]
        times = preview.plan_times(self.cfg, d.rendered, d.day, d.meta["seed"])
        plats = {}
        for p, r in d.rendered.items():
            plats[p] = {
                "label": preview.LABELS[p], "when": preview.fmt_time(self.cfg, times[p]),
                "title": r.get("title"), "text": r["text"], "tags": r.get("tags"),
                "related": r.get("related"), "comment": r.get("comment"), "link": r.get("link"),
                "warnings": r.get("warnings", []), "direct": r.get("direct"),
                "focus": d.meta.get("focus", {}).get(p, {}).get("keyword"),
            }
        return {
            "id": did, "file": d.path.name, "topic": d.vi.parsed.topic, "series": d.vi.parsed.series,
            "day": str(d.day), "pre": d.pre, "warnings": d.warnings, "source": d.meta.get("source"), "model": d.meta.get("model"),
            "products": [x for x in d.meta.get("core", {}).get("products", []) if x],
            "media": f"{d.info.duration_s:.0f}s {d.info.width}x{d.info.height} {d.info.vcodec}/{d.info.pix_fmt} {d.info.size_bytes / 1e6:.0f} MB",
            "platforms": plats,
        }

    def new_draft(self, path: str, hint: str = "") -> str:
        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError(path)

        def work(say):
            conn = self.conn()
            say("Probing the video and picking the day...")
            d = pipeline.prepare(self.cfg, conn, p)
            say("Writing the text with the local AI (about a minute)...")
            pipeline.generate(self.cfg, d, hint)
            did = uuid.uuid4().hex[:8]
            self.drafts[did] = d
            self.save_draft(did)
            return did

        return self.start_job(f"Draft {p.name}", work)

    def regen(self, did: str, hint: str) -> str:
        d = self.drafts[did]

        def work(say):
            say("Writing new text with the local AI...")
            pipeline.generate(self.cfg, d, hint)
            self.save_draft(did)
            return did

        return self.start_job(f"Regenerate {d.path.name}", work)

    def fix_product(self, did: str, old: str, new: str) -> int:
        """Replace a misspelt product name everywhere in the draft (case-insensitive, whole text)."""
        d = self.drafts[did]
        if not old or not new:
            raise ValueError("both names needed")
        rx = re.compile(re.escape(old), re.I)
        n = 0

        def sub(s):
            nonlocal n
            out, k = rx.subn(new, s)
            n += k
            return out

        for r in d.rendered.values():
            for f in ("text", "title", "comment"):
                if r.get(f):
                    r[f] = sub(r[f])
            if r.get("tags"):
                r["tags"] = [sub(t) for t in r["tags"]]
        core = d.meta.get("core", {})
        core["products"] = [sub(x) for x in core.get("products", [])]
        self.save_draft(did)
        return n

    def tiktok_creator(self) -> dict:
        """TikTok's own answer about the account that will receive the post (queried every time the page asks)."""
        from ..http import Http
        from ..platforms import tiktok_direct, tiktok_inbox

        http = Http()
        t = tiktok_inbox.refresh_if_needed(http, config.load_secrets())
        return tiktok_direct.creator_info(http, t["access_token"])

    def set_tiktok(self, did: str, b: dict) -> dict:
        from ..platforms import tiktok_direct

        d = self.drafts[did]
        if "tiktok" not in d.rendered:
            raise ValueError("this draft has no TikTok post")
        s = {k: (bool(b.get(k)) if k != "privacy" else (b.get(k) or None))
             for k in ("privacy", "comment", "duet", "stitch", "disclose", "your_brand", "branded", "consented")}
        if not s["disclose"]:
            s["your_brand"] = s["branded"] = False
        if b.get("clear"):
            d.rendered["tiktok"].pop("direct", None)
            self.save_draft(did)
            return {"direct": None, "problems": []}
        problems = tiktok_direct.validate_settings(s, self.tiktok_creator())
        if problems:
            raise ValueError("; ".join(problems))
        d.rendered["tiktok"]["direct"] = s
        self.save_draft(did)
        return {"direct": s, "problems": []}

    def tiktok_post(self, did: str) -> str:
        """Send this draft's video to TikTok right now with the saved Direct Post settings (no other platform, nothing saved)."""
        from ..http import Http
        from ..platforms import tiktok_inbox

        d = self.drafts[did]
        r = d.rendered.get("tiktok")
        if not r or not (r.get("direct") or {}).get("consented"):
            raise ValueError("save the TikTok settings (with consent) first")

        def run(say):
            say("Uploading to TikTok...")
            res = tiktok_inbox.upload(self.cfg, d.path, r, None, Http())
            return {"state": res.state, "id": res.remote_id, "url": res.remote_url}

        return self.start_job("TikTok " + d.path.name, run)

    def edit_draft(self, did: str, plat: str, field: str, value: str) -> None:
        if field not in EDITABLE_FIELDS:
            raise ValueError("field not editable")
        r = self.drafts[did].rendered[plat]
        if field == "title" and "title" not in r:
            raise ValueError("this platform has no title")
        r[field] = value
        self.save_draft(did)

    def handles_path(self) -> Path:
        return self.draft_dir().parent / "handles.json"

    def apply_handles(self, dids: list[str], b: dict) -> dict:
        from .. import handles

        spec = handles.spec_of(b)
        if not any(spec.values()):
            raise ValueError("enter at least one handle")
        dup = [self.drafts[x].path.name for x in dids if handles.already_there(self.drafts[x].rendered, spec)]
        if dup:
            raise ValueError("these exact handles are already added to: " + "; ".join(dup) + ". Nothing was changed.")
        out = {}
        for did in dids:
            d = self.drafts[did]
            out[d.path.name] = handles.apply(d.rendered, spec)
            self.save_draft(did)
        handles.save(self.handles_path(), handles.upsert(handles.load(self.handles_path()), b))  # remember what was used
        return out

    def tiktok_resend(self, vid: int) -> str:
        """Send this video's TikTok post again (a new Inbox draft, new push) in a background job."""
        conn = self.conn()
        row = conn.execute("SELECT id FROM posts WHERE platform='tiktok' AND video_id=?", (vid,)).fetchone()
        if not row:
            raise ValueError("this video has no TikTok post")
        if any(j["state"] == "running" and j["label"] == f"TikTok resend #{vid}" for j in self.jobs.values()):
            raise ValueError("already sending")
        with conn:
            conn.execute("UPDATE posts SET state='pending', attempts=0, next_attempt_at_utc=NULL, last_error=NULL, fire_at_utc=?, remote_id=NULL, "
                         "remote_url=NULL, extra_json='{\"resend\": true}', updated_at=? WHERE id=?", (db.now_iso(), db.now_iso(), row["id"]))
            conn.execute("UPDATE videos SET state='approved' WHERE id=?", (vid,))
            db.event(conn, vid, "tiktok", "resend", "TikTok post sent again from the UI")

        def run(say):
            say("Sending to TikTok (it can take a few minutes to process)...")
            return [x for x in runner.process(self.cfg, self.conn()) if "tiktok" in x]

        return self.start_job(f"TikTok resend #{vid}", run, serial=self.approve_lock)

    def preapprove(self, did: str) -> None:
        """Park the draft as final text: nothing is uploaded or scheduled and no day is reserved."""
        self.drafts[did].pre = True
        self.save_draft(did)

    def discard(self, did: str) -> None:
        d = self.drafts.get(did)
        if d and any(j["state"] == "running" and j["label"] == f"Regenerate {d.path.name}" for j in self.jobs.values()):
            raise ValueError("new text is being written for this draft: wait for it to finish")
        self.drafts.pop(did, None)
        self.delete_draft_file(did)

    def remove(self, vid: int) -> str:
        """Cancel the video's pending posts and delete it where it is already on a platform (background job)."""
        def work(say):
            return remove.remove_video(self.cfg, self.conn(), vid, say)

        return self.start_job(f"Remove #{vid}", work)

    def approve(self, did: str, now: bool = False) -> str:
        d = self.drafts[did]
        label = f"Approve {d.path.name}"
        if any(j["state"] == "running" and j["label"] == label for j in self.jobs.values()):
            raise ValueError("this video is already being approved: wait for the upload to finish (YouTube takes a few minutes)")

        # The draft leaves the New list at once; if the commit fails it comes back so nothing is lost.
        self.drafts.pop(did, None)  # the saved file stays until the commit succeeded

        def work(say):
            conn = self.conn()
            try:
                if not now and d.day in db.occupied_dates(conn, self.cfg):
                    # an earlier approval (or a YouTube Short) took this day while this draft waited: pick the next free one
                    old = d.day
                    pipeline.reslot(self.cfg, conn, d, synced=self.synced)
                    say(f"{old} is taken now; using {d.day}")
                vid, lib = pipeline.commit(self.cfg, conn, d, say, post_now=now)
            except Exception:
                self.drafts[did] = d
                self.save_draft(did)
                raise
            self.delete_draft_file(did)
            return vid

        return self.start_job(f"Approve {d.path.name}", work, serial=self.approve_lock)

    # ------------------------------------------------------------ queue
    def queue(self) -> dict:
        conn = self.conn()
        rows = conn.execute("SELECT * FROM videos WHERE state != 'cancelled' ORDER BY slot_date_ny DESC, id DESC LIMIT 60").fetchall()
        vids = []
        for v in rows:
            posts = []
            for p in db.posts_of(conn, v["id"]):
                when = datetime.fromisoformat(p["fire_at_utc"])
                posts.append({"id": p["id"], "platform": p["platform"], "state": p["state"], "when": preview.fmt_time(self.cfg, when),
                              "url": p["remote_url"], "error": p["last_error"], "attempts": p["attempts"],
                              "tt_open": p["platform"] == "tiktok" and p["state"] == "published" and bool((json.loads(p["extra_json"] or "{}")).get("gate")) and not json.loads(p["extra_json"] or "{}").get("user_done")})
            vids.append({"id": v["id"], "topic": v["topic"], "day": v["slot_date_ny"], "state": v["state"],
                         "part": f"{v['series_part']}/{v['series_of']}" if v["series_part"] else None, "posts": posts})
        approving = {j["label"][len("Approve "):] for j in self.jobs.values() if j["state"] == "running" and j["label"].startswith("Approve ")}
        inbox = [{"path": str(f), "name": f.name} for f in runner.inbox_candidates(self.cfg, conn, datetime.fromtimestamp(0, timezone.utc).replace(year=2100)) if f.name not in approving]
        return {"videos": vids, "inbox": inbox, "drafts": [{"id": k, "file": d.path.name, "topic": d.vi.parsed.topic, "pre": d.pre, "day": str(d.day)} for k, d in self.drafts.items()],
                "notes": self.load_notes[-5:],
                "jobs": [{**{k: j[k] for k in ("id", "label", "state", "error")}, "last": (j["log"] or [""])[-1][:160]} for j in list(self.jobs.values())[-8:]]}

    def retry(self, vid: int, plat: str | None) -> int:
        conn = self.conn()
        with conn:
            cur = conn.execute(
                "UPDATE posts SET state='pending', attempts=0, next_attempt_at_utc=NULL, last_error=NULL, updated_at=? "
                "WHERE video_id=? AND state='failed'" + (" AND platform=?" if plat else ""),
                (db.now_iso(), vid) + ((plat,) if plat else ()))
            db.event(conn, vid, plat, "retry", "from the web UI")
            db.refresh_video_state(conn, vid)
        return cur.rowcount

    def post_now(self, vid: int, plat: str | None) -> str:
        """Bypass the schedule: make every unposted local-time/inbox post due now and run the runner at once.
        Native-scheduled posts (YouTube, Facebook) already sit on the platform's own schedule and are not touched."""
        from .. import runner

        conn = self.conn()
        now = db.now_iso()
        with conn:
            cur = conn.execute(
                "UPDATE posts SET state='pending', attempts=0, next_attempt_at_utc=NULL, last_error=NULL, fire_at_utc=?, updated_at=? "
                "WHERE video_id=? AND mode != 'native' AND state IN ('pending','failed','missed')" + (" AND platform=?" if plat else ""),
                (now, now, vid) + ((plat,) if plat else ()))
            db.event(conn, vid, plat, "post_now", "from the web UI")
            db.refresh_video_state(conn, vid)
        n = cur.rowcount
        if not n:
            raise ValueError("nothing to post now (everything is posted or scheduled on the platform)")

        def work(say):
            say(f"Posting {n} platform(s) now...")
            lines = runner.process(self.cfg, self.conn())
            for ln in lines:
                say(ln)
            return lines

        return self.start_job(f"Post now #{vid}", work)

    def failures(self) -> list[dict]:
        conn = self.conn()
        rows = conn.execute("SELECT p.*, v.topic FROM posts p JOIN videos v ON v.id=p.video_id WHERE p.state IN ('failed','missed') ORDER BY p.updated_at DESC LIMIT 30").fetchall()
        return [{"video": r["video_id"], "topic": r["topic"], "platform": r["platform"], "state": r["state"], "error": r["last_error"],
                 "attempts": r["attempts"]} for r in rows]

    def log_tail(self, lines: int = 120) -> str:
        d = config.resolve(self.cfg["general"]["log_dir"])
        files = sorted(d.glob("shortpost-*.log")) if d.is_dir() else []
        if not files:
            return "(no log yet)"
        return "".join(files[-1].read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)[-lines:])

    def saved_text(self, vid: int) -> dict:
        v = db.get_video(self.conn(), vid)
        doc = json.loads(Path(v["meta_path"]).read_text(encoding="utf-8"))
        _, open_, done = _editable(self.conn(), vid)
        return {"rendered": {p: {"title": r.get("title"), "text": r["text"]} for p, r in doc["rendered"].items()}, "open": open_, "uploaded": done}

    def save_text(self, vid: int, plat: str, field: str, value: str) -> str:
        if field not in EDITABLE_FIELDS:
            raise ValueError("field not editable")
        conn = self.conn()
        v = db.get_video(conn, vid)
        open_ = _editable(conn, vid)[1]
        if plat not in open_:
            return f"{plat} is already uploaded or scheduled; change it on the platform itself."
        path = Path(v["meta_path"])
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["rendered"][plat][field] = value
        path.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
        if plat == "tiktok" and field == "text":
            (path.parent / "tiktok_caption.txt").write_text(value, encoding="utf-8")
        return "saved"


def _editable(conn, vid: int):
    posts = conn.execute("SELECT platform, state FROM posts WHERE video_id=?", (vid,)).fetchall()
    open_ = [p["platform"] for p in posts if p["state"] in ("pending", "failed")]
    done = [p["platform"] for p in posts if p["state"] not in ("pending", "failed", "cancelled", "skipped")]
    return None, open_, done


def make_handler(app: App, port: int):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # keep the console quiet
            pass

        def _send(self, code: int, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else (json.dumps(body) if ctype == "application/json" else body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _serve_video(self, vid: int):
            """Stream a library video (only by id, never a path from the page) with Range support so the player can seek."""
            row = app.conn().execute("SELECT lib_dir FROM videos WHERE id=?", (vid,)).fetchone()
            f = runner.video_path(app.cfg, Path(row["lib_dir"]), "instagram") if row and row["lib_dir"] else None
            if not f or not f.is_file():
                return self._send(404, {"error": "video file not found"})
            size, start, end, code = f.stat().st_size, 0, f.stat().st_size - 1, 200
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                    end = int(m.group(2)) if m.group(2) else end
                else:
                    start = max(size - int(m.group(2)), 0)
                end, code = min(end, size - 1), 206
            self.send_response(code)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            if code == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            with open(f, "rb") as fh:
                fh.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = fh.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)

        def _guard(self, post: bool) -> bool:
            if self.headers.get("Host", "") not in allowed_hosts:
                self._send(403, {"error": "bad host"})
                return False
            if post and self.headers.get("X-ShortPost") != "1":
                self._send(403, {"error": "missing X-ShortPost header"})
                return False
            return True

        def do_GET(self):
            if not self._guard(False):
                return
            path = self.path.split("?")[0]
            try:
                if path == "/":
                    return self._send(200, PAGE.read_text(encoding="utf-8"), "text/html")
                if path == "/api/state":
                    return self._send(200, app.queue())
                if path == "/api/failures":
                    return self._send(200, app.failures())
                if path == "/api/tiktok/creator":
                    return self._send(200, app.tiktok_creator())
                if path == "/api/handles":
                    from .. import handles
                    return self._send(200, {"handles": handles.load(app.handles_path())})
                if path == "/api/log":
                    return self._send(200, {"log": app.log_tail()})
                m = re.fullmatch(r"/api/job/(\w+)", path)
                if m:
                    return self._send(200, app.jobs[m.group(1)])
                m = re.fullmatch(r"/api/draft/(\w+)", path)
                if m:
                    return self._send(200, app.draft_json(m.group(1)))
                m = re.fullmatch(r"/api/video/(\d+)/file", path)
                if m:
                    return self._serve_video(int(m.group(1)))
                m = re.fullmatch(r"/api/video/(\d+)", path)
                if m:
                    return self._send(200, app.saved_text(int(m.group(1))))
                self._send(404, {"error": "not found"})
            except KeyError:
                self._send(404, {"error": "unknown id"})
            except Exception as e:  # noqa: BLE001
                self._send(500, {"error": f"{type(e).__name__}: {e}"})

        def do_POST(self):
            if not self._guard(True):
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                self._send(200, self._route(self.path.split("?")[0], body))
            except KeyError:
                self._send(404, {"error": "unknown id"})
            except (ValueError, FileNotFoundError) as e:
                self._send(400, {"error": str(e)})
            except Exception as e:  # noqa: BLE001
                self._send(500, {"error": f"{type(e).__name__}: {e}"})

        def _route(self, path: str, b: dict):
            if path == "/api/sync":
                def work(say):
                    lines = pipeline.sync_youtube(app.cfg, app.conn(), say)
                    app.synced = True
                    return lines
                return {"job": app.start_job("Sync YouTube", work)}
            if path == "/api/draft":
                # only files the inbox scan shows may be drafted (no arbitrary paths from a web page)
                known = {x["path"] for x in app.queue()["inbox"]}
                if b["path"] not in known:
                    raise ValueError("file is not in the inbox list")
                name = Path(b["path"]).name
                if any(d.path.name == name for d in app.drafts.values()) or any(j["state"] == "running" and j["label"] in (f"Draft {name}", f"Approve {name}") for j in app.jobs.values()):
                    raise ValueError("this video already has a draft (or one is being written)")
                return {"job": app.new_draft(b["path"], b.get("hint", ""))}
            if path in ("/api/handles/save", "/api/handles/delete", "/api/handles/apply"):
                from .. import handles
                hp = app.handles_path()
                if path.endswith("save"):
                    items = handles.upsert(handles.load(hp), b)
                    handles.save(hp, items)
                    return {"handles": items}
                if path.endswith("delete"):
                    items = handles.remove(handles.load(hp), b["id"])
                    handles.save(hp, items)
                    return {"handles": items}
                return {"result": app.apply_handles(b["drafts"], b)}
            m = re.fullmatch(r"/api/draft/(\w+)/(\w+)", path)
            if m:
                did, act = m.groups()
                if act == "regen":
                    return {"job": app.regen(did, b.get("hint", ""))}
                if act == "reslot":
                    d = pipeline.reslot(app.cfg, app.conn(), app.drafts[did], synced=app.synced)
                    app.save_draft(did)
                    return {"day": str(d.day)}
                if act == "preapprove":
                    app.preapprove(did)
                    return {"ok": True}
                if act == "approve":
                    return {"job": app.approve(did, bool(b.get("now")))}
                if act == "product":
                    return {"replaced": app.fix_product(did, b["old"], b["new"])}
                if act == "tiktok":
                    return app.set_tiktok(did, b)
                if act == "tiktok_post":
                    return {"job": app.tiktok_post(did)}
                if act == "edit":
                    app.edit_draft(did, b["platform"], b["field"], b["value"])
                    return {"ok": True}
                if act == "discard":
                    app.discard(did)
                    return {"ok": True}
            m = re.fullmatch(r"/api/video/(\d+)/(\w+)", path)
            if m:
                vid, act = int(m.group(1)), m.group(2)
                conn = app.conn()
                if act == "cancel":
                    return {"messages": library.cancel(conn, vid, b.get("platform"))}
                if act == "tiktokresend":
                    return {"job": app.tiktok_resend(vid)}
                if act == "tiktokdone":
                    return {"ok": runner.mark_tiktok_done(conn, vid)}
                if act == "removeplan":
                    return {"lines": remove.describe(conn, vid)}
                if act == "remove":
                    return {"job": app.remove(vid)}
                if act == "reschedule":
                    msg = library.reschedule(app.cfg, conn, vid, date.fromisoformat(b["date"]))
                    return {"message": msg, "ok": not msg.startswith(("Refused", "No video"))}
                if act == "postnow":
                    return {"job": app.post_now(vid, b.get("platform"))}
                if act == "retry":
                    return {"reset": app.retry(vid, b.get("platform"))}
                if act == "text":
                    return {"message": app.save_text(vid, b["platform"], b["field"], b["value"])}
            raise KeyError(path)

    return H


def serve(cfg: dict, port: int = 8780, open_browser: bool = True) -> int:
    app = App(cfg)
    class QuietServer(ThreadingHTTPServer):
        def handle_error(self, request, client_address):
            import sys

            if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):  # the browser reloaded or closed the tab mid-answer
                return
            super().handle_error(request, client_address)

    srv = QuietServer(("127.0.0.1", port), make_handler(app, port))
    url = f"http://127.0.0.1:{port}/"
    print(f"ShortPost UI on {url}  (Ctrl+C to stop; nothing is posted without your Approve click)")
    if open_browser:
        import webbrowser

        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("stopped")
    return 0
