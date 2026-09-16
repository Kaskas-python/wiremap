# wiremap v2 — adoption (2026-09-15)

Decision (user, 2026-09-15, after the intelligent debate in
`~/Documents/wiremap_notes/debates/2026-09-15-adoption/`): keep wiremap, move it to where an
agent cannot skip it, fix the tool before asking anyone to trust it. Shape R with three
amendments: the candidate-list caveat ships first, `impact` ships without signature-change
detection, the PostToolUse hook is switched off while Steps 1–2 are measured.

## Design

- **Tool first.** `pack` keeps definitions, names what each caller calls, budgets per file,
  stamps the dirty tree, shows full signatures; `related` is deleted. `callers` greps the bare
  name and lists every hit the graph has no edge for. New `impact --base <ref>` lists cross-file
  callers of changed symbols that are outside the diff, production first, tests collapsed.
  `triage` sees untracked files. `wiremap hook` with no subcommand means `post-edit`.
- **Flow.** Implementers no longer load the skill; their brief carries Fable's assertion lines
  above a pasted `pack`. Every review brief carries a pasted `impact`. Researcher unchanged.
- **Hook off** during measurement (user edit to settings.json). A gated, deduped, diff-aware
  version is a later plan, only if the counters move.
- **Measurement.** `~/.claude/hooks/wiremap-metrics.py` counts from the session transcripts at
  SessionEnd; baseline row from the copied 9bafd3b0 transcripts.
- **Out of scope (YAGNI):** signature-change detection in `impact`, brief-lint PreToolUse hook,
  `deps --min-confidence`, `summarize`, decorators in skeletons, a graph-sha stamp, MCP.

Trade-off accepted: "real CLI calls" measures compliance, not value; value shows only in
review findings and in BLOCKED reports citing a pack line, read by hand after one feature.

## Pre-mortem

Flow: Fable runs `pack` → pastes under assertion lines → implementer reads, STOP rule →
reviewer brief carries `impact` → reviewer checks each listed caller → SessionEnd counter.

1. **Order & repetition** — pack run before the tree is final: the stamp's modified/untracked
   counts differ when the implementer checks → false STOP. *Accepted:* Fable runs `pack` last,
   right before sending; a STOP on drift is the tripwire working.
2. **Boundaries** — 6+ files in one pack starve each file below one row. *Accepted:* briefs
   pack ≤5 files (task sizing already caps a task at 2–5 min). Common bare names (`get`) in
   `callers` grep → thousands of hits. *Covered:* count + 3 rows, 30 s timeout (`GREP_TIMEOUT`).
3. **Absence** — file with no symbols → empty skeleton, no marker; symbol with no callers →
   no caller rows; `impact` with no changes → "no changes". *Covered, untested* (existing
   `test_callers_none_is_explicit` covers the callers case).
4. **Error paths** — `git status` fails on a repo with no commits → `head_stamp` returns
   "no-commit" before status runs. `git grep` rc>1 → one line "name hits: unavailable".
   *Covered, untested.*
5. **Concurrency** — none new; cache writes stay temp+rename.
6. **Contracts at the seam** — pack row format changes (`caller: dst <- src  file:line`),
   the STOP sentence changes (the metrics script keys on it), `callers()` gains a `root`
   argument, `head_stamp` gains drift counts (consumers split on whitespace: `status` takes
   `[0]`, still the sha). *Covered:* A6 updates README/SKILL/CLAUDE.md; A1 updates the cli call.
7. **Existing callers** — `pack` was the only caller of `callers()` (`commands.py:230`);
   `triage`'s diff parsing moves into `_changed` and `triage` keeps its output;
   `Symbol.signature` consumers: `skeleton`, `pack`, `export`, `graph` (longer strings, no
   contract). Signature change invalidates every cache entry → *Covered:* SCHEMA bump in A2
   (one cold parse, ≈2 s on TMS).
8. **Effects & time** — `pack` +1 `git status` (~30 ms); `callers` +1 `git grep` (~50 ms);
   `impact` ≈ triage cost. *Accepted.*

## Test budget

One file, `tests/test_wiremap.py`; extend existing tests only. Fixture additions in
`tests/conftest.py`: `app/dispatch.py` (string dispatch), a wrapped `def` in `app/svc.py`.
Test lines added ≤ 25 (production ≈ 150).

| test | asserts |
|---|---|
| `test_callers_none_is_explicit` (extend) | `callers send_mail` prints `name hits without an edge: 1` and `app/dispatch.py:2` |
| `test_pack_fits_15_lines` (rewrite body, same name) | `pack --files app/db.py app/svc.py`: ≤15 lines, `HEAD ` first, STOP last, `caller: get_db <- app.api.list_orders` present, `def load( o: Order, ):` present, no `… and` |
| `test_triage_lists_changed_callers` (extend) | `impact --base HEAD` lists `get_db <- app.api.list_orders` and the word `candidates`; after writing untracked `app/new.py`, triage's `changed:` count is strictly larger |
| `test_hook_post_edit_capped` (extend) | `wiremap hook` (no subcommand) exits 0 |

`wiremap-metrics.py` has no pytest: its check is one run over the baseline copy (D2) whose
row must read `cli_calls 0, briefs 25, tool_packs 0, impact_briefs 0, hook_firings 39`.

## Phase A — tool (`~/projects/wiremap`, branch `main`, HEAD 76541f9)

Verification checkpoint: `uv run pytest -q` → 14 passed, 0 skipped, then a live run of every
changed command against `~/projects/transport-management-system` (read-only) pasted in the report.

### A1 — `callers` lists name hits without an edge; dynamic patterns extended
Files: `src/wiremap/commands.py`, `src/wiremap/cli.py`, `tests/conftest.py`, `tests/test_wiremap.py`.
Behaviour: `wiremap callers <symbol>` ends with `unresolved: N` (unchanged) followed by
`name hits without an edge: k` and up to 5 rows `  file:line: text`, production files before
test files (`_is_test`, defined in A2); hits in the symbol's own file, and on lines that already
carry an edge to it, are excluded. (Amended 2026-09-16: cap 3 → 5, production first, after the
TMS run showed 5 production `send_task` sites.)

```python
_DYNAMIC = ("getattr(", "importlib", "globals()[", "send_task(", "apply_async(", "setattr(")


def _name_hits(g: Graph, root: Path, symbol: str) -> list[str]:
    # ponytail: string dispatch is invisible to the parser, so the bare name is
    # grepped and every hit without an edge is listed; upgrade: none wanted
    s = g.symbols[symbol]
    known = {(e.file, e.line) for e in g.edges if e.dst == symbol}
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "grep", "-n", "-I", "-w", "-F", "-z",
             "--untracked", "-e", s.name, "--"],
            capture_output=True, encoding="utf-8", errors="replace",
            timeout=GREP_TIMEOUT, check=False,
        )
    except subprocess.TimeoutExpired:
        return ["name hits: grep timed out"]
    if proc.returncode > 1:
        return ["name hits: unavailable"]
    rows = []
    for row in proc.stdout.split("\n"):
        parts = row.split("\0", 2)
        if len(parts) == 3 and parts[1].isdigit() and parts[0] != s.file:
            if (parts[0], int(parts[1])) not in known:
                rows.append(f"  {parts[0]}:{parts[1]}: {parts[2].strip()[:80]}")
    return [f"name hits without an edge: {len(rows)}"] + rows[:3]
```
In `callers(g, root, symbol, depth=1, min_conf="INFERRED")` (new `root` parameter, second
position) the final line becomes:
```python
    return "\n".join(body + [f"unresolved: {unresolved}"] + _name_hits(g, root, ids[0])), 0
```
`cli.py:201` → `callers(g, root, args.symbol, args.depth, args.min_confidence.upper())`.
Fixture (`tests/conftest.py` FILES): `"app/dispatch.py": 'def fire():\n    return send("app.tasks.send_mail")\n',`.
Test: extend `test_callers_none_is_explicit` with
`out, _, _ = run(capsys, "--repo", str(repo), "callers", "send_mail")` and
`assert "name hits without an edge: 1" in out and "app/dispatch.py:2" in out`.
Untouched: `deps`, `_find`, the `unresolved` semantics, the hook.
Verify: `uv run pytest -q`; live `wiremap callers app.celery_tasks.negotiation.negotiation.send_sibling_withdrawal`
on TMS shows 4 name hits (the `send_task` sites).

### A2 — `pack` rework, full signatures, tree-aware stamp
Files: `src/wiremap/commands.py`, `src/wiremap/parse.py`, `src/wiremap/discover.py`,
`tests/conftest.py`, `tests/test_wiremap.py`.

`discover.py` — replace `head_stamp`:
```python
def head_stamp(root: Path) -> str:
    try:
        sha = _git(root, "rev-parse", "--short", "HEAD")
    except subprocess.CalledProcessError:
        return "no-commit"
    branch = _git(root, "branch", "--show-current")
    rows = _git(root, "status", "--porcelain", "--untracked-files=all").splitlines()
    unt = sum(r.startswith("??") for r in rows)
    mod = len(rows) - unt
    drift = (f" +{mod} modified" if mod else "") + (f" +{unt} untracked" if unt else "")
    return f"{sha} {branch or '(detached)'}{drift}"
```

`parse.py` — signature is the whole span before the body, whitespace collapsed (replace the
`signature=n.text.decode().splitlines()[0]` field at `parse.py:208`):
```python
def _signature(n: Node) -> str:
    body = n.child_by_field_name("body")
    text = n.text[: body.start_byte - n.start_byte] if body else n.text
    return " ".join(text.decode(errors="replace").split())[:160]
```
and `signature=_signature(n),`. Add `+ "sig:span"` to the `SCHEMA` string (`parse.py:303-311`)
so cached entries re-parse.

`commands.py` — replace `STOP`, `pack`; delete `_ask_rows` use in pack (keep `_ask_rows` for `ask`):
```python
from itertools import zip_longest

STOP = (
    "Callers are candidates (string dispatch is invisible). "
    "If this pack contradicts the code or the stamp differs, STOP and report BLOCKED."
)
PACK_LINES = 15


def _is_test(file: str) -> bool:
    # ponytail: path heuristic — the graph has no test concept; upgrade: none
    return any(p.startswith("test") for p in Path(file).parts) or file.endswith(
        (".spec.ts", ".test.ts", "_test.go")
    )


def _fit(sections: list[list[str]], hints: list[str], budget: int) -> list[str]:
    kept = [list(s) for s in sections]

    def markers() -> int:
        return sum(len(k) < len(s) for k, s in zip(kept, sections))

    for k in reversed(kept):
        while k and sum(map(len, kept)) + markers() > budget:
            k.pop()
    out: list[str] = []
    for k, s, h in zip(kept, sections, hints):
        out += k
        if len(k) < len(s):
            out.append(f"… {len(s) - len(k)} more {h}")
    return out


def pack(
    g: Graph, root: Path, files: list[str], task: str | None = None
) -> tuple[str, int]:
    rels = []
    for p in files:
        rel = _rel(root, p)
        if rel is None:
            return f"not found: {p}", 1
        rels.append(rel)
    rels = list(dict.fromkeys(rels))
    head = [f"HEAD {head_stamp(root)}"] + ([f"task: {task}"] if task else [])
    fl = [f"files: {', '.join(rels)}"]
    syms = {f: _symbols_in(g, f) for f in rels}
    owner = {s.id: f for f, ss in syms.items() for s in ss}
    ep = sorted(
        f"entrypoint: {e.dst}  {e.kind}  {e.file}:{e.line}"
        for e in g.edges
        if e.kind in ("route", "task") and e.dst in owner
    )
    ca_files, sk_files = [], []
    for f, ss in syms.items():
        rows = sorted(
            (
                _is_test(e.file),
                f"caller: {g.symbols[e.dst].name} <- {e.src}  {e.file}:{e.line}",
            )
            for e in g.edges
            if owner.get(e.dst) == f
            and e.confidence == "EXTRACTED"
            and e.kind not in ("mentions", "imports")
            and e.file != f
        )
        ca_files.append([r for _, r in rows])
        sk_files.append(["  " * s.qualname.count(".") + s.signature for s in ss])
    ca = [r for rows in zip_longest(*ca_files) for r in rows if r]
    sk = [r for rows in zip_longest(*sk_files) for r in rows if r]
    body = _fit(
        [ep, sk, ca],
        [
            "entry points",
            f"definitions: wiremap skeleton {' '.join(rels)}",
            "callers (tests last): wiremap callers <symbol id>",
        ],
        PACK_LINES - len(head) - len(fl) - 1,
    )
    return "\n".join(head + fl + body + [STOP]), 0
```
Implementer note: confirm the import-edge kind name in `resolve.py` (`imports` assumed) and
that `Symbol` rows for the same file keep source order (`_symbols_in` sorts by `line_start`).
Fixture: replace `app/svc.py` with
`"from app.db import Order\ndef load(\n    o: Order,\n):\n    return o.total()\n"`.
Test: rewrite the body of `test_pack_fits_15_lines`:
```python
    out, _, _ = run(capsys, "--repo", str(repo), "pack", "--files", "app/db.py", "app/svc.py")
    lines = out.rstrip().splitlines()
    assert len(lines) <= 15 and lines[0].startswith("HEAD ") and "STOP" in lines[-1]
    assert "caller: get_db <- app.api.list_orders" in out and "def load( o: Order, ):" in out
    assert "… and" not in out
```
Untouched: `callers`, `skeleton`, `entrypoints`, `ask`, `report`, the hook.
Verify: `uv run pytest -q`; live on TMS: `pack --files app/services/load_cancellation.py`
shows definitions with parameters and no `… more definitions` unless callers were shown first;
`pack --files app/routers/loads.py` shows `entrypoint:` rows; stamp shows `+N modified +M untracked`.

### A3 — `impact --base <ref>`; `triage` sees untracked files
Files: `src/wiremap/commands.py`, `src/wiremap/cli.py`, `tests/test_wiremap.py`.
```python
def _changed(g: Graph, root: Path, base: str) -> set[str] | None:
    try:
        _git(root, "rev-parse", "--verify", "--quiet", base)
    except subprocess.CalledProcessError:
        return None
    try:
        diff = _git(root, *_DIFF_OPTS, f"{base}...HEAD", "--")
    except subprocess.CalledProcessError:
        # ponytail: the ref exists but shares no merge base (orphan branch), so
        # there is no range to diff; upgrade: none — the working tree still counts
        diff = ""
    diff += "\n" + _git(root, *_DIFF_OPTS, "HEAD", "--")
    untracked = set(
        _git(root, "ls-files", "--others", "--exclude-standard").splitlines()
    )
    syms_by_file: dict[str, list[Symbol]] = defaultdict(list)
    for s in g.symbols.values():
        syms_by_file[s.file].append(s)
    changed = {s.id for f in untracked for s in syms_by_file.get(f, [])}
    f = None
    for l in diff.splitlines():
        if l.startswith("+++ "):
            f = l[6:] if l.startswith("+++ b/") else None
        elif l.startswith("@@") and f and (m := _HUNK.match(l)):
            a = int(m[1])
            b = a + max(int(m[2] or 1), 1) - 1
            changed |= {
                s.id
                for s in syms_by_file.get(f, [])
                if s.line_start <= b and a <= s.line_end
            }
    return changed


IMPACT_MAX = 1000
CANDIDATES = (
    "Callers are candidates: string dispatch is invisible — "
    "grep the bare name of every changed public symbol."
)


def impact(g: Graph, root: Path, base: str = "main") -> tuple[str, int]:
    changed = _changed(g, root, base)
    if changed is None:
        return f"unknown --base {base}", 2
    if not changed:
        return "no changes", 0
    if len(changed) > IMPACT_MAX:
        return (
            f"changed: {len(changed)} symbols, more than {IMPACT_MAX}; "
            "pass the branch's real --base",
            2,
        )
    hits = sorted(
        (
            e.confidence != "EXTRACTED",
            _is_test(e.file),
            f"{g.symbols[e.dst].name} <- {e.src}  {e.file}:{e.line}",
        )
        for e in g.edges
        if e.dst in changed
        and e.src not in changed
        and e.kind not in ("mentions", "imports")
    )
    fact = [r for c, t, r in hits if not c and not t]
    tests = sum(1 for c, t, _ in hits if not c and t)
    leads = [f"unconfirmed (name-only): {r}" for c, _, r in hits if c]
    body = _cap(fact) or ["no cross-file callers outside the diff"]
    body += [f"+ {tests} test callers"] if tests else []
    return (
        "\n".join(
            [f"HEAD {head_stamp(root)}", f"changed: {len(changed)} symbols vs {base}"]
            + body
            + _top(leads, 5)
            + [CANDIDATES]
        ),
        0,
    )
```
`triage` keeps its output; its first ~30 lines (ref check, diff, untracked comment, hunk loop)
are replaced by `changed = _changed(g, root, base)`, `if changed is None: return f"unknown --base {base}", 2`,
`if not changed: return "no changes", 0`; drop the untracked ponytail comment.
`cli.py`: `im = sub.add_parser("impact"); im.add_argument("--base", default="main")`; add
`"impact"` to `_SINGLE_ROOT` and to the imports; dispatch exactly like `triage` (exit 2 → stderr).
Test: extend `test_triage_lists_changed_callers`:
```python
    out, _, _ = run(capsys, "--repo", str(repo), "impact", "--base", "HEAD")
    assert "get_db <- app.api.list_orders" in out and "candidates" in out
    before = run(capsys, "--repo", str(repo), "triage", "--base", "HEAD")[0].split()[1]
    (repo / "app/new.py").write_text("def fresh():\n    return 1\n")
    after = run(capsys, "--repo", str(repo), "triage", "--base", "HEAD")[0].split()[1]
    assert int(after) > int(before)
```
Untouched: `triage` row formatting, `entrypoint touched:` rows, `_HUNK`, `_DIFF_OPTS`.
Verify: `uv run pytest -q`; live on TMS `impact --base ef59da6` lists production callers first
and `+ N test callers`; `impact --base main` exits 2 with the sanity message.

### A4 — `wiremap hook` defaults to `post-edit`; install text guarded with `|| true`
Files: `src/wiremap/cli.py`, `src/wiremap/commands.py`, `tests/test_wiremap.py`.
`cli.py:119`: `hks = hk.add_subparsers(dest="hook_cmd")` (drop `required=True`).
`HOOK_JSON` command string: `"command -v wiremap >/dev/null && wiremap hook post-edit || true"`.
Test: in `test_hook_post_edit_capped`, after the `{}` case add
`monkeypatch.setattr(sys, "stdin", io.StringIO("{}")); assert run(capsys, "--repo", str(repo), "hook")[2] == 0`.
Verify: `uv run pytest -q`; `echo '{}' | wiremap hook; echo $?` → 0.

### A5 — docs and skill
Files: `README.md`, `skills/wiremap/SKILL.md`, `CLAUDE.md`, `docs/plans/2026-09-14-wiremap-v1.md` (ledger line only).
README: command table rows for `pack` (assertion lines + tool block, tests last, census markers),
`callers` (name hits line), new `impact`, `triage` (untracked included), `hook` (`|| true`;
recommended off while measuring); the "What it does not do" bullet on `unresolved:` gains
"— `callers` therefore greps the bare name and lists hits without an edge". CLAUDE.md header
command list adds `impact`. Replace `skills/wiremap/SKILL.md` with:
```
---
name: wiremap
description: Framework-aware callers, deps, skeletons, context packs and review-time impact lists from the wiremap CLI, never stale. For Fable writing briefs, reviewers and researchers; implementers get the pack inside their brief.
---
# wiremap
`--repo <root>` goes before the subcommand; a worktree-isolated agent passes the main tree.
Every caller list is a candidate list: string dispatch (`send_task`, `getattr`) is invisible, so grep the bare name before concluding "no callers". EXTRACTED = fact; INFERRED = name-only lead.
1. Implementer brief (Fable): `wiremap --repo <root> pack --files <paths> --task "<one line>"`, pasted verbatim under your own assertion lines (imports present or absent, prohibitions, nullability, each with file:line). A file under ~60 lines is pasted instead of packed.
2. Review brief (Fable): `wiremap --repo <root> impact --base <target>`, pasted verbatim. Reviewer: every listed caller outside the diff is a finding or one line "unaffected because …".
3. Reviewer on demand: `wiremap callers <symbol id>` for a changed public symbol the impact list capped; `triage --base <target>` for the files to read.
4. Researcher: `skeleton <file>` before opening a file, `deps` and `callers` for the hops; cite file:line.
5. The tool never outranks the code: on any contradiction, STOP and report BLOCKED.
```
Then `wiremap install-skill` (copies to `~/.claude/skills/wiremap/SKILL.md`).
Verify: `diff skills/wiremap/SKILL.md ~/.claude/skills/wiremap/SKILL.md` empty; README rows match live output.

## Phase B — final review + gate
Fresh senior-engineer review over `git diff 76541f9...HEAD`; `/code-review`; qa-e2e gate on the
fixture repo plus TMS live runs (read-only); `wip:` commits per accepted task; push after GO.

## Phase C — flow config (`~/.claude`, branch `main`)
One mechanical task for a developer-tier stand-in reading a brief file; config-consistency
researcher audit before commit; `git push` and settings.json edits are the user's.

### C1 — role bodies, brief rule, reviewing, planning
- `agents/developer.md:9`, `agents/senior-engineer.md:9`: "load the `ponytail:ponytail`, `code-style` and `wiremap`
  skills" → "load the `ponytail:ponytail` and `code-style` skills".
- `rules/common/agents.md:174-189` context-pack paragraph → replace with:
```
- **The brief is the plan task, verbatim** — exact repo-relative paths,
  expected behavior, the test that proves it — plus what must stay
  untouched, plus a **context pack**: Fable's own assertion lines first
  (imports present or absent, prohibitions, nullability — each with
  file:line), then the output of `wiremap --repo <root> pack --files <paths>
  --task "<one line>"` pasted verbatim (the **wiremap** skill); a file under
  ~60 lines is pasted instead of packed; where wiremap does not parse the
  language the structural block is built by hand and labelled so. The pack
  ends with wiremap's STOP line — implementers keep that rule and load
  nothing else of the skill. Worktree-isolated implementers only: never put absolute
  main-checkout paths in their brief (writes there are hard-blocked)
  and require the report to state the worktree path. Every implementer
  and review brief also carries the plan's Test budget section verbatim.
  If the brief would require the subagent to make a design
  decision, the task isn't ready: return to the plan.
```
- `rules/common/agents.md` "Two-stage review" bullet: append "Every review brief also carries
  `wiremap --repo <root> impact --base <target>` output pasted verbatim (the **wiremap** skill)."
- `skills/reviewing/SKILL.md:20-22` → "Run `git diff` to understand the changes. The brief
  carries `wiremap impact` output: every caller it lists outside the diff is a finding or one
  line "unaffected because …". For a branch, `wiremap --repo <main tree> triage --base <target>`
  lists the files to read (`--repo` because a worktree-isolated reviewer's own tree lacks the branch)".
  `:91-92`: append "— the list is a candidate list; grep the bare name too."
- `skills/planning/SKILL.md:43-44` → "`wiremap callers` before answering, then grep the bare
  name: the list is a candidate list (string dispatch is invisible)."
Verify: `grep -rn wiremap ~/.claude/agents ~/.claude/rules ~/.claude/skills/{reviewing,planning}` matches the above; audit GO.

### C2 — user-owned settings.json edits (classifier refuses agent edits)
Removes the wiremap PostToolUse entry and ADDS wiremap-metrics as a second SessionEnd hook
(`session-metrics.py` is kept: its series continues — decided 2026-09-16 after the D1 review):
```
cd ~/.claude && jq '.hooks.PostToolUse[0].hooks |= map(select(.statusMessage != "wiremap: cross-file callers"))
  | .hooks.SessionEnd[0].hooks += [{"type":"command","command":"python3 ~/.claude/hooks/wiremap-metrics.py","timeout":10,"statusMessage":"metrics: wiremap counters"}]' settings.json > s.tmp && mv s.tmp settings.json
```
Statusline stays. Takes effect in new sessions.

## Phase D — measurement

### D1 — `~/.claude/hooks/wiremap-metrics.py` (senior-engineer stand-in; runs beside `session-metrics.py`, which stays)
```python
#!/usr/bin/env python3
# ponytail: counts wiremap use from this session's transcripts at SessionEnd; subagent
# transcripts live under /tmp and vanish on reboot, so a row can be partial — never zero-filled
import json
import pathlib
import re
import sys
import time

inp = json.load(sys.stdin)
main = pathlib.Path(inp["transcript_path"])
sid = inp["session_id"]
tasks = (
    pathlib.Path(sys.argv[1])
    if len(sys.argv) > 1
    else pathlib.Path("/tmp/claude-1000") / main.parent.name / sid / "tasks"
)
files = [main] + (sorted(tasks.glob("*.output")) if tasks.is_dir() else [])
CLI = re.compile(
    r"\bwiremap (?:--repo \S+ )?(?:pack|callers|impact|triage|skeleton|deps|grep)\b(?![^\n]*<)"
)
c = dict(
    session=sid, ts=int(time.time()), cwd=inp.get("cwd"), files=len(files),
    cli_calls=0, briefs=0, tool_packs=0, impact_briefs=0,
    hook_firings=0, hook_bytes=0, prompt_bytes=0,
)
for f in files:
    for line in f.open(encoding="utf-8", errors="replace"):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        a = r.get("attachment") or {}
        if a.get("type") == "hook_additional_context":
            t = str((a.get("content") or [""])[0])
            if t.startswith("wiremap:"):
                c["hook_firings"] += 1
                c["hook_bytes"] += len(t)
        for b in (r.get("message") or {}).get("content") or []:
            if not isinstance(b, dict) or b.get("type") != "tool_use":
                continue
            i = b.get("input") or {}
            if b["name"] == "Bash" and CLI.search(i.get("command", "")):
                c["cli_calls"] += 1
            p = i.get("prompt") or i.get("message") or ""
            if b["name"] in ("Agent", "SendMessage") and "STOP and report BLOCKED" in p:
                c["briefs"] += 1
                c["prompt_bytes"] += len(p)
                c["tool_packs"] += "Callers are candidates (string dispatch" in p
                c["impact_briefs"] += "Callers are candidates: string dispatch" in p
out = pathlib.Path.home() / "Documents" / "claude_flow_notes" / "metrics" / "wiremap.jsonl"
out.parent.mkdir(parents=True, exist_ok=True)
with out.open("a") as fh:
    fh.write(json.dumps(c) + "\n")
```
Verify (D2): `printf '{"session_id":"9bafd3b0","transcript_path":"%s","cwd":"x"}' ~/Documents/tms_notes/metrics/9bafd3b0/main.jsonl | python3 ~/.claude/hooks/wiremap-metrics.py ~/Documents/tms_notes/metrics/9bafd3b0/tasks`
→ last row of `wiremap.jsonl` reads `cli_calls 0, briefs 25, tool_packs 0, impact_briefs 0, hook_firings 39, hook_bytes 51558`
(`briefs` may differ from 25 if some SendMessage briefs carry the STOP line; record the actual number as the baseline).

### D3 — after one comparable feature
Read the new row against the baseline; read the feature's review briefs for impact rows that
became findings. If `tool_packs`/`impact_briefs` stay 0, Fable's compliance is the failure →
add the brief-lint hook. If they are non-zero and no impact row became a finding on two
features, remove the review-time integration (shape C).

## Follow-ups (outside this branch)
- CLI stderr hints (`resolve.py` `uv tool install 'wiremap[...]'`) and CLAUDE.md line 3 still point at a PyPI install that does not exist yet; fix when publishing, or append the git URL (found by /code-review 2026-09-16).
- `_signature` no-body fallback (Go `type_spec`, TS `lexical_declaration`) keeps a trailing comment on its first line (LOW, cosmetic).
- `grep` has no unit test (budget at 25/25); covered by the qa-e2e gate scenario 10.
- Keyword-prefixed shell segments (`do wiremap …`, `then wiremap …`) are not counted by wiremap-metrics (LOW).
- qa-e2e 2026-09-16 (GO with findings, `~/Documents/wiremap_notes/qa-e2e/v2/verdict.md`), shipped unfixed:
  - F1 `_name_hits` is noise for short/method names (TMS `PendingTaskStore.get` → 2197 hits in Dockerfile/README rows); restrict the grep to code extensions and/or suppress for class members.
  - F2 `_is_test` treats any `test*` directory as tests, so `app/testing/harness.py` production callers land in `+ N test callers` of `impact`.
  - F3 pack census markers: entry-points hint has no command; callers hint `wiremap callers <symbol id>` is not buildable from the bare `dst` name shown.
  - F4 `head_stamp` returns `no-commit` without drift counts on a commit-less repo.
  - F5 the 15-line pack contract bounds lines, not bytes (`files:` and `… N more definitions` rows exceed 200 chars on 12-file packs; `--task` echoed unbounded).
  - F6 `impact` prints `+ 1 test callers`.
  - `callers` in workspace (multi-`--repo`) mode has no name hits at all.

## User-owed items
1. Plan acceptance (this doc) before the first delegation.
2. C2 settings.json edits; `git push` of `~/.claude` after the audit GO.
3. `git push` of the wiremap repo after the qa-e2e GO (Fable may push; user authorised earlier).

## Ledger
- Phase A: [x] A1 (review PASS r1; cap 5 production-first) · [x] A2 (review PASS r1; A2.1 floor-then-fill `_fit`, A2.2 global tests-last + one-line task + live assertion) · [x] A3 (review PASS r1; A3.1 `continue` + `…` markers + SCHEMA `sig:span-cut`; A3.2 `IMPACT_PER_SYMBOL = 3`, `_is_test` dirs-only + conventional filenames) · [x] A4 · [x] A5 — A3.1/A3.2/A4/A5 covered by the final branch review
- Phase B: [x] final review (r1 FAIL: skeleton interleave broke indentation → A2.3 select-by-interleave/render-in-source-order, caller dedupe, .tsx suffixes, real regression assertion; r2 PASS) · [x] /code-review (high: 10 confirmed findings → A6 fix round, then A7 AST comment blanking in `_signature` + SCHEMA `sig:span-nocomment-ast`; scoped reviews r3 FAIL → r4 PASS: impact cross-file guard, name-hits workspace skip + import-line skip, untracked -z, signature comment/no-body fallbacks, pack graph_root entry points, IMPACT_MAX wording, --no-optional-locks, shared _git_grep) · [x] qa-e2e GO with findings (05b687c, F1–F6 → Follow-ups; marker stamped on the archive commit) · [x] push
- Phase C: [x] C1 (Sonnet stand-in, verbatim) · [x] audit GO (`~/Documents/claude_flow_notes/wiremap-integration/research/2026-09-16-config-audit-v2.md`; ~/.claude commit 3515ab3) · [x] C2 (user, 2026-09-16; ~/.claude 826acee pushed)
- Phase D: [x] D1 (review PASS r2; six findings fixed; keyword-prefix segments `do|then` uncounted — LOW, accepted) · [x] D2 baseline row (cli_calls 0, briefs 43, tool_packs 0, impact_briefs 0, hook 39/51558 B; `~/Documents/wiremap_notes/baselines/2026-09-15-adoption-baseline.md`) · [ ] D3 (after the next feature)
