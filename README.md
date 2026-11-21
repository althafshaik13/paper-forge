# Paper Forge

Researches a company's latest exam pattern, reports what we can and cannot generate, then writes new papers
(Claude only, no GPT). Papers are drafted in `out/`; nothing is written to the repo or pushed.

> **Status:** built and tested offline (92 tests, no API key needed). It has not yet been run against the live Claude
> API. It writes papers in the format of a `fluentedge-coding-problems` content repo (see `paper_forge/layout.py` and
> `assemble.py`); to use it on another content repo, adapt those modules. Contributions welcome: see CONTRIBUTING.md.

## The web app (easiest way to use it)
```
python -m paper_forge app
```
Opens http://127.0.0.1:8765 in your browser with the whole flow: type a company and role, research the pattern, the one
discussion (gaps and difficulty, with a cost estimate and spending cap), the live run, review and edit, then save a branch,
record the developer and tester sign-offs, and allow commit, push and PR one step at a time. It runs on your own machine only
(127.0.0.1) and checks every request, so another web page cannot drive it. Everything below is the same flow from the terminal.

## No API key? Use your Claude plan (Pro or Max)
Paper Forge has two ways to reach Claude, chosen by `[backend] kind` in `forge.toml` (default `auto`):
- **api**: the Anthropic API with `ANTHROPIC_API_KEY`. Billed per token on your Console account. A Claude.ai chat plan does NOT include API credits.
- **claude-code**: runs the `claude` command (Claude Code) with your own Claude login. No key, no API credits.
  `auto` picks this when no `ANTHROPIC_API_KEY` is set and `claude` is installed. Force it with `PAPER_FORGE_BACKEND=claude-code`.

To use it: install Claude Code, run `claude` once and sign in (`/login`), then `python -m paper_forge doctor`.
Things to know:
- Usage counts against your plan's limits, which are small on Pro. Start small: 1 paper, `--no-coding --no-audio`, `--no-web`.
  If you hit the limit the run stops with a clear message and keeps finished work; wait for the reset and run again.
- If your plan does not include Opus, map it in `forge.toml`: `[claude_code.models]` `"claude-opus-5-5" = "sonnet"`.
- Each call runs with MCP servers, skills and extra settings switched off (about 1,000 tokens of overhead instead of about 30,000).
- The dollar figures are the list-price equivalent reported by the tool, not a bill. Calls run one after another and each takes
  a few seconds longer than the API, so a full paper is slower.
- Check Anthropic's terms for scripted use of a subscription.

## Task queue (keeps running when you restart the app)
Research and Generate go into a queue on the Run page and run one at a time in a **background worker** (`python -m paper_forge worker`,
started for you). Restarting the app or reloading the page does not stop a task: reopen the app and the queue and the live log are
there. Each task has Pause, Resume, Cancel (waiting tasks can also be held and moved). Pause and cancel take effect before the next
Claude call, so the call in progress finishes first; finished work stays in `out/`. State is kept in `cache/jobs/`. If the whole
computer restarts, a task that was running shows as interrupted and is not started again by itself (it would repeat work and spend
tokens twice); tasks that were still waiting start by themselves the next time the app opens.

## Setup (once)
1. Python 3.12: `winget install Python.Python.3.12`
2. `python -m venv .venv` then `.venv\Scripts\pip install -r requirements.txt`
3. Set `ANTHROPIC_API_KEY` (own key, billed to Anthropic), or skip it and use your Claude plan through Claude Code
   (see "No API key?" above).
4. Check `forge.toml` (repo path, models, prices, spending cap).
5. `.venv\Scripts\python -m paper_forge doctor`

## Use
```
python -m paper_forge research --company Virtusa --role "Software Engineer"
python -m paper_forge gap --company Virtusa --role "Software Engineer" --split 30/50/20 --dev-tasks
python -m paper_forge generate --company Virtusa --role "Software Engineer" --papers 3 --split 30/50/20 --missing dev
```
`generate` options: `--no-web` (skip the per-question web search), `--round <roundKey>`, `--missing dev|replace|drop`.

## What `generate` does for each paper
1. Writes questions section by section (Opus 5.5), told to change every value, name and scenario.
2. Free checks: structure, and similarity to every question already in the repo (also catches same story, new numbers).
3. One web search per question that passed (Haiku 5.5).
4. Blind difficulty rating by written rules (Haiku 5.5). A mismatch triggers a rewrite, up to 3 attempts.
5. Shuffles options, builds the paper JSON, runs the repo's own validator on a throwaway copy.
6. Saves `out/<paperKey>.json`, `.report.json` (evidence per question and cost) and, for image sections, `.pending.json`.

## Review, PDF, save to a branch
```
python -m paper_forge review                      # edit difficulty per question, regenerate, download PDF
python -m paper_forge publish <paperKey> [...]    # save on a fresh LOCAL branch cut from origin/main
```
`publish` never pushes and never opens a PR. Files are staged, not committed, unless you pass `--commit`.
If your checkout has uncommitted changes to tracked files it refuses; pass `--worktree <folder>` to build the
branch in a separate folder instead. It also refuses drafts with questions that need a human (`--allow-flagged`).
University-drive papers go to `companies/university-exams/<folder>/` (`--tree placement` when generating for the
practice catalog). It does not invent companies or rounds. Image sections become a developer task note under `tasks/`.

**Preview:** point `app.problemset.path` at the branch folder, RESTART the backend (the company catalog is read once
at startup), start bet-apps. A developer and a tester confirm; only then push and open the PR yourself.

## Coding problems
`generate` also writes the pattern's CODING sections (`--no-coding` skips them). For each problem Claude writes a
statement, a Python reference, a brute force, a test generator and starter stubs. Scripts then prove it:
test outputs come from running the reference (never from the model's words); the brute force must agree with the
reference on every small input; for MEDIUM and HARD the brute force must time out at maximum size (so the
constraints force the intended technique); statement similarity vs every problem in the repo; one web search;
a blind difficulty rating; then the repo's own `verify-solutions.py` and `build-manifest.py` run on the folder.
Problems are written to `out/problems/<key>/` with `status: REVIEW` (a reviewer flips them to ACTIVE) and the paper
references them by slug. `publish` copies them into `problems/`, re-verifies, regenerates `manifest.json` and stages it.
SAFETY: the model-written reference, brute force and generator run on THIS machine as plain Python subprocesses with
timeouts, no sandbox. Run Paper Forge only on a machine where that is acceptable.
Only stdin/stdout problems are generated (no SQL or WEB problems yet).

## Listening and speaking (audio made by the dev team)
`generate` also writes listening-comprehension recordings (up to 3 questions each, split into one section per recording as
the player needs) and speaking sections (read aloud, listen and repeat, spoken response). Claude cannot make audio, so:
1. Every audio field in the paper starts as `null`.
2. Paper Forge writes the hand-off for the dev team in `out/audio/<paperKey>/`: `HANDOFF.md` (checklist), one `.txt` per
   recording with the exact TEXT TO SPEAK, speakers and file name, and `urls.json` (file name -> URL, all null).
3. The dev team produces the MP3s, uploads them to Wasabi and sends back the URLs (fill `urls.json`, or lines of `file-name URL`).
4. `python -m paper_forge audio <paperKey> --map urls.json` (or `--set file.mp3=https://...`) replaces the nulls with the URLs,
   checks the paper again and saves. Add `--check` to confirm each URL answers.
5. `publish` refuses while any audio URL is still null (`--allow-missing-audio` overrides).
Listening questions and speaking items are read-only in the review screen for now (a rewrite would lose the audio).
Skip with `--no-audio`.

## Writing
`generate` also writes WRITING sections (email, essay, letter, memo, report; skip with `--no-writing`). Each task has a
rubric of 3 lines and a model answer. A script checks that every model answer really fits its word limit (a too-long
answer is sent back for repair). Word limits and marks come from the researched pattern when it states them, otherwise
Easy 60-90 words / 5 marks, Medium 90-120 / 10, Hard 150-190 / 15 (the Capgemini mocks' values). There is no answer
key: the platform scores the candidate's text with the Writing Lab engine, so `correctAnswer` is null.
Writing tasks are read-only in the review screen for now.

## Sign-off and PR
After `publish`, a developer previews the branch in the running backend and frontend and a tester tests it. Then:
```
python -m paper_forge signoff latest --role developer --name "Asha Rao"
python -m paper_forge signoff latest --role tester    --name "Vikram Shah"
python -m paper_forge status
python -m paper_forge pr latest
```
- A sign-off records who, when, and a hash of the papers. It must be two different people. Changing the papers afterwards
  makes both sign-offs stale and they must be given again.
- `pr` refuses until both are valid, the branch is checked out, and `gh` is logged in.
- It asks before EACH step: the commit, the push, and opening the PR (type `yes`; in scripts pass `--yes-commit`,
  `--yes-push`, `--yes-pr`). Say no at any step and nothing after it happens.
- It never pushes to a branch whose PR is already merged. If a PR is already open for the branch it updates that one and
  does not open a second.
- The PR body is the draft with both sign-offs ticked and named. Nothing is merged automatically.

## Not built yet
SQL and web problems, editing coding/audio/speaking/writing items in the review screen.

## Tests
`.venv\Scripts\python -m pytest -q` (no API key or network needed)
