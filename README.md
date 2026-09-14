# wiremap

Framework-aware code context for AI coding agents: callers, deps, skeletons and context packs, never stale.

## The problem

Coding agents spend roughly 30% of their tokens re-exploring code they have already read.
Static call graphs miss the edges frameworks register at runtime — dependency injection,
graph nodes, ORM relationships, task queues — so "who calls this?" comes back empty for
exactly the code that matters most. And every prebuilt index goes stale the moment you
work in a worktree or switch branches.

## 30-second demo

```python
# app/api.py
@router.get("/orders")
def list_orders(db=Depends(get_db)):
    return []

# app/graph.py
g.add_node("classify", classify)
g.add_node("handle", handle)
g.add_edge("classify", "handle")
```

```console
$ wiremap callers app.db.get_db
app.api.list_orders  depends  EXTRACTED  app/api.py:5
unresolved: 0

$ wiremap callers app.graph.handle
app.graph  graph_node  EXTRACTED  app/graph.py:10
app.graph.classify  graph_edge  EXTRACTED  app/graph.py:11
unresolved: 0
```

No grep would have found either edge.

## Install

```console
$ uv tool install wiremap
$ wiremap install-skill      # copies SKILL.md to ~/.claude/skills/wiremap/
```

## Commands

All commands accept `--repo PATH` (default: the current checkout) and `--stats`.

| Command | What it prints |
|---|---|
| `skeleton <path>...` | One line per definition, indented by nesting |
| `callers <symbol> [--depth N] [--min-confidence extracted]` | Who calls, injects, routes to or queues this symbol |
| `deps <path\|symbol> [--depth N]` | What it calls, plus dynamic-dispatch notices |
| `grep <regex>` | ripgrep hits grouped by enclosing symbol |
| `pack --files <path>... [--task TEXT]` | A ≤15-line context pack for a brief |
| `entrypoints` | Routes, tasks and graph roots |
| `install-skill` | Copies `SKILL.md` to `~/.claude/skills/wiremap/` |
| `cache prune [--days 30]` | Drops cache entries older than N days |

Exit codes: `0` ok · `1` symbol not found or ambiguous (candidates printed) · `2` repo or
parse error (also when more than 10% of files fail to parse).

## Confidence model

Every edge carries its confidence, and ambiguity is reported rather than guessed.

- **EXTRACTED** — a literal reference in the source. Treat it as fact.
- **INFERRED** — matched by unique name across the repo. Confirm before relying on it.
- **`unresolved: N`** — N candidates shared that name, so no edge was recorded. A
  non-zero count means grep before concluding there are no callers.

## Frameworks

| Framework | What it matches | Edge kind |
|---|---|---|
| FastAPI | `Depends(fn)` | `depends` |
| FastAPI | `@router.get/post/put/patch/delete/websocket` | `route` |
| LangGraph | `add_node("label", fn)` | `graph_node` |
| LangGraph | `add_edge("a", "b")` | `graph_edge` |
| LangGraph | `add_conditional_edges("a", fn)` | `graph_edge` |
| SQLAlchemy | `relationship("Other")` | `relationship` |
| Celery | `@shared_task` / `@celery.task(...)` | `task` |
| Celery | `fn.delay(...)` / `fn.apply_async(...)` | `calls` |

Add a framework: one `Rule`, one fixture file, one assertion.

## What it does not do

- No server, no MCP, no daemon. It is a CLI that exits.
- Writes nothing into the repository it analyses. The only state is a parse cache under
  `~/.cache/wiremap/`.
- No build step and no index to rebuild — it parses on every call, so a worktree or a
  branch switch can never serve stale answers.
- TypeScript gets generic edges only (symbols, imports, calls); framework rules are
  Python-only in v1.
- Method calls resolve by name alone, so they are INFERRED, not EXTRACTED.
- Relative and aliased imports (`from .x import y`, `import a as b`) are not tracked as
  import edges, so their targets resolve by unique name only (INFERRED) or land in
  `unresolved:`.

## Roadmap

1. More languages via grammar drop-in (Go, Rust)
2. Cross-artifact edges (SQL, docs)
3. Multi-repo workspaces
4. Community detection + visualisation
5. PR triage
6. Hook + statusline (strict caps)
7. LSP resolution (INFERRED → EXTRACTED)
8. LLM summaries
9. Docs and rules

One issue each. MCP: never.

## License

MIT
