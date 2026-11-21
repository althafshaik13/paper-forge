"""The web app server: security, research and run jobs, gap report with estimate, publish, sign-off."""
import http.client
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from paper_forge.config import load
from paper_forge.server import State, make_handler
from test_m2 import FakeClaude
from test_publish import make_repo, sh

PATTERN = {"company": "Acme", "role": "Software Engineer", "year_or_date_seen": "2026",
           "sections": [{"name": "Quantitative", "type": "MCQ_TEXT", "questions": 5, "minutes": 10, "topics": ["percentages"], "confidence": "high"},
                        {"name": "Figures", "type": "MCQ_IMAGE", "questions": 2, "minutes": 5, "topics": [], "confidence": "low"}],
           "total_minutes": 15, "negative_marking": None, "sources": [{"url": "https://example.com/a", "title": "A", "date": None}],
           "conflicts": ["Sources disagree on the number of aptitude questions"], "notes": ""}


class AppFake(FakeClaude):
    def __init__(self, delay=0.0):
        super().__init__()
        self.delay = delay

    def ask(self, **kw):
        if self.delay:
            time.sleep(self.delay)
        if kw["label"] == "research":
            return "```json\n" + json.dumps(PATTERN) + "\n```"
        return super().ask(**kw)


@pytest.fixture
def app(tmp_path):
    cfg, repo, origin = make_repo(tmp_path)
    fake = AppFake()
    state = State(cfg, claude_factory=lambda c, t: fake)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def call(path, body=None, headers=None):
        h = {"Content-Type": "application/json", "X-Paper-Forge": "1"} if body is not None else {}
        h.update(headers or {})
        req = urllib.request.Request(base + path, None if body is None else json.dumps(body).encode(), h)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def wait(job, timeout=120):
        t0, since = time.time(), 0
        while time.time() - t0 < timeout:
            _, j = call(f"/api/job?id={job}&since={since}")
            if j["status"] not in ("running", "queued", "paused"):
                return j
            time.sleep(0.2)
        raise AssertionError("job did not finish")

    yield {"cfg": cfg, "repo": repo, "state": state, "fake": fake, "call": call, "wait": wait, "base": base, "srv": srv}
    srv.shutdown()


def test_only_localhost_and_marked_posts_are_accepted(app):
    port = app["srv"].server_address[1]
    c = http.client.HTTPConnection("127.0.0.1", port)
    c.request("GET", "/api/app", headers={"Host": "evil.example.com"})
    assert c.getresponse().status == 403
    c = http.client.HTTPConnection("127.0.0.1", port)
    c.request("POST", "/api/cap", body=json.dumps({"usd": 20}), headers={"Content-Type": "application/json"})
    assert c.getresponse().status == 403                                   # no X-Paper-Forge header
    c = http.client.HTTPConnection("127.0.0.1", port)
    c.request("POST", "/api/cap", body=json.dumps({"usd": 20}), headers={"Content-Type": "text/plain", "X-Paper-Forge": "1"})
    assert c.getresponse().status == 403                                   # not JSON
    assert app["call"]("/api/cap", {"usd": 20}) == (200, {"capUsd": 20.0})
    assert app["call"]("/api/cap", {"usd": 100000})[0] == 400


def test_status_and_pages(app, monkeypatch):
    monkeypatch.setenv("PAPER_FORGE_BACKEND", "api")                   # the API-key path, whatever forge.local.toml says
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-usr-test")
    code, a = app["call"]("/api/app")
    assert code == 200 and a["hasKey"] and "needsWorkspaceId" not in a and a["models"]["generator"] == load().generator_model
    html = urllib.request.urlopen(app["base"] + "/").read().decode()
    assert "Specialist Programmer" in html and "Start research" in html and "Lock plan and generate" in html


def test_research_then_gap_with_estimate(app):
    call, wait = app["call"], app["wait"]
    assert call("/api/gap?company=Acme&role=Software+Engineer")[0] == 400            # nothing researched yet
    code, r = call("/api/research", {"company": "Acme", "role": "Software Engineer"})
    j = wait(r["job"])
    assert code == 200 and j["status"] == "done" and j["result"]["sections"][1]["type"] == "MCQ_IMAGE"
    code, g = call("/api/gap?company=Acme&role=Software+Engineer&split=30/50/20&papers=3&missing=dev")
    assert code == 200
    assert [x["type"] for x in g["report"]["missing"]] == ["MCQ_IMAGE"] and g["pattern"]["conflicts"]
    assert g["counts"]["mcq"] == 7 and g["plan"][0]["easy"] + g["plan"][0]["medium"] + g["plan"][0]["hard"] == 5
    es = g["estimate"]
    assert es["papers"] == 3 and es["totalUsd"] == pytest.approx(es["perPaperUsd"] * 3, abs=0.02) and es["researchUsdAlreadySpent"]
    assert call("/api/gap?company=Acme&role=Software+Engineer&split=50/50/50")[0] == 400   # does not add to 100
    drop = call("/api/gap?company=Acme&role=Software+Engineer&missing=drop")[1]["counts"]["mcq"]
    assert drop == 5                                                                  # image section left out


def test_run_then_publish_and_signoff_flow(app):
    call, wait = app["call"], app["wait"]
    wait(call("/api/research", {"company": "Acme", "role": "Software Engineer"})[1]["job"])
    code, r = call("/api/run", {"company": "Acme", "role": "Software Engineer", "papers": 1, "split": "0/100/0", "missing": "drop",
                                "web": False, "coding": False, "media": False, "writing": False})
    assert code == 200
    j = wait(r["job"])
    assert j["status"] == "done", j["error"]
    key = j["result"]["keys"][0]
    assert any("saved out/" in line for line in call(f"/api/job?id={r['job']}")[1]["lines"])
    assert call("/api/drafts")[1]["drafts"] == [key] and call(f"/api/paper?key={key}")[1]["totalMarks"] == 5

    assert call("/api/publish-state")[1] == {"record": None}
    code, p = call("/api/publish", {"keys": [key]})
    assert code == 200 and p["result"]["branch"] == "feat/acme-university-papers-001" and "RESTART" in p["preview"]
    code, s = call("/api/publish-state")
    assert s["record"]["branch"] == p["result"]["branch"] and len(s["status"]["blockers"]) == 2

    assert call("/api/signoff", {"ref": "latest", "role": "developer", "name": "Asha Rao"})[0] == 400      # not confirmed
    assert call("/api/signoff", {"ref": "latest", "role": "developer", "name": "Asha Rao", "confirm": True})[0] == 200
    code, e = call("/api/signoff", {"ref": "latest", "role": "tester", "name": "asha rao", "confirm": True})
    assert code == 400 and "two different people" in e["error"]
    code, ok = call("/api/signoff", {"ref": "latest", "role": "tester", "name": "Vikram Shah", "confirm": True})
    assert code == 200 and ok["blockers"] == []
    # the PR step needs gh; with nothing allowed it asks permission for the commit and stops there
    code, pr = call("/api/pr", {"ref": "latest", "allow": []})
    assert code in (200, 400)
    if code == 200:
        assert pr.get("stopped") and sh(app["repo"], "rev-list", "--count", "main..HEAD") == "0"


def test_a_second_task_waits_in_the_queue_while_one_runs(app):
    app["fake"].delay = 0.4
    call = app["call"]
    first = call("/api/research", {"company": "Acme", "role": "Software Engineer"})
    second = call("/api/research", {"company": "Acme", "role": "Other"})
    assert first[0] == 200 and second[0] == 200 and first[1]["job"] != second[1]["job"]
    assert call(f"/api/job?id={second[1]['job']}")[1]["status"] == "queued"
    assert app["wait"](first[1]["job"])["status"] == "done"
    assert app["wait"](second[1]["job"])["status"] == "done"


def test_generate_can_be_queued_behind_its_own_research(app):
    app["fake"].delay = 0.2
    call = app["call"]
    r = call("/api/research", {"company": "Fresh", "role": "Software Engineer"})
    g = call("/api/run", {"company": "Fresh", "role": "Software Engineer", "papers": 1, "coding": False, "media": False, "writing": False})
    assert r[0] == 200 and g[0] == 200                                   # accepted: its research is queued first
    assert app["wait"](r[1]["job"])["status"] == "done"
    assert app["wait"](g[1]["job"], timeout=300)["status"] in ("done", "error")


def test_run_requires_research_first(app):
    code, e = app["call"]("/api/run", {"company": "Nobody", "role": "None"})
    assert code == 400 and "Research" in e["error"]


def _wait_status(app, jid, want, timeout=10):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if app["call"](f"/api/job?id={jid}")[1]["status"] == want:
            return
        time.sleep(0.05)
    raise AssertionError(f"job never became {want}: {app['call'](f'/api/job?id={jid}')[1]['status']}")


def test_running_job_is_reported_so_a_reloaded_page_can_pick_it_up(app):
    release = threading.Event()
    assert app["call"]("/api/app")[1]["activeJob"] is None
    jid = app["state"].jobs.submit("generate", meta={"company": "Acme", "role": "SE", "papers": 2},
                                   fn=lambda log: (log("working"), release.wait(5)))
    try:
        _wait_status(app, jid, "running")
        active = app["call"]("/api/app")[1]["activeJob"]
        assert active == {"id": jid, "kind": "generate", "meta": {"company": "Acme", "role": "SE", "papers": 2}, "status": "running"}
        assert app["call"](f"/api/job?id={jid}&since=0")[1]["lines"] == ["working"]            # the log replays from the start
    finally:
        release.set()
    _wait_status(app, jid, "done")
    assert app["call"]("/api/app")[1]["activeJob"] is None


def test_tasks_queue_up_run_in_order_and_can_be_cancelled_or_reordered(app):
    jobs, call = app["state"].jobs, app["call"]
    gate, ran = threading.Event(), []
    first = jobs.submit("research", meta={"company": "A", "role": "r"}, fn=lambda log: (gate.wait(5), ran.append("A")))
    _wait_status(app, first, "running")
    b = jobs.submit("research", meta={"company": "B", "role": "r"}, fn=lambda log: ran.append("B"))
    c = jobs.submit("research", meta={"company": "C", "role": "r"}, fn=lambda log: ran.append("C"))
    d = jobs.submit("research", meta={"company": "D", "role": "r"}, fn=lambda log: ran.append("D"))
    statuses = lambda: {j["meta"]["company"]: j["status"] for j in call("/api/jobs")[1]["jobs"]}
    assert statuses() == {"A": "running", "B": "queued", "C": "queued", "D": "queued"}
    assert call("/api/jobs/cancel", {"id": c})[0] == 200                       # remove a waiting task
    assert call("/api/jobs/move", {"id": d, "delta": -1})[0] == 200            # D now before B
    call("/api/jobs/pause", {"id": b})                                         # hold B
    assert statuses()["B"] == "held"
    gate.set()
    _wait_status(app, d, "done")
    assert ran == ["A", "D"] and statuses()["B"] == "held" and statuses()["C"] == "cancelled"
    call("/api/jobs/resume", {"id": b})
    _wait_status(app, b, "done")
    assert ran == ["A", "D", "B"]
    assert call("/api/jobs/cancel", {"id": b})[0] == 400                       # a finished task cannot be cancelled


def test_pause_and_cancel_a_running_task_at_the_next_claude_call(app):
    jobs, call, tracker = app["state"].jobs, app["call"], app["state"].tracker
    step, calls = threading.Event(), []

    def work(log):
        for i in range(50):
            tracker.check()                      # what every Claude call does first
            calls.append(i)
            log(f"call {i}")
            step.wait(0.05)

    jid = jobs.submit("generate", meta={"company": "X", "role": "r", "papers": 1}, fn=work)
    _wait_status(app, jid, "running")
    time.sleep(0.2)
    call("/api/jobs/pause", {"id": jid})
    _wait_status(app, jid, "paused")
    n = len(calls)
    time.sleep(0.4)
    assert len(calls) == n                       # nothing happens while paused
    call("/api/jobs/resume", {"id": jid})
    _wait_status(app, jid, "running")
    call("/api/jobs/cancel", {"id": jid})
    _wait_status(app, jid, "cancelled")
    assert len(calls) < 50 and "Cancelled" in call(f"/api/job?id={jid}")[1]["lines"][-1]


def test_waiting_tasks_survive_a_restart_of_the_app(tmp_path):
    from paper_forge.jobs import Jobs
    folder, built, release = tmp_path / "jobs", [], threading.Event()
    first = Jobs(folder, builder=lambda k, s, c: (lambda log: release.wait(5)))
    first.submit("research", {"n": 1}, {"company": "A"})
    time.sleep(0.3)
    first.submit("research", {"n": 2}, {"company": "B"})
    first.submit("research", {"n": 3}, {"company": "C"})
    release.set()                                      # the "old" app goes away once its task is over

    def builder(kind, spec, ctl):
        built.append(spec["n"])
        return lambda log: spec["n"]
    second = Jobs(folder, builder=builder)             # "restart": same folder, new app
    for _ in range(100):
        if {j["status"] for j in second.snapshot()} == {"done"}:
            break
        time.sleep(0.1)
    assert [j["meta"]["company"] for j in second.snapshot()] == ["A", "B", "C"]
    assert [j["status"] for j in second.snapshot()] == ["done", "done", "done"]


def test_worker_process_runs_tasks_by_itself_and_the_app_reads_the_result(tmp_path):
    """A detached worker process finishes a task that an app wrote to the queue folder, then the app (a new one) reads it."""
    import subprocess, sys, textwrap
    import paper_forge
    from paper_forge.jobs import Jobs, Store
    folder = tmp_path / "jobs"
    store = Store(folder)
    store.set_seq("t1", 1)
    store.write({"id": "t1", "kind": "research", "status": "queued", "spec": {"n": 7}, "meta": {"company": "Z"}, "result": None,
                 "error": None, "tokens": 0, "queuedAt": time.time()})          # exactly what the app writes; no worker here
    script = textwrap.dedent(f"""
        import time
        from pathlib import Path
        from paper_forge.jobs import run_worker_process
        def builder(kind, spec, ctl):
            def fn(log):
                log("started"); ctl.report(123); time.sleep(0.5); log("finished"); return spec["n"] * 2
            return fn
        raise SystemExit(run_worker_process(Path({str(folder)!r}), builder))
    """)
    root = str(__import__("pathlib").Path(paper_forge.__file__).resolve().parent.parent)
    p = subprocess.Popen([sys.executable, "-c", script], cwd=root)
    assert p.wait(timeout=60) == 0
    got = Jobs(folder).get("t1")                                                # a fresh app object, as after a restart
    assert got["status"] == "done" and got["result"] == 14 and got["tokens"] == 123
    assert got["lines"] == ["started", "finished"]
    assert not (folder / "worker.hb").exists()                                  # the worker left cleanly


def test_a_task_left_running_by_a_dead_worker_is_shown_as_interrupted_not_restarted(tmp_path):
    from paper_forge.jobs import Jobs, Store
    store = Store(tmp_path / "jobs")
    store.set_seq("r1", 1)
    store.write({"id": "r1", "kind": "generate", "status": "running", "spec": {}, "meta": {}, "result": None, "error": None,
                 "tokens": 5, "queuedAt": time.time()})
    snap = Jobs(tmp_path / "jobs").snapshot()
    assert snap[0]["status"] == "error" and "Interrupted" in snap[0]["error"]
