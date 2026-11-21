from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from . import gap as gapmod
from .generate import build_coding_plan as gapmod_coding, build_plan
from .speech import build_media_plan
from .writing import build_writing_plan
from .config import ROOT, load
from .cost import CapExceeded, CostTracker
from .research import cache_path, research


def _ok(flag: bool) -> str:
    return "OK     " if flag else "MISSING"


def cmd_doctor(args) -> int:
    cfg = load()
    checks: list[tuple[bool, str, str]] = []
    checks.append((sys.version_info >= (3, 11), f"Python {sys.version.split()[0]}", "needs 3.11 or newer"))
    try:
        import anthropic
        checks.append((True, f"anthropic package {anthropic.__version__}", ""))
    except ImportError:
        checks.append((False, "anthropic package", "run: pip install -r requirements.txt"))
    from .claude import effective_backend
    kind = effective_backend(cfg)
    checks.append((True, f"backend: {kind}" + (" (your Claude plan login through Claude Code)" if kind == "claude-code" else " (Anthropic API)"), ""))
    if kind == "claude-code":
        checks.append((shutil.which("claude") is not None, "claude command (Claude Code) on PATH",
                       "install Claude Code, run `claude` once and sign in with /login"))
    else:
        checks.append((bool(os.environ.get("ANTHROPIC_API_KEY")), "ANTHROPIC_API_KEY set",
                       "set a key, or install Claude Code and use your Claude plan: set PAPER_FORGE_BACKEND=claude-code"))
    checks.append((cfg.repo.is_dir(), f"repo folder {cfg.repo}", "fix [paths] repo in forge.toml"))
    checks.append((shutil.which("git") is not None, "git on PATH", "install git"))
    for rel in ("companies/tools/validate_placement_papers.py", "scripts/verify-solutions.py",
                "scripts/build-manifest.py", "companies/AUTHORING.md"):
        checks.append(((cfg.repo / rel).is_file(), f"repo file {rel}", "wrong repo path or old checkout"))
    if cfg.repo.is_dir() and shutil.which("git"):
        r = subprocess.run(["git", "-C", str(cfg.repo), "status", "--porcelain"], capture_output=True, text=True)
        dirty = len([l for l in r.stdout.splitlines() if l.strip()])
        checks.append((True, f"repo has {dirty} uncommitted change(s)",
                       "informational: Paper Forge will cut its own branch from origin/main"))
    for model in (cfg.research_model, cfg.generator_model, cfg.checker_model):
        checks.append((model in cfg.prices, f"price known for {model}", "add it under [prices] in forge.toml"))
    bad = 0
    for ok, what, fix in checks:
        print(f"{_ok(ok)} {what}" + ("" if ok or not fix else f"   -> {fix}"))
        bad += 0 if ok else 1
    print()
    print("All good." if not bad else f"{bad} thing(s) to fix.")
    return 1 if bad else 0


def _pattern(args, cfg, need_api=True) -> dict:
    if getattr(args, "file", None):
        return json.loads(Path(args.file).read_text(encoding="utf-8"))
    path = cache_path(cfg, args.company, args.role)
    if path.exists() and not getattr(args, "refresh", False):
        print(f"(using saved research: {path.name})")
        return json.loads(path.read_text(encoding="utf-8"))
    from .claude import make_claude
    tracker = CostTracker(cfg)
    try:
        p = research(cfg, make_claude(cfg, tracker), args.company, args.role, refresh=True)
    except CapExceeded as e:
        print(e)
        raise SystemExit(2)
    print(tracker.summary())
    return p


def cmd_research(args) -> int:
    cfg = load()
    p = _pattern(args, cfg)
    print(f"\nSources ({len(p.get('sources', []))}):")
    for s in p.get("sources", []):
        print(f"  - {s.get('title', '')}  {s.get('url', '')}  {s.get('date') or ''}")
    if p.get("conflicts"):
        print("\nSources disagree on:")
        for c in p["conflicts"]:
            print(f"  - {c}")
    if p.get("_problems"):
        print("\nProblems with this pattern (check before trusting):")
        for x in p["_problems"]:
            print(f"  - {x}")
    print(f"\nSaved: {cache_path(cfg, p['company'], p['role'])}")
    return 0


def cmd_gap(args) -> int:
    cfg = load()
    p = _pattern(args, cfg)
    rep = gapmod.gap_report(p)
    plan = None
    if args.split:
        e, m, h = (int(x) for x in args.split.split("/"))
        plan = gapmod.difficulty_plan(p, e, m, h)
    print(gapmod.render(rep, plan))
    if args.dev_tasks:
        out = ROOT / "out"
        out.mkdir(exist_ok=True)
        for r in rep["missing"]:
            f = out / f"dev-task-{r['type'].lower()}.txt"
            f.write_text(gapmod.developer_task(rep, r), encoding="utf-8")
            print(f"Wrote {f}")
    return 0


def cmd_generate(args) -> int:
    from .claude import make_claude
    from .pipeline import generate_papers
    cfg = load()
    if args.no_web:
        cfg = replace(cfg, web_check=False)
    pattern = _pattern(args, cfg)
    split = tuple(int(x) for x in args.split.split("/"))
    if len(split) != 3 or sum(split) != 100:
        print("--split must be three numbers that add up to 100, for example 30/50/20")
        return 2
    plans, notes = build_plan(pattern, split, args.missing)
    n_q = sum(len(p.targets) for p in plans)
    n_write = 0 if args.no_writing else sum(len(w["items"]) for w in build_writing_plan(pattern, split))
    n_media = 0 if args.no_audio else sum(len(m["targets"]) for m in build_media_plan(pattern, split))
    n_code = 0 if args.no_coding else sum(len(c["targets"]) for c in gapmod_coding(pattern, split))
    print(f"Plan: {args.papers} paper(s), {n_q} questions + {n_code} coding problem(s) + {n_media} listening/speaking item(s) + {n_write} writing task(s) each, split {args.split}, "
          f"web check {'on' if cfg.web_check else 'off'}, cap {cfg.token_cap:,} tokens")
    for n in notes:
        print(f"  skipped: {n}")
    tracker = CostTracker(cfg)
    try:
        generate_papers(cfg, make_claude(cfg, tracker), tracker, pattern, split, args.papers, args.missing, cfg.web_check,
                        round_key=args.round, tree=args.tree, coding=not args.no_coding, media=not args.no_audio, writing_on=not args.no_writing)
    except CapExceeded as e:
        print(e)
        print(tracker.summary())
        return 2
    print()
    print(tracker.summary())
    return 0


def cmd_publish(args) -> int:
    from . import review
    from .publish import PublishError, preview_instructions, publish
    cfg = load()
    keys = review.list_drafts(cfg) if args.all else args.keys
    if not keys:
        print("Name the draft(s) to publish, or pass --all. Drafts: " + ", ".join(review.list_drafts(cfg)))
        return 2
    try:
        res = publish(cfg, keys, fetch=not args.no_fetch, worktree=Path(args.worktree) if args.worktree else None,
                      commit=args.commit, allow_flagged=args.allow_flagged,
                      allow_missing_audio=args.allow_missing_audio)
    except PublishError as e:
        print(f"Not published: {e}")
        return 1
    print(f"\nSaved on local branch {res['branch']}  (not pushed, no PR)")
    print("Staged: " + ", ".join(res["paths"]) + ("" if args.commit else "\nNot committed. Re-run with --commit when ready."))
    print(preview_instructions(cfg, res))
    return 0


def cmd_audio(args) -> int:
    from . import review
    from .speech import attach_urls, check_urls, parse_url_lines
    from .validate import validate_draft
    cfg = load()
    paper, rep = review.load(cfg, args.key)
    jobs = rep.get("audio") or []
    if not jobs:
        print("This paper has no audio files.")
        return 0
    mapping = {}
    if args.map:
        mapping.update(parse_url_lines(Path(args.map).read_text(encoding="utf-8")))
    for item in args.set or []:
        name, _, url = item.partition("=")
        mapping[name.strip()] = url.strip()
    if mapping:
        r = attach_urls(paper, rep, mapping)
        for n in r["unknown"]:
            print(f"Not a file of this paper: {n}")
        for n in r["badUrl"]:
            print(f"Not a valid http(s) URL for: {n}")
        if r["unknown"] or r["badUrl"]:
            print("Nothing was saved. Expected file names: " + ", ".join(j["file"] for j in jobs))
            return 2
        rep["repoValidator"] = validate_draft(cfg, paper, rep.get("tree", "university"))
        review._save(cfg, args.key, paper, rep)
        print(f"Filled in {len(r['applied'])} URL(s) in place of null.")
        jobs = rep["audio"]
    have = [j["url"] for j in jobs if j["url"]]
    status = check_urls(have) if (args.check and have) else {u: "not checked" for u in have}
    for j in jobs:
        print(f"{(status[j['url']] if j['url'] else 'waiting for URL'):<16} {j['file']}  ({j['kind']}, about {j['estimatedSeconds']}s)")
    pending = [j for j in jobs if not j["url"]]
    bad = [j for j in jobs if j["url"] and status[j["url"]] not in ("ok", "not checked")]
    print(f"\nHand-off for the dev team (exact text to record): {cfg.out_dir / 'audio' / args.key / 'HANDOFF.md'}")
    if pending:
        print(f"{len(pending)} of {len(jobs)} audio file(s) still null: the dev team uploads them to Wasabi and sends the URLs; "
              f"then run: audio {args.key} --map urls.json")
    elif bad:
        print(f"{len(bad)} URL(s) did not answer. Check them.")
    else:
        print("Every audio URL is filled in.")
    return 1 if pending or bad else 0


def _confirm_factory(args):
    import sys

    def confirm(kind: str, what: str) -> bool:
        if getattr(args, f"yes_{kind}", False):
            return True
        if sys.stdin.isatty():
            return input(f"\nAbout to {what}.\nType yes to do this, anything else to stop: ").strip().lower() == "yes"
        print(f"Not doing: {what}. Run in a terminal, or pass --yes-{kind} to allow exactly this step.")
        return False
    return confirm


def cmd_signoff(args) -> int:
    import sys
    from . import pr as prm
    cfg = load()
    try:
        rec = prm.load_record(cfg, args.ref)
        stmt = prm.STATEMENTS[args.role]
        print(f"Branch {rec['branch']}\n{args.role.capitalize()} {args.name!r} confirms: {stmt}")
        ok = args.yes or (sys.stdin.isatty() and input("Type yes to record this sign-off: ").strip().lower() == "yes")
        if not ok:
            print("Not recorded. Only record a sign-off after you really did this. Pass --yes to confirm in a script.")
            return 2
        s = prm.add_signoff(cfg, rec, args.role, args.name, args.note or "")
    except prm.PrError as e:
        print(f"Not recorded: {e}")
        return 1
    print(f"Recorded {args.role} sign-off by {s['name']} at {s['at']}.")
    left = prm.readiness(rec)
    print("Ready for a PR." if not left else "Still needed: " + "; ".join(left))
    return 0


def cmd_status(args) -> int:
    from . import pr as prm
    try:
        st = prm.status(prm.load_record(load(), args.ref))
    except prm.PrError as e:
        print(e)
        return 1
    print(f"Branch      {st['branch']}   (checked out: {st.get('checkedOut')}, committed: {st.get('committed')})")
    print(f"Developer   {st['signoffs']['developer'] or 'not signed off'}")
    print(f"Tester      {st['signoffs']['tester'] or 'not signed off'}")
    print(f"PR          {st['pr'] or 'not opened'}")
    print("Ready for a PR." if not st["blockers"] else "Blocked: " + "; ".join(st["blockers"]))
    return 0 if not st["blockers"] else 1


def cmd_pr(args) -> int:
    from . import pr as prm
    cfg = load()
    try:
        done = prm.open_pr(cfg, args.ref, _confirm_factory(args), base=args.base)
    except prm.PrAborted as e:
        print(e)
        return 3
    except prm.PrError as e:
        print(f"Stopped: {e}")
        return 1
    print(f"\nDone. committed={done['committed']} pushed={done['pushed']} PR: {done['prUrl']}")
    return 0


def cmd_review(args) -> int:
    from .server import serve
    serve(args.port, open_browser=not args.no_browser)
    return 0


def cmd_worker(args) -> int:
    """The background worker. The web app starts it; it keeps running when the app is restarted."""
    from .jobs import run_worker_process
    from .tasks import process_builder
    return run_worker_process(load().cache_dir / "jobs", process_builder)


def cmd_pdf(args) -> int:
    from . import review
    from .pdf import build_pdf
    cfg = load()
    paper, rep = review.load(cfg, args.key)
    out = cfg.out_dir / f"{args.key}-question-paper.pdf"
    out.write_bytes(build_pdf(paper, with_answers=not args.no_answers, problems=review.coding_info(cfg, paper, rep)))
    print(f"Wrote {out}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="paper-forge", description="Paper Forge: research, gap report, generate.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor", help="check this machine is ready").set_defaults(fn=cmd_doctor)

    r = sub.add_parser("research", help="find the latest exam pattern (uses Claude + web search, costs money)")
    r.add_argument("--company", required=True)
    r.add_argument("--role", required=True)
    r.add_argument("--refresh", action="store_true", help="ignore the saved research")
    r.set_defaults(fn=cmd_research)

    g = sub.add_parser("gap", help="show supported and missing question types")
    g.add_argument("--company")
    g.add_argument("--role")
    g.add_argument("--file", help="use a pattern JSON file instead of researching (free)")
    g.add_argument("--refresh", action="store_true")
    g.add_argument("--split", help="easy/medium/hard percentages, for example 30/50/20")
    g.add_argument("--dev-tasks", action="store_true", help="write a developer task file per missing type")
    g.set_defaults(fn=cmd_gap)

    v = sub.add_parser("app", aliases=["review"], help="open the Paper Forge web app (the whole flow, in your browser)")
    v.add_argument("--port", type=int, default=8765)
    v.add_argument("--no-browser", action="store_true")
    v.set_defaults(fn=cmd_review)

    w = sub.add_parser("worker", help=argparse.SUPPRESS)
    w.set_defaults(fn=cmd_worker)

    pf = sub.add_parser("pdf", help="write a question paper PDF for a draft without opening the review screen")
    pf.add_argument("key", help="draft paper key, as in out/<key>.json")
    pf.add_argument("--no-answers", action="store_true")
    pf.set_defaults(fn=cmd_pdf)

    so = sub.add_parser("signoff", help="record that a developer or a tester confirmed the published papers")
    so.add_argument("ref", help="branch, paper key, or 'latest'")
    so.add_argument("--role", required=True, choices=["developer", "tester"])
    so.add_argument("--name", required=True, help="the person's name")
    so.add_argument("--note", help="optional note")
    so.add_argument("--yes", action="store_true", help="confirm without asking (only after you really did the check)")
    so.set_defaults(fn=cmd_signoff)

    stt = sub.add_parser("status", help="show a published branch: sign-offs, blockers, PR")
    stt.add_argument("ref", nargs="?", default="latest")
    stt.set_defaults(fn=cmd_status)

    pp = sub.add_parser("pr", help="commit, push and open a PR, asking before EACH step. Needs both sign-offs")
    pp.add_argument("ref", nargs="?", default="latest")
    pp.add_argument("--base", default="main")
    pp.add_argument("--yes-commit", action="store_true", help="allow the commit step without asking")
    pp.add_argument("--yes-push", action="store_true", help="allow the push step without asking")
    pp.add_argument("--yes-pr", action="store_true", help="allow opening the PR without asking")
    pp.set_defaults(fn=cmd_pr)

    au = sub.add_parser("audio", help="show the audio a paper needs; --map / --set fill in the dev team's Wasabi URLs")
    au.add_argument("key")
    au.add_argument("--map", help="file with the URLs: a JSON object {file name: URL}, or lines of 'file-name URL'")
    au.add_argument("--set", action="append", metavar="FILE=URL", help="one URL, repeatable")
    au.add_argument("--check", action="store_true", help="also test that each URL answers (read-only request)")
    au.set_defaults(fn=cmd_audio)

    pb = sub.add_parser("publish", help="save approved drafts on a fresh LOCAL branch of the repo (never pushes)")
    pb.add_argument("keys", nargs="*", help="draft paper keys")
    pb.add_argument("--all", action="store_true", help="publish every draft in out/")
    pb.add_argument("--no-fetch", action="store_true", help="do not run git fetch; use the local origin/main")
    pb.add_argument("--worktree", help="build the branch in this separate folder (use when your checkout has uncommitted changes)")
    pb.add_argument("--commit", action="store_true", help="also create the local commit (default: files are staged only)")
    pb.add_argument("--allow-missing-audio", action="store_true",
                    help="publish even if some audio files are not uploaded yet (the paper will not work until they are)")
    pb.add_argument("--allow-flagged", action="store_true", help="publish even if some questions need a human")
    pb.set_defaults(fn=cmd_publish)

    n = sub.add_parser("generate", help="write new papers (costs money; stops at the spending cap)")
    n.add_argument("--company")
    n.add_argument("--role")
    n.add_argument("--file", help="use a pattern JSON file instead of saved research")
    n.add_argument("--papers", type=int, default=1)
    n.add_argument("--split", default="30/50/20", help="easy/medium/hard percentages")
    n.add_argument("--missing", choices=["dev", "replace", "drop"], default="dev",
                   help="what to do about image sections: dev task, replace with text, or leave out")
    n.add_argument("--round", help="roundKey from the company's company.json (default: its first online assessment)")
    n.add_argument("--tree", choices=["university", "placement"], default="university",
                   help="university = university-drive papers (default), placement = practice company papers")
    n.add_argument("--no-writing", action="store_true", help="skip writing sections")
    n.add_argument("--no-audio", action="store_true", help="skip listening and speaking sections")
    n.add_argument("--no-coding", action="store_true", help="skip coding sections")
    n.add_argument("--no-web", action="store_true", help="skip the web search per question (repo checks only)")
    n.set_defaults(fn=cmd_generate)

    args = ap.parse_args(argv)
    if args.cmd in ("gap", "generate") and not args.file and not (args.company and args.role):
        ap.error("gap needs --file, or both --company and --role")
    return args.fn(args)
