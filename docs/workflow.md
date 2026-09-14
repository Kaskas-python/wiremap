# Agent workflow

## For every task

1. **Check the tree.** `git rev-parse --short HEAD` and `git branch --show-current` must
   match the brief. Mismatch → STOP, report BLOCKED.
2. **Read the task** in `docs/plans/2026-09-14-wiremap-v1.md`: its snippet, its Verify line,
   and the "Task conventions" section (module map, shared `Symbol` / `RawEdge` types).
3. **Read the file you touch** and its neighbours in the module map. Match how they are
   written. Nothing outside the named file or region changes.
4. **Type the snippet's shape.** Adapt only where the pinned tree-sitter API differs from the
   snippet, and say so in the report. No extra helpers, options, abstractions or fallbacks.
5. **Run the task's Verify line** exactly as written, then `uv run pytest -q`.
6. **Report:** what changed, the Verify output pasted verbatim, the pytest summary line, and
   anything adapted, skipped or assumed.

## Definition of done

- The task's Verify line passes and its output is in the report.
- `uv run pytest -q` passes (the tests in the plan's Test budget tables; never fewer than
  before the task).
- CI (`.github/workflows/ci.yml`, Python 3.10–3.13) is green after push.
- A new command or flag appears in `cli.py`, the README command table and, if agents use
  it, `skills/wiremap/SKILL.md` in the same change.
- A new framework rule ships with one fixture file addition and one assertion in
  `test_framework_edges`.
- The plan ledger entry is ticked by the orchestrator after review, not by the implementer.

## Keeping docs current

The plan doc is the source of truth for design and progress. A task that changes a
command, exit code, edge kind, cache layout or the `pack` line contract updates the
matching section of the plan and the README in the same change. Say what to do, not the
history of why.

## Things to avoid

- Committing, pushing or opening PRs unless asked. The user creates the public repo and
  publishes to PyPI; agents never do.
- Adding a dependency the plan does not list. Use `uv add` with an exact pin.
- Writing anything into the target repository being analysed, or into the real
  `~/.cache/wiremap` from tests (`conftest.py` redirects `HOME`).
- Output that grows without a cap: lists stop at 40 lines, `pack` at 15.
- Guessing when a symbol is ambiguous. Exit 1 and print the candidates.
