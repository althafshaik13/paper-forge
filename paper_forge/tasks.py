"""What each queued task does. Shared by the web app (tests, in-process) and the background worker process."""
from __future__ import annotations

import json
from typing import Callable

from .research import cache_path, research


def split_of(text: str) -> tuple[int, int, int]:
    e, m, h = (int(x) for x in str(text).split("/"))
    if min(e, m, h) < 0 or e + m + h != 100:
        raise ValueError("split must be three percentages that add up to 100, for example 30/50/20")
    return e, m, h


def make_task(kind: str, spec: dict, cfg, claude, tracker) -> Callable:
    """Return the function that does the work of one task. It is called with a `log(line)` function."""
    if kind == "research":
        def job(log):
            from .server import pattern_summary
            log(f"Researching {spec['company']}, {spec['role']} with {cfg.research_model} and web search ...")
            before = tracker.tokens
            p = research(cfg, claude, spec["company"], spec["role"], refresh=spec.get("refresh", False))
            log(f"Found {len(p['sections'])} sections and {len(p.get('sources', []))} sources. "
                f"Used {tracker.tokens - before:,} tokens this step")
            return pattern_summary(p)
        return job
    if kind == "generate":
        def job(log):
            from .pipeline import generate_papers
            pf = cache_path(cfg, spec["company"], spec["role"])
            if not pf.exists():
                raise FileNotFoundError("Research this company and role first")
            pattern = json.loads(pf.read_text(encoding="utf-8"))
            res = generate_papers(cfg, claude, tracker, pattern, split_of(spec["split"]), spec["papers"], log=log, **spec["opts"])
            return {"keys": [r["paper"]["paperKey"] for r in res], "needsHuman": [r["report"]["needsHuman"] for r in res]}
        return job
    raise ValueError(f"unknown task kind {kind!r}")


def process_builder(kind: str, spec: dict, ctl):
    """Used by the background worker process: a tracker and Claude client of its own for each task."""
    from dataclasses import replace
    from .claude import make_claude
    from .config import load
    from .cost import CostTracker
    cap = spec.get("cap") or {}
    cfg = load()
    cfg = replace(cfg, token_cap=int(cap.get("tokens", cfg.token_cap)), spend_cap_usd=float(cap.get("usd", cfg.spend_cap_usd)))
    tracker = CostTracker(cfg)
    tracker.gate, tracker.base_tokens = ctl.gate, ctl.base
    tracker.on_add = lambda: ctl.report(tracker.tokens)
    return make_task(kind, spec, cfg, make_claude(cfg, tracker), tracker)
