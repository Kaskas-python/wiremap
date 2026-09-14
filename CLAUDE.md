# wiremap

Framework-aware code context for AI coding agents. A stateless CLI (`uv tool install wiremap`)
run from any git checkout or worktree: parses Python and TypeScript with tree-sitter into an
in-memory graph on every call (symbols, imports, calls, plus framework edges for FastAPI,
LangGraph, SQLAlchemy and Celery) and answers `callers`, `deps`, `skeleton`, `grep`,
`entrypoints` and `pack`. Only a per-file parse cache persists, under `~/.cache/wiremap/`.

Stack: Python ≥ 3.10 · uv · hatchling · tree-sitter (exact pins) · pytest. Public repo
`Kaskas-python/wiremap`, MIT, PyPI name `wiremap`.

## Read before working

| When | Read |
|---|---|
| Always | [docs/workflow.md](docs/workflow.md) |
| Design, module map, test budget, task ledger | [docs/plans/2026-09-14-wiremap-v1.md](docs/plans/2026-09-14-wiremap-v1.md) |
| Changing what agents do with the tool | `skills/wiremap/SKILL.md` (created in T10a) |

## Non-negotiable rules

1. **The plan is the spec.** Every task is a numbered entry in the plan doc with a code
   snippet and a Verify line. Implement the snippet's shape; touch nothing the task does not
   name. Contradiction between plan and code → STOP, report BLOCKED.
2. **Module map is fixed.** `src/wiremap/{cli,discover,parse,frameworks,resolve,commands,lsp,llm}.py`,
   `src/wiremap/viewer.html` plus `queries/*.scm`. No new modules, packages or config files
   without a plan change.
3. **Comments only as `# ponytail:` markers** naming a deliberate shortcut and its upgrade
   path. Everything else is expressed by names.
4. **Stateless by contract.** Never write into the target repo, never leave a process running
   after exit (an `--lsp` language server is a child killed before return), never add MCP. The
   only persisted state is under `~/.cache/wiremap/<repo-sha1>/` (parse entries, `summaries/`,
   `last_stats.json`), written per-process temp + rename, best-effort. `install-skill`
   additionally copies `skills/wiremap/SKILL.md` to `~/.claude/skills/wiremap/`.
5. **Every edge carries a confidence** (`EXTRACTED` or `INFERRED`); ambiguity is reported
   (exit 1 with candidates, or an `unresolved:` count), never silently picked.
6. **Test budget is one file.** `tests/test_wiremap.py`, the tests listed in the plan's
   Test budget tables (v1 + delta), against the fixture repo in `tests/conftest.py`. Test
   lines never exceed production lines. Do not add tests beyond those tables; extend an
   existing one if a new assertion is truly needed.
7. **Dependencies are exact-pinned** (`tree-sitter==…`). Add via `uv add`, never by hand.
8. Run `uv run pytest -q` before calling a task done and paste the output.
