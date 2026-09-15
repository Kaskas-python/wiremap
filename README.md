# wiremap

Framework-aware code context for AI coding agents: callers, deps, skeletons and context packs, never stale.
It parses Python, TypeScript, Go and Rust (grammar drop-in), plus SQL schemas and Markdown docs.

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

### Optional extras

```console
$ uv tool install 'wiremap[go,rust]'   # Go and Rust grammars
```

`--lsp` additionally needs language servers on PATH:
`npm i -g pyright typescript-language-server typescript`.

## Commands

All commands accept `--repo PATH` (default: the current checkout) and `--stats`, given before
the subcommand: `wiremap --repo PATH --stats <command>`; `wiremap --version` prints the version.

`--repo` is repeatable (workspace: ids are prefixed `<dirname>:`; single-root commands use
the first). `--lsp` resolves INFERRED and unresolved calls through pyright /
typescript-language-server; the child processes are killed before exit.

| Command | What it prints |
|---|---|
| `skeleton <path>...` | One line per definition, indented by nesting |
| `callers <symbol> [--depth N] [--min-confidence extracted]` | Who calls, injects, routes to or queues this symbol |
| `deps <path\|symbol> [--depth N]` | What it calls, plus dynamic-dispatch notices |
| `grep <regex>` | ripgrep hits grouped by enclosing symbol |
| `pack --files <path>... [--task TEXT]` | A ≤15-line context pack for a brief |
| `entrypoints` | Routes, tasks and graph roots |
| `communities` | Clusters of connected symbols with their hub (label propagation) |
| `graph [SYMBOL] [--files PATH...] [--format mermaid\|dot\|graphml\|cypher] [--html PATH]` | The neighbourhood as Mermaid (default), DOT, GraphML, Cypher, or a self-contained HTML viewer |
| `export --obsidian DIR` | One Markdown page per file with wikilinked calls/callers (Obsidian vault or agent-crawlable wiki) |
| `report` | Hubs, directories, communities, entry points, ambiguous names — a GRAPH_REPORT in one screen |
| `ask TEXT` | Symbols whose names match the task text, ranked by match then degree |
| `triage [--base REF]` | Files a reviewer should read for the branch diff, plus entry points touched |
| `hook post-edit` | Claude Code PostToolUse hook: ≤ 20 cross-file EXTRACTED callers of symbols in the edited file, always exit 0, as PostToolUse additionalContext JSON |
| `install-hook` | Prints the settings.json hook block and statusline suffix to paste |
| `status` | One statusline segment from the last build's stats; never parses |
| `summarize --files PATH... \| --write PATH` | Agent-written ≤ 10-line notes per file, cached by content hash; `--write` reads the note from stdin |
| `install-skill` | Copies `SKILL.md` to `~/.claude/skills/wiremap/` |
| `cache prune [--days 30]` | Drops parse-cache entries older than N days (notes are kept) |

Exit codes: `0` ok · `1` symbol or path not found, or ambiguous (candidates printed) ·
`2` repo error, or more than 10% of files unreadable (syntax errors warn and parse
partially).

## Hook and statusline

`wiremap install-hook` prints two blocks to paste into `~/.claude/settings.json`: a
`hooks.PostToolUse` entry matching `Write|Edit`, and a `statusLine.command` suffix.

The hook is deliberately small: at most 20 rows, only cross-file callers, only EXTRACTED
edges, and only symbols whose name is at least 6 characters (a repo-wide `get` or `run`
would bury the useful rows). It runs with a 10 s timeout and **always exits 0** — a hook
that fails is silent, never an error in your editor.

The statusline suffix captures stdin once and replays it to your existing command, so an
already-configured statusline keeps working; `wiremap status` then appends one segment
read from the last build's stats, without parsing anything. The directory comes from
`jq -r '.workspace.current_dir // "."'`, so a payload without that key falls back to the
current directory and a jq error never reaches the status line.

## Confidence model

Every edge carries its confidence, and ambiguity is reported rather than guessed.

- **EXTRACTED** — a literal reference in the source. Treat it as fact.
- **INFERRED** — matched by unique name across the repo. Confirm before relying on it.
- **`unresolved: N`** — N candidates shared that name, so no edge was recorded. A
  non-zero count means grep before concluding there are no callers. `deps` prints
  `unresolved: N` — the number of ambiguous callee names it had to drop.

Cross-artifact edges follow the same rule. `table_ref` is EXTRACTED from a `CREATE TABLE`
or a `__tablename__`, and INFERRED when it comes from a SQL string embedded in code.
`mentions` is always INFERRED (prose is not evidence), even when a doc cites the full id.

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

- No server, no MCP; an `--lsp` language server is a child process killed before exit.
  No daemon.
- Writes nothing into the repository it analyses. The only state is under
  `~/.cache/wiremap/<repo>/`: parse entries, `summaries/`, `last_stats.json`.
- No build step and no index to rebuild — it parses on every call, so a worktree or a
  branch switch can never serve stale answers.
- Framework rules are Python-only; TypeScript, Go and Rust get generic symbol, import
  and call edges.
- Method calls resolve by name alone, so they are INFERRED, not EXTRACTED.
- Relative and aliased imports (`from .x import y`, `import a as b`) are not tracked as
  import edges, so their targets resolve by unique name only (INFERRED) or land in
  `unresolved:`.
- `START`/`END` constants passed to `add_edge` are not treated as edges.
- A `.delay()`/`.apply_async()` call also yields a name-only `calls` edge to any repo
  function named `delay`/`apply_async`.
- Files over 1 MB are skipped (stderr notice + `skipped_too_large` stat).
- `grep` needs ripgrep (`rg`) on PATH; exits `2` otherwise.
- Untracked files are invisible to `triage` — it reads the diff, and git does not diff
  what it does not track.
- `export --obsidian` refuses a DIR inside the repo unless it is gitignored. A `dir/`
  pattern only matches once the directory exists, so `mkdir` it first or use a slashless
  pattern.
- Doc mentions never count toward `unresolved:` — prose naming an ambiguous symbol is not
  a missing edge.
- Go and Rust methods do not nest under their receiver or `impl` type; the qualname is the
  bare method name.
- Never calls a model or the network. The agent driving it is the model;
  `summarize --write` stores the agent's own notes.

## Shipped

1. More languages via grammar drop-in (Go, Rust)
2. Cross-artifact edges (SQL, docs)
3. Multi-repo workspaces
4. Community detection
5. Visualisation (Mermaid, DOT, GraphML, Cypher, HTML viewer)
6. PR triage
7. Hook + statusline (strict caps)
8. LSP resolution (INFERRED → EXTRACTED)
9. Agent-written summaries (content-hash cache)
10. Obsidian export

Not planned: MCP, live-reload viewer, direct Neo4j push.

## License

MIT
