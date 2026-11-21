"""The task queue: tasks run one at a time, in a background WORKER that keeps running when the web app is restarted.

State lives in files under cache/jobs/, so the app (which only reads and writes control files) and the worker (which does
the work and writes progress) can stop and start independently:

    <id>.json   the task and its progress (written only by the worker, after it is created)
    <id>.log    one JSON line per log line
    <id>.seq    queue order
    <id>.pause / .hold / .cancel   control flags written by the app, read by the worker

A task is queued, running, paused, done, error or cancelled (a queued task can also be held). Pause and cancel take effect
before the next Claude call, so the call in progress finishes first; whatever a task already saved in out/ stays.

The worker is a separate process (`python -m paper_forge worker`) started detached, so closing or restarting the app does
not stop it. In tests it runs as a thread instead. If the whole machine restarts, a running task cannot continue: it is shown
as an error ("interrupted") and is not started again by itself, because that would repeat work and spend tokens twice.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

from .cost import CapExceeded

LIVE = ("queued", "running", "paused", "held")
HEARTBEAT_STALE = 5.0


class Cancelled(BaseException):
    """Raised inside a running task when it is cancelled. A BaseException so no `except Exception` can swallow it."""


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:6]}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class Store:
    def __init__(self, folder: Path):
        self.dir = Path(folder)
        self.dir.mkdir(parents=True, exist_ok=True)

    def p(self, jid: str, ext: str) -> Path:
        return self.dir / f"{jid}.{ext}"

    def read(self, jid: str) -> dict | None:
        for _ in range(3):                                   # a rewrite is atomic, but retry if we raced a delete
            try:
                return json.loads(self.p(jid, "json").read_text(encoding="utf-8"))
            except FileNotFoundError:
                return None
            except ValueError:
                time.sleep(0.02)
        return None

    def write(self, job: dict) -> None:
        _atomic_write(self.p(job["id"], "json"), json.dumps(job))

    def seq(self, jid: str) -> int:
        try:
            return int(self.p(jid, "seq").read_text())
        except (OSError, ValueError):
            return 0

    def set_seq(self, jid: str, n: int) -> None:
        _atomic_write(self.p(jid, "seq"), str(n))

    def ids(self) -> list[str]:
        ids = [f.name[:-5] for f in self.dir.glob("*.json") if ".tmp" not in f.name and f.name != "worker.json"]
        return sorted(ids, key=self.seq)

    def flag(self, jid: str, name: str) -> bool:
        return self.p(jid, name).exists()

    def set_flag(self, jid: str, name: str, on: bool) -> None:
        if on:
            self.p(jid, name).touch()
        else:
            self.p(jid, name).unlink(missing_ok=True)

    def add_line(self, jid: str, line: str) -> None:
        with open(self.p(jid, "log"), "a", encoding="utf-8") as f:
            f.write(json.dumps(line) + "\n")

    def lines(self, jid: str) -> list[str]:
        try:
            text = self.p(jid, "log").read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        out = []
        for ln in text.splitlines():
            try:
                out.append(json.loads(ln))
            except ValueError:
                pass                                         # a half-written last line; the next poll gets it
        return out

    def delete(self, jid: str) -> None:
        for ext in ("json", "log", "seq", "pause", "hold", "cancel"):
            self.p(jid, ext).unlink(missing_ok=True)

    # worker liveness
    def heartbeat_fresh(self) -> bool:
        try:
            return time.time() - (self.dir / "worker.hb").stat().st_mtime < HEARTBEAT_STALE
        except OSError:
            return False


class Worker:
    """Runs queued tasks one after another. `builder(kind, spec, ctl)` returns the function that does a task."""

    def __init__(self, store: Store, builder: Callable, fns: dict | None = None):
        self.store, self.builder, self.fns = store, builder, fns if fns is not None else {}
        self.current: str | None = None
        self.thread: threading.Thread | None = None

    # -- called before every Claude call (through the cost tracker) ---------------------------------------------
    def checkpoint(self) -> None:
        if self.current is None or threading.current_thread() is not self.thread:
            return
        jid, st = self.current, self.store
        paused = False
        while True:
            if st.flag(jid, "cancel"):
                raise Cancelled()
            if not st.flag(jid, "pause"):
                break
            if not paused:
                paused = True
                self._set(jid, status="paused")
                st.add_line(jid, "Paused. Resume to continue; no tokens are used while paused.")
            time.sleep(0.2)
        if paused:
            self._set(jid, status="running")
            st.add_line(jid, "Resumed.")

    def _set(self, jid: str, **fields) -> None:
        job = self.store.read(jid)
        if job:
            job.update(fields)
            self.store.write(job)

    def _next(self) -> str | None:
        for jid in self.store.ids():
            job = self.store.read(jid)
            if not job or job["status"] != "queued":
                continue
            if self.store.flag(jid, "cancel"):
                job.update(status="cancelled", finishedAt=time.time())
                self.store.write(job)
                continue
            if self.store.flag(jid, "hold"):
                continue
            return jid
        return None

    def finalize_stale(self) -> None:
        """Tasks left 'running' by a worker that no longer exists (machine restarted) cannot continue."""
        for jid in self.store.ids():
            job = self.store.read(jid)
            if job and job["status"] in ("running", "paused"):
                job.update(status="error", error="Interrupted: the worker stopped (the computer restarted). Run it again.",
                           finishedAt=time.time())
                self.store.write(job)

    def run(self, idle_passes: int = 2) -> None:
        self.thread = threading.current_thread()
        self.finalize_stale()
        idle = 0
        while True:
            jid = self._next()
            if jid is None:
                idle += 1
                if idle > idle_passes:
                    return
                time.sleep(0.3)
                continue
            idle = 0
            self._run_one(jid)

    def _run_one(self, jid: str) -> None:
        st = self.store
        job = st.read(jid)
        job.update(status="running", startedAt=time.time())
        st.write(job)
        self.current = jid
        ctl = SimpleNamespace(gate=self.checkpoint, report=lambda n: self._set(jid, tokens=n), base=self._base_tokens(jid))
        try:
            fn = self.fns.pop(jid, None) or self.builder(job["kind"], job["spec"], ctl)
            result = fn(lambda *parts: st.add_line(jid, " ".join(str(p) for p in parts)))
            update = {"status": "done", "result": result}
        except Cancelled:
            st.add_line(jid, "Cancelled. Anything already finished is saved in out/.")
            update = {"status": "cancelled"}
        except CapExceeded as e:
            update = {"status": "error", "error": str(e)}
        except Exception as e:  # noqa: BLE001  show any failure in the UI instead of dying silently
            update = {"status": "error", "error": f"{type(e).__name__}: {e}"}
        finally:
            self.current = None
        self._set(jid, finishedAt=time.time(), **update)

    def _base_tokens(self, jid: str) -> int:
        return sum((self.store.read(i) or {}).get("tokens", 0) for i in self.store.ids() if i != jid)


def run_worker_process(folder: Path, builder: Callable) -> int:
    """Entry point of the detached worker. Only one worker runs at a time (heartbeat file)."""
    store = Store(folder)
    hb = store.dir / "worker.hb"
    if store.heartbeat_fresh():
        return 0
    token = uuid.uuid4().hex
    hb.write_text(token)
    time.sleep(0.4)                                          # if two workers started together, only the last writer stays
    if hb.read_text() != token:
        return 0
    stop = threading.Event()

    def beat():
        while not stop.is_set():
            try:
                hb.write_text(token)
            except OSError:
                pass
            stop.wait(1.0)
    threading.Thread(target=beat, daemon=True).start()
    worker = Worker(store, builder)
    try:
        while True:
            worker.run()
            hb.unlink(missing_ok=True)                       # announce we are leaving, then look once more for late tasks
            if worker._next() is None:
                return 0
            hb.write_text(token)
    finally:
        stop.set()
        hb.unlink(missing_ok=True)


def spawn_worker(folder: Path) -> None:
    store = Store(folder)
    if store.heartbeat_fresh():
        return
    log = open(store.dir / "worker.log", "ab")
    kw = dict(stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True, cwd=str(Path(__file__).resolve().parent.parent))
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True                       # its own session: closing the app's terminal does not stop it
    subprocess.Popen([sys.executable, "-m", "paper_forge", "worker"], **kw)


class Jobs:
    """What the web app uses: add tasks, control them, read their state. Doing the work is the worker's job."""

    def __init__(self, folder: Path, builder: Callable | None = None, detached: bool = False):
        self.store = Store(folder)
        self.builder, self.detached = builder, detached
        self.fns: dict[str, Callable] = {}                   # in-process functions (tests only)
        self.worker = Worker(self.store, builder or (lambda *a: (_ for _ in ()).throw(ValueError("no builder"))), self.fns)
        self._thread: threading.Thread | None = None
        if any(self._status(j) == "queued" for j in self._all()):
            self._ensure_worker()                            # tasks left waiting by the last session

    def _all(self) -> list[dict]:
        return [j for j in (self.store.read(i) for i in self.store.ids()) if j]

    # -- worker ---------------------------------------------------------------------------------------------
    def _alive(self) -> bool:
        if self.detached:
            return self.store.heartbeat_fresh()
        return bool(self._thread and self._thread.is_alive())

    def _ensure_worker(self) -> None:
        if self.detached:
            spawn_worker(self.store.dir)
        elif not self._alive():
            self._thread = threading.Thread(target=lambda: self.worker.run(idle_passes=10**9), daemon=True)
            self._thread.start()

    # -- effective status (what the person sees) ----------------------------------------------------------------
    def _status(self, job: dict) -> str:
        s, jid = job["status"], job["id"]
        if s == "queued":
            if self.store.flag(jid, "cancel"):
                return "cancelled"
            return "held" if self.store.flag(jid, "hold") else "queued"
        if s in ("running", "paused") and not self._alive():
            return "error"                                   # the worker is gone
        return s

    def submit(self, kind: str, spec: dict | None = None, meta: dict | None = None, fn: Callable | None = None) -> str:
        """Add a task. `spec` (JSON) is what the worker rebuilds the task from; `fn` is a function for tests."""
        if fn is None and spec is None:
            raise ValueError("a task needs a spec or a function")
        jid = uuid.uuid4().hex[:8]
        if fn is not None:
            self.fns[jid] = fn
        self.store.set_seq(jid, time.time_ns())
        self.store.write({"id": jid, "kind": kind, "status": "queued", "spec": spec, "meta": meta or {}, "result": None,
                          "error": None, "tokens": 0, "queuedAt": time.time()})
        self._ensure_worker()
        return jid

    start = submit

    # -- controls --------------------------------------------------------------------------------------------
    def _job(self, jid: str) -> dict:
        job = self.store.read(jid)
        if not job:
            raise KeyError(f"no job {jid}")
        return job

    def pause(self, jid: str) -> None:
        st = self._status(self._job(jid))
        if st == "queued":
            self.store.set_flag(jid, "hold", True)
        elif st in ("running", "paused"):
            self.store.set_flag(jid, "pause", True)
        else:
            raise ValueError(f"A {st} task cannot be paused")

    def resume(self, jid: str) -> None:
        st = self._status(self._job(jid))
        if st == "held":
            self.store.set_flag(jid, "hold", False)
            self._ensure_worker()
        elif st in ("running", "paused"):
            self.store.set_flag(jid, "pause", False)
        else:
            raise ValueError(f"A {st} task cannot be resumed")

    def cancel(self, jid: str) -> None:
        st = self._status(self._job(jid))
        if st not in ("queued", "held", "running", "paused"):
            raise ValueError(f"A {st} task cannot be cancelled")
        self.store.set_flag(jid, "cancel", True)
        if st in ("queued", "held"):
            self._ensure_worker()                            # let the worker record it as cancelled

    def move(self, jid: str, delta: int) -> None:
        """Move a waiting task up (-1) or down (+1) among the waiting tasks."""
        if self._status(self._job(jid)) not in ("queued", "held"):
            raise ValueError("Only waiting tasks can be moved")
        waiting = [j["id"] for j in self._all() if self._status(j) in ("queued", "held")]
        k = waiting.index(jid)
        t = k + (1 if delta > 0 else -1)
        if 0 <= t < len(waiting):
            a, b = self.store.seq(waiting[k]), self.store.seq(waiting[t])
            self.store.set_seq(waiting[k], b)
            self.store.set_seq(waiting[t], a)

    def clear_finished(self) -> None:
        for j in self._all():
            if self._status(j) not in LIVE:
                self.store.delete(j["id"])

    # -- reading ---------------------------------------------------------------------------------------------
    def active(self) -> dict | None:
        """The task running right now (so a reloaded page can pick it up again), or None."""
        for j in self._all():
            if self._status(j) in ("running", "paused"):
                return {"id": j["id"], "kind": j["kind"], "meta": j["meta"], "status": self._status(j)}
        return None

    def has_pending(self, kind: str, **meta) -> bool:
        return any(j["kind"] == kind and self._status(j) in LIVE and all(j["meta"].get(k) == v for k, v in meta.items())
                   for j in self._all())

    def tokens(self) -> int:
        return sum(j.get("tokens", 0) for j in self._all())

    def snapshot(self) -> list[dict]:
        out = []
        for j in self._all():
            s, jid = self._status(j), j["id"]
            err = j["error"] or ("Interrupted: the worker stopped. Run it again." if s == "error" and j["status"] != "error" else None)
            out.append({"id": jid, "kind": j["kind"], "status": s, "meta": j["meta"], "error": err, "tokens": j.get("tokens", 0),
                        "cancelRequested": self.store.flag(jid, "cancel") and s in ("running", "paused"),
                        "pauseRequested": self.store.flag(jid, "pause") and s == "running",
                        "queuedAt": j.get("queuedAt"), "startedAt": j.get("startedAt"), "finishedAt": j.get("finishedAt")})
        return out

    def get(self, jid: str, since: int = 0) -> dict:
        job = self._job(jid)
        lines = self.store.lines(jid)
        s = self._status(job)
        err = job["error"] or ("Interrupted: the worker stopped. Run it again." if s == "error" and job["status"] != "error" else None)
        return {"id": jid, "kind": job["kind"], "status": s, "total": len(lines), "lines": lines[since:],
                "result": job["result"], "error": err, "meta": job["meta"], "tokens": job.get("tokens", 0)}
