"""Local web app: enter a company and role, follow the whole flow, review, publish, sign off, open the PR.

Binds to 127.0.0.1 only. Start with: python -m paper_forge app   (or `review`, same thing)

Safety: it can spend money and push code, so a web page you happen to visit must not be able to drive it.
Every request must carry a localhost Host header, and every POST must carry the custom header X-Paper-Forge
(which browsers do not allow another website to send without our permission).
"""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import gap as gapmod
from . import pr as prm
from . import review
from . import speech
from .config import Config, load
from .cost import CapExceeded, CostTracker
from .estimate import counts, estimate
from .jobs import Jobs
from .pdf import build_pdf
from .publish import PublishError, preview_instructions, publish
from .research import cache_path, research

STATIC = Path(__file__).parent / "static"


class State:
    """One cost tracker for the whole session, so the spending cap covers every click."""

    def __init__(self, cfg: Config, claude_factory=None, detached: bool = False):
        self.cfg = cfg
        self.tracker = CostTracker(cfg)
        self.lock = threading.Lock()
        self.detached = detached
        self.jobs = Jobs(cfg.cache_dir / "jobs", builder=self.build, detached=detached)
        self.tracker.gate = self.jobs.worker.checkpoint
        self._claude = None
        self._factory = claude_factory

    def spent_tokens(self) -> int:
        """Tokens used this session. Background tasks count their own; edits made on the review screen use the app's tracker."""
        return self.tracker.tokens + (self.jobs.tokens() if self.detached else 0)

    def build(self, kind: str, spec: dict, ctl=None):
        """Turn a task description into the function that does the work.

        In the background worker process this builds a tracker and Claude client for the task. In the app process
        (tests) it uses the app's own."""
        from .tasks import make_task
        if not self.detached:
            return make_task(kind, spec, self.cfg, self.claude(), self.tracker)
        raise RuntimeError("the app does not run tasks itself; the background worker does")

    def claude(self):
        if self._claude is None:
            if self._factory:
                self._claude = self._factory(self.cfg, self.tracker)
            else:
                from .claude import make_claude
                self._claude = make_claude(self.cfg, self.tracker)
        return self._claude

    def cap_spec(self) -> dict:
        return {"tokens": self.cfg.token_cap, "usd": self.cfg.spend_cap_usd}

    def set_token_cap(self, tokens: int) -> None:
        if not 10_000 <= tokens <= 100_000_000:
            raise ValueError("The cap must be between 10,000 and 100,000,000 tokens")
        self.cfg = dataclasses.replace(self.cfg, token_cap=int(tokens))
        self.tracker.cfg = self.cfg

    def set_cap(self, usd: float) -> None:
        if not 1 <= usd <= 1000:
            raise ValueError("The cap must be between 1 and 1000 USD")
        self.cfg = dataclasses.replace(self.cfg, spend_cap_usd=float(usd))
        self.tracker.cfg = self.cfg


def _split(s: str) -> tuple[int, int, int]:
    e, m, h = (int(x) for x in s.split("/"))
    if e + m + h != 100 or min(e, m, h) < 0:
        raise ValueError("The split must be three numbers that add up to 100")
    return e, m, h


def _need(body: dict, *names: str):
    for n in names:
        if body.get(n) in (None, ""):
            raise ValueError(f"missing {n}")


def pattern_summary(p: dict) -> dict:
    return {"company": p["company"], "role": p["role"], "fetchedAt": p.get("_fetched_at"),
            "sections": p["sections"], "sources": p.get("sources", []), "conflicts": p.get("conflicts", []),
            "problems": p.get("_problems", []), "totalMinutes": p.get("total_minutes"),
            "negativeMarking": p.get("negative_marking"), "notes": p.get("notes", "")}


def make_handler(state: State):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        # ---- plumbing
        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _host_ok(self) -> bool:
            port = self.server.server_address[1]
            return (self.headers.get("Host") or "") in (f"127.0.0.1:{port}", f"localhost:{port}")

        def _cfg(self) -> Config:
            return state.cfg

        # ---- GET
        def do_GET(self):
            if not self._host_ok():
                return self._json(403, {"error": "bad host"})
            u = urlparse(self.path)
            q = parse_qs(u.query)
            g = lambda k, d=None: (q.get(k) or [d])[0]  # noqa: E731
            cfg = self._cfg()
            try:
                if u.path == "/":
                    return self._send(200, (STATIC / "app.html").read_bytes(), "text/html; charset=utf-8")
                if u.path == "/favicon.ico":
                    return self._send(204, b"", "image/x-icon")
                if u.path == "/review":
                    return self._send(200, (STATIC / "review.html").read_bytes(), "text/html; charset=utf-8")
                if u.path == "/api/app":
                    from .claude import effective_backend
                    backend = effective_backend(cfg)
                    key = os.environ.get("ANTHROPIC_API_KEY", "")
                    cc = shutil.which("claude") is not None
                    return self._json(200, {
                        "capUsd": cfg.spend_cap_usd, "spentUsd": round(state.tracker.total, 4),
                        "capTokens": cfg.token_cap, "spentTokens": state.spent_tokens(),
                        "activeJob": state.jobs.active(),
                        "models": {"research": cfg.research_model, "generator": cfg.generator_model, "checker": cfg.checker_model},
                        "repo": str(cfg.repo), "repoOk": cfg.repo.is_dir(), "backend": backend,
                        "hasKey": bool(key) if backend == "api" else cc})
                if u.path == "/api/jobs":
                    return self._json(200, {"jobs": state.jobs.snapshot(), "spentTokens": state.spent_tokens(),
                                            "capTokens": cfg.token_cap})
                if u.path == "/api/job":
                    return self._json(200, state.jobs.get(g("id"), int(g("since", "0"))) | {"spentUsd": round(state.tracker.total, 4), "spentTokens": state.spent_tokens()})
                if u.path == "/api/gap":
                    return self._json(200, self._gap(cfg, g("company"), g("role"), g("split", "30/50/20"), g("missing", "dev"),
                                                     int(g("papers", "1")), g("coding", "1") == "1", g("media", "1") == "1",
                                                     g("writing", "1") == "1", g("web", "1") == "1"))
                if u.path == "/api/drafts":
                    return self._json(200, {"drafts": review.list_drafts(cfg), "capUsd": cfg.spend_cap_usd,
                                            "spentUsd": round(state.tracker.total, 4),
                                            "capTokens": cfg.token_cap, "spentTokens": state.spent_tokens()})
                if u.path == "/api/paper":
                    return self._json(200, review.view(cfg, g("key")))
                if u.path == "/api/pdf":
                    key = g("key")
                    paper, rep = review.load(cfg, key)
                    data = build_pdf(paper, with_answers=g("answers", "1") != "0", problems=review.coding_info(cfg, paper, rep))
                    return self._send(200, data, "application/pdf",
                                      {"Content-Disposition": f'attachment; filename="{key}-question-paper.pdf"'})
                if u.path == "/api/publish-state":
                    try:
                        rec = prm.load_record(cfg, g("ref", "latest"))
                    except prm.PrError:
                        return self._json(200, {"record": None})
                    return self._json(200, {"record": {k: rec[k] for k in ("branch", "slug", "tree", "keys", "paths", "target",
                                                                       "previousBranch", "prDraft", "prUrl")},
                                            "status": prm.status(rec)})
                self._json(404, {"error": "not found"})
            except (KeyError, ValueError, FileNotFoundError) as e:
                self._json(400, {"error": str(e)})

        def _gap(self, cfg, company, role, split_s, missing, papers, coding, media, writing, web) -> dict:
            if not company or not role:
                raise ValueError("company and role are required")
            path = cache_path(cfg, company, role)
            if not path.exists():
                raise FileNotFoundError("No research saved for this company and role yet")
            p = json.loads(path.read_text(encoding="utf-8"))
            split = _split(split_s)
            rep = gapmod.gap_report(p)
            return {"pattern": pattern_summary(p), "report": rep, "plan": gapmod.difficulty_plan(p, *split),
                    "counts": counts(p, split, missing, coding, media, writing),
                    "estimate": estimate(cfg, p, split, papers, missing=missing, coding=coding, media=media, writing=writing, web=web)}

        # ---- POST
        def do_POST(self):
            if not self._host_ok():
                return self._json(403, {"error": "bad host"})
            if self.headers.get("X-Paper-Forge") != "1" or "application/json" not in (self.headers.get("Content-Type") or ""):
                return self._json(403, {"error": "missing X-Paper-Forge header or JSON content type"})
            path = urlparse(self.path).path
            cfg = self._cfg()
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")

                if path == "/api/cap":
                    if "tokens" in body:
                        state.set_token_cap(int(body["tokens"]))
                        return self._json(200, {"capTokens": state.cfg.token_cap})
                    state.set_cap(float(body["usd"]))
                    return self._json(200, {"capUsd": state.cfg.spend_cap_usd})

                if path in ("/api/jobs/pause", "/api/jobs/resume", "/api/jobs/cancel", "/api/jobs/move"):
                    jid = str(body["id"])
                    if path.endswith("/move"):
                        state.jobs.move(jid, int(body.get("delta", -1)))
                    else:
                        getattr(state.jobs, path.rsplit("/", 1)[1])(jid)
                    return self._json(200, {"jobs": state.jobs.snapshot()})
                if path == "/api/jobs/clear":
                    state.jobs.clear_finished()
                    return self._json(200, {"jobs": state.jobs.snapshot()})

                if path == "/api/research":
                    _need(body, "company", "role")
                    company, role, refresh = body["company"].strip(), body["role"].strip(), bool(body.get("refresh"))

                    spec = {"company": company, "role": role, "refresh": refresh, "cap": state.cap_spec()}
                    return self._json(200, {"job": state.jobs.submit("research", spec, {"company": company, "role": role})})

                if path == "/api/run":
                    _need(body, "company", "role")
                    company, role = body["company"].strip(), body["role"].strip()
                    pf = cache_path(cfg, company, role)
                    if not pf.exists() and not state.jobs.has_pending("research", company=company, role=role):
                        raise FileNotFoundError("Research this company and role first")
                    split = _split(body.get("split", "30/50/20"))
                    opts = dict(missing=body.get("missing", "dev"), web=bool(body.get("web", True)),
                                round_key=body.get("round") or None, tree=body.get("tree", "university"),
                                coding=bool(body.get("coding", True)), media=bool(body.get("media", True)),
                                writing_on=bool(body.get("writing", True)))
                    if opts["missing"] not in ("dev", "replace", "drop") or opts["tree"] not in ("university", "placement"):
                        raise ValueError("bad option")
                    papers = max(1, min(10, int(body.get("papers", 1))))
                    if "capTokens" in body:
                        state.set_token_cap(int(body["capTokens"]))
                    if "capUsd" in body:
                        state.set_cap(float(body["capUsd"]))

                    spec = {"company": company, "role": role, "split": body.get("split", "30/50/20"), "papers": papers, "opts": opts,
                            "cap": state.cap_spec()}
                    return self._json(200, {"job": state.jobs.submit("generate", spec, {"company": company, "role": role, "papers": papers})})

                if path == "/api/generate":          # review screen: change difficulty of chosen questions
                    key, changes = body["key"], body["changes"]
                    if not isinstance(changes, dict) or not changes:
                        return self._json(400, {"error": "No changes to apply"})
                    with state.lock:
                        out = review.apply_changes(state.cfg, state.claude(), state.tracker, key, changes, log=lambda *_: None)
                    out["sessionSpentUsd"] = round(state.tracker.total, 4)
                    out["sessionSpentTokens"] = state.spent_tokens()
                    return self._json(200, out)

                if path == "/api/audio":
                    _need(body, "key", "text")
                    from .validate import validate_draft
                    paper, rep = review.load(cfg, body["key"])
                    r = speech.attach_urls(paper, rep, speech.parse_url_lines(body["text"]))
                    if r["unknown"] or r["badUrl"]:
                        return self._json(400, {"error": "Nothing saved. Unknown file names: " + ", ".join(r["unknown"]) +
                                                "; not a valid http(s) URL: " + ", ".join(r["badUrl"]), "result": r})
                    rep["repoValidator"] = validate_draft(cfg, paper, rep.get("tree", "university"))
                    review._save(cfg, body["key"], paper, rep)
                    return self._json(200, {"result": r, "view": review.view(cfg, body["key"])})

                if path == "/api/publish":
                    _need(body, "keys")
                    lines: list[str] = []
                    wt = body.get("worktree")
                    res = publish(cfg, list(body["keys"]), fetch=not body.get("noFetch"), worktree=Path(wt) if wt else None,
                                  commit=bool(body.get("commit")), allow_flagged=bool(body.get("allowFlagged")),
                                  allow_missing_audio=bool(body.get("allowMissingAudio")), log=lines.append)
                    return self._json(200, {"result": res, "lines": lines, "preview": preview_instructions(cfg, res)})

                if path == "/api/signoff":
                    _need(body, "ref", "role", "name")
                    if body.get("confirm") is not True:
                        return self._json(400, {"error": "A sign-off must be confirmed by the person signing."})
                    rec = prm.load_record(cfg, body["ref"])
                    s = prm.add_signoff(cfg, rec, body["role"], body["name"], body.get("note", ""))
                    return self._json(200, {"signoff": s, "blockers": prm.readiness(rec)})

                if path == "/api/pr":
                    _need(body, "ref")
                    allow = set(body.get("allow") or [])
                    lines = []
                    try:
                        done = prm.open_pr(cfg, body["ref"], lambda kind, what: kind in allow, base=body.get("base", "main"),
                                           log=lines.append)
                    except prm.PrAborted as e:
                        return self._json(200, {"stopped": str(e), "lines": lines})
                    return self._json(200, {"done": done, "lines": lines})

                self._json(404, {"error": "not found"})
            except CapExceeded as e:
                self._json(402, {"error": str(e)})
            except (PublishError, prm.PrError) as e:
                self._json(400, {"error": str(e)})
            except (KeyError, ValueError, FileNotFoundError) as e:
                self._json(400, {"error": str(e)})
            except Exception as e:  # missing key, network, API error: show it, do not crash the server
                self._json(500, {"error": f"{type(e).__name__}: {e}"})

    return H


def serve(port: int = 8765, open_browser: bool = True, cfg: Config | None = None) -> None:
    state = State(cfg or load(), detached=True)             # tasks run in a background worker that survives a restart
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(state))
    url = f"http://127.0.0.1:{port}/"
    print(f"Paper Forge at {url}  (Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
