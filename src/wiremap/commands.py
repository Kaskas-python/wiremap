import hashlib
import html
import json
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from itertools import zip_longest
from pathlib import Path

from wiremap.discover import RepoError, _git, head_stamp
from wiremap.parse import Symbol, cache_dir, write_atomic
from wiremap.resolve import Edge, Graph

CAP = 40
GREP_TIMEOUT = 30
NAME_HITS_SHOWN = 5
PACK_LINES = 15


def _is_test(file: str) -> bool:
    # ponytail: path heuristic — the graph has no test concept, so a test is a
    # directory named test* or a conventional test filename; a source file merely
    # named test.py stays production; upgrade: none
    p = Path(file)
    return (
        any(d.startswith("test") for d in p.parts[:-1])
        or p.name.startswith("test_")
        or p.name.endswith(("_test.py", "_test.go", ".spec.ts", ".test.ts"))
    )


def _top(rows: list[str], n: int) -> list[str]:
    return rows if len(rows) <= n else rows[:n] + [f"… and {len(rows) - n} more"]


def _cap(lines: list[str]) -> list[str]:
    return _top(lines, CAP)


def _find(g: Graph, symbol: str) -> tuple[list[str], int]:
    if symbol in g.symbols:
        return [symbol], 0
    ids = [s.id for s in g.symbols.values() if s.name == symbol]
    if len(ids) == 1:
        return ids, 0
    return ids, 1


def _name_hits(g: Graph, root: Path, symbol: str) -> list[str]:
    # ponytail: string dispatch is invisible to the parser, so the bare name is
    # grepped and every hit without an edge is listed; upgrade: none wanted
    s = g.symbols[symbol]
    known = {(e.file, e.line) for e in g.edges if e.dst == symbol}
    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "grep",
                "-n",
                "-I",
                "-w",
                "-F",
                "-z",
                "--untracked",
                "-e",
                s.name,
                "--",
            ],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=GREP_TIMEOUT,
            check=False,
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
                text = parts[2].strip()
                text = text[:80] + "…" if len(text) > 80 else text
                rows.append((_is_test(parts[0]), f"  {parts[0]}:{parts[1]}: {text}"))
    rows.sort()
    return [f"name hits without an edge: {len(rows)}"] + [
        r for _, r in rows[:NAME_HITS_SHOWN]
    ]


def callers(
    g: Graph, root: Path, symbol: str, depth: int = 1, min_conf: str = "INFERRED"
) -> tuple[str, int]:
    ids, code = _find(g, symbol)
    if code:
        if ids:
            return "\n".join(["ambiguous, candidates:"] + ids), 1
        return f"not found: {symbol}", 1
    seen, frontier, rows = set(), {ids[0]}, []
    for _ in range(depth):
        nxt = set()
        for e in g.edges:
            if (
                e.dst in frontier
                and e.src != e.dst
                and e.src not in seen
                and (min_conf == "INFERRED" or e.confidence == "EXTRACTED")
            ):
                rows.append(f"{e.src}  {e.kind}  {e.confidence}  {e.file}:{e.line}")
                seen.add(e.src)
                nxt.add(e.src)
        frontier = nxt
    rows.sort(key=lambda r: ("INFERRED" in r, r))
    body = _cap(rows) or ["no callers found"]
    unresolved = g.unresolved.get(symbol.split(".")[-1], 0)
    return "\n".join(
        body + [f"unresolved: {unresolved}"] + _name_hits(g, root, ids[0])
    ), 0


def _rel(root: Path, p: str) -> str | None:
    q = (
        Path(p)
        if Path(p).is_absolute()
        else (root / p if (root / p).is_file() else Path.cwd() / p)
    )
    try:
        rel = q.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    return str(rel) if q.is_file() else None


_DYNAMIC = (
    "getattr(",
    "importlib",
    "globals()[",
    "send_task(",
    "apply_async(",
    "setattr(",
)


def deps(g: Graph, root: Path, target: str, depth: int = 1) -> tuple[str, int]:
    rel = _rel(root, target)
    frontier = {s.id for s in g.symbols.values() if s.file == rel} if rel else set()
    if not frontier:
        ids, code = _find(g, target)
        if code:
            if ids:
                return "\n".join(["ambiguous, candidates:"] + ids), 1
            return f"not found: {target}", 1
        frontier = {ids[0]}
    seen, rows = set(), []
    start = set(frontier)
    for _ in range(depth):
        nxt = set()
        for e in g.edges:
            if e.src in frontier and e.src != e.dst and e.dst not in seen:
                rows.append(f"{e.dst}  {e.kind}  {e.confidence}  {e.file}:{e.line}")
                seen.add(e.dst)
                nxt.add(e.dst)
        frontier = nxt
    rows.sort(key=lambda r: ("INFERRED" in r, r))
    rows = _cap(rows) or ["no deps found"]
    amb = {n for i in start for n in g.ambiguous.get(i, [])}
    rows.append(f"unresolved: {len(amb)}")
    involved = {g.symbols[i].file for i in start | seen if i in g.symbols}
    notices = []
    for file in sorted(involved):
        text = (root / file).read_text(encoding="utf-8", errors="replace")
        for ln, line in enumerate(text.splitlines(), 1):
            if any(p in line for p in _DYNAMIC):
                notices.append(f"dynamic dispatch present at {file}:{ln}")
    return "\n".join(rows + _cap(notices)), 0


def entrypoints(g: Graph) -> str:
    rows = [
        f"{e.dst}  {e.kind}  {e.file}:{e.line}"
        for e in g.edges
        if e.kind in ("route", "task")
    ]
    linked = {e.dst for e in g.edges if e.kind == "graph_edge"}
    roots = [e.src for e in g.edges if e.kind == "graph_edge" and e.src not in linked]
    body = sorted(rows) + [f"{r}  graph_root" for r in sorted(set(roots))]
    return "\n".join(_cap(body) or ["no entry points found"])


def skeleton(g: Graph, root: Path, paths: list[str]) -> tuple[str, int]:
    rels = []
    for p in paths:
        rel = _rel(root, p)
        if rel is None:
            return f"not found: {p}", 1
        rels.append(rel)
    rows = []
    for s in sorted(g.symbols.values(), key=lambda s: (s.file, s.line_start)):
        if s.file in rels and s.kind != "table":
            rows.append("  " * s.qualname.count(".") + s.signature)
    return "\n".join(_cap(rows) or ["no symbols in given files"]), 0


def _enclosing_symbol(syms: list[Symbol], line: int) -> str | None:
    spans = [
        (s.line_end - s.line_start, s.id)
        for s in syms
        if s.line_start <= line <= s.line_end
    ]
    return min(spans)[1] if spans else None


def grep(g: Graph, root: Path, pattern: str) -> tuple[str, int]:
    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "grep",
                "-n",
                "-I",
                "-E",
                "-z",
                "--untracked",
                "-e",
                pattern,
                "--",
            ],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=GREP_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return f"grep: timed out after {GREP_TIMEOUT} s", 2
    if proc.returncode > 1:
        return proc.stderr.strip() or "grep: failed", 2
    hits = []
    for row in proc.stdout.split("\n"):
        parts = row.split("\0", 2)
        if len(parts) == 3 and parts[1].isdigit():
            hits.append((parts[0], int(parts[1]), parts[2].rstrip()))
    by_file = defaultdict(list)
    for s in g.symbols.values():
        by_file[s.file].append(s)
    groups = defaultdict(list)
    for f, ln, text in hits:
        groups[_enclosing_symbol(by_file[f], ln) or f].append(f"  {f}:{ln}: {text}")
    body = [
        row
        for k, v in sorted(groups.items())
        for row in [f"{k} ({len(v)} hits)"] + v[:3]
    ]
    return "\n".join(_cap(body) or ["no matches"]), 0


STOP = (
    "Callers are candidates (string dispatch is invisible). "
    "If this pack contradicts the code or the stamp differs, STOP and report BLOCKED."
)


def _symbols_in(g: Graph, file: str) -> list[Symbol]:
    return sorted(
        (s for s in g.symbols.values() if s.file == file and s.kind != "table"),
        key=lambda s: s.line_start,
    )


def _fit(
    sections: list[list[str]], hints: list[str], budget: int, floor: int = 3
) -> list[str]:
    # ponytail: every section keeps up to `floor` rows before any section grows;
    # a row that does not fit is skipped, not stopped on — finishing a section
    # drops its marker, so a later row can still fit
    order = [(i, r) for i, s in enumerate(sections) for r in s[:floor]]
    order += [(i, r) for i, s in enumerate(sections) for r in s[floor:]]
    kept: list[list[str]] = [[] for _ in sections]
    for i, r in order:
        markers = sum(
            len(k) + (i == j) < len(s)
            for j, (k, s) in enumerate(zip(kept, sections))
        )
        if sum(map(len, kept)) + 1 + markers > budget:
            continue
        kept[i].append(r)
    out: list[str] = []
    for k, s, h in zip(kept, sections, hints):
        out += k
        if len(k) < len(s):
            out.append(f"\u2026 {len(s) - len(k)} more {h}")
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
    one_line = task.replace("\n", " ") if task else None
    head = [f"HEAD {head_stamp(root)}"] + ([f"task: {one_line}"] if one_line else [])
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
        ca_files.append(rows)
        sk_files.append(["  " * s.qualname.count(".") + s.signature for s in ss])
    pairs = [p for rows in zip_longest(*ca_files) for p in rows if p]
    ca = [r for _, r in sorted(pairs, key=lambda p: p[0])]
    sk = [r for rows in zip_longest(*sk_files) for r in rows if r]
    body = _fit(
        [sk, ca, ep],
        [
            f"definitions: wiremap skeleton {' '.join(rels)}",
            "callers (tests last): wiremap callers <symbol id>",
            "entry points",
        ],
        PACK_LINES - len(head) - len(fl) - 1,
    )
    return "\n".join(head + fl + body + [STOP]), 0


def install_skill() -> str:
    src = next(
        p
        for p in (
            Path(__file__).parent / "skills" / "wiremap" / "SKILL.md",
            Path(__file__).parents[2] / "skills" / "wiremap" / "SKILL.md",
        )
        if p.exists()
    )
    dst = Path.home() / ".claude" / "skills" / "wiremap" / "SKILL.md"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(src.read_bytes())
    return f"installed {dst}"


def cache_prune(root: Path, days: int = 30) -> str:
    cutoff = time.time() - days * 86400
    n = 0
    # ponytail: agent-authored notes are unreproducible, so pruning never touches
    # summaries/*.txt; upgrade: none wanted
    entries = list(cache_dir(root).glob("*.json"))
    entries += list((cache_dir(root) / "summaries").glob("*.tmp"))
    for p in entries:
        if p.stat().st_mtime < cutoff:
            p.unlink()
            n += 1
    return f"pruned {n} entries"


# ponytail: imports are excluded, their targets are modules and never resolve;
# upgrade: include them once module nodes exist in the graph
_STRUCT = {"calls", "depends", "graph_edge", "relationship", "task", "table_ref"}


def _adjacency(g: Graph) -> dict[str, set[str]]:
    nbrs: dict[str, set[str]] = defaultdict(set)
    for e in g.edges:
        if e.kind in _STRUCT:
            nbrs[e.src].add(e.dst)
            nbrs[e.dst].add(e.src)
    return nbrs


def community_of(
    g: Graph, nbrs: dict[str, set[str]] | None = None
) -> dict[str, str]:
    nbrs = _adjacency(g) if nbrs is None else nbrs
    label = {n: n for n in nbrs}
    # ponytail: label propagation in sorted order (deterministic);
    # upgrade: Leiden via igraph if clusters look wrong
    for _ in range(20):
        changed = False
        for n in sorted(nbrs):
            best = min(
                Counter(label[m] for m in nbrs[n]).items(),
                key=lambda kv: (-kv[1], kv[0]),
            )[0]
            if best != label[n]:
                label[n] = best
                changed = True
        if not changed:
            break
    return label


def communities(g: Graph, nbrs: dict[str, set[str]] | None = None) -> str:
    nbrs = _adjacency(g) if nbrs is None else nbrs
    label = community_of(g, nbrs)
    groups: dict[str, list[str]] = defaultdict(list)
    for n, l in label.items():
        groups[l].append(n)
    rows = []
    for members in sorted(groups.values(), key=lambda m: (-len(m), m[0])):
        top = sorted(members, key=lambda n: (-len(nbrs[n]), n))[:5]
        rows.append(
            f"{len(members)} symbols  hub={top[0]} ({len(nbrs[top[0]])} edges)  "
            f"{', '.join(top[1:])}"
        )
    return "\n".join(_cap(rows) or ["no edges"])


NODE_CAP = 1500


def _select(
    g: Graph, files: list[str] | None = None, symbol: str | None = None
) -> tuple[list[str], list[Edge]]:
    # ponytail: a workspace prefixes file paths with "<root>:"; strip it so
    # --files matches; upgrade: pass the root through if prefixes ever nest
    def bare(f: str) -> str:
        return f.split(":", 1)[-1] if ":" in f else f

    keep = (
        {symbol}
        if symbol
        else {
            s.id
            for s in g.symbols.values()
            if not files or bare(s.file) in files
        }
    )
    hit = [e for e in g.edges if e.src in keep or e.dst in keep]
    # ponytail: one row per (src, dst, kind); EXTRACTED wins over INFERRED;
    # upgrade: keep every line number if a UI ever needs them
    seen: dict[tuple[str, str, str], Edge] = {}
    for e in sorted(hit, key=lambda e: e.confidence != "EXTRACTED"):
        seen.setdefault((e.src, e.dst, e.kind), e)
    edges = list(seen.values())
    nodes = sorted({e.src for e in edges} | {e.dst for e in edges} | keep)
    if len(nodes) > NODE_CAP:
        print(
            f"graph: {len(nodes)} nodes, capped to {NODE_CAP}; use --files",
            file=sys.stderr,
        )
        deg: Counter = Counter()
        for e in edges:
            deg[e.src] += 1
            deg[e.dst] += 1
        nodes = sorted(sorted(nodes, key=lambda n: (-deg[n], n))[:NODE_CAP])
        ns = set(nodes)
        edges = [e for e in edges if e.src in ns and e.dst in ns]
    return nodes, edges


def to_mermaid(edges: list[Edge]) -> str:
    def nid(i: str) -> str:
        return re.sub(r"\W", "_", i)

    def lbl(i: str) -> str:
        return i.replace('"', "#quot;")

    rows = [
        f'  {nid(e.src)}["{lbl(e.src)}"] -->'
        f'|{e.kind}{"" if e.confidence == "EXTRACTED" else "?"}| '
        f'{nid(e.dst)}["{lbl(e.dst)}"]'
        for e in edges[:CAP]
    ]
    omitted = (
        [f"  %% {len(edges) - CAP} more edges omitted"] if len(edges) > CAP else []
    )
    return "\n".join(["graph LR"] + rows + omitted)


def _dq(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def to_dot(edges: list[Edge]) -> str:
    return (
        "digraph wiremap {\n  rankdir=LR;\n"
        + "".join(
            f'  "{_dq(e.src)}" -> "{_dq(e.dst)}" [label="{_dq(e.kind)}"'
            f'{", style=dashed" if e.confidence == "INFERRED" else ""}];\n'
            for e in edges
        )
        + "}"
    )


def to_graphml(nodes: list[str], edges: list[Edge], comm: dict[str, str]) -> str:
    n = "".join(
        f'<node id="{html.escape(i)}">'
        f'<data key="c">{html.escape(comm.get(i, i))}</data></node>'
        for i in nodes
    )
    e = "".join(
        f'<edge source="{html.escape(x.src)}" target="{html.escape(x.dst)}">'
        f'<data key="k">{x.kind}</data>'
        f'<data key="f">{x.confidence}</data></edge>'
        for x in edges
    )
    return (
        '<?xml version="1.0"?>'
        '<graphml xmlns="http://graphml.graphdrawing.org/xmlns">'
        '<key id="c" for="node" attr.name="community" attr.type="string"/>'
        '<key id="k" for="edge" attr.name="kind" attr.type="string"/>'
        '<key id="f" for="edge" attr.name="confidence" attr.type="string"/>'
        f'<graph edgedefault="directed">{n}{e}</graph></graphml>'
    )


def to_cypher(nodes: list[str], edges: list[Edge], comm: dict[str, str]) -> str:
    q = json.dumps
    return "\n".join(
        [
            f"MERGE (n:Symbol {{id: {q(i)}}}) SET n.community = {q(comm.get(i, i))};"
            for i in nodes
        ]
        + [
            f"MATCH (a:Symbol {{id: {q(e.src)}}}), (b:Symbol {{id: {q(e.dst)}}}) "
            f"MERGE (a)-[:{e.kind.upper()} {{confidence: {q(e.confidence)}}}]->(b);"
            for e in edges
        ]
    )


def to_html(
    g: Graph, nodes: list[str], edges: list[Edge], comm: dict[str, str]
) -> str:
    def rec(i: str) -> dict:
        s = g.symbols.get(i)
        return {
            "id": i,
            "file": s.file if s else "",
            "line": s.line_start if s else 0,
            "kind": s.kind
            if s
            else ("table" if i.startswith("sql:") else "module"),
            "community": comm.get(i, i),
        }

    data = {
        "nodes": [rec(i) for i in nodes],
        "edges": [
            {"src": e.src, "dst": e.dst, "kind": e.kind, "confidence": e.confidence}
            for e in edges
        ],
    }
    viewer = (Path(__file__).parent / "viewer.html").read_text()
    # ponytail: escaping "</" is what stops a path closing the <script> block;
    # upgrade: a JSON <script type="application/json"> block if more escaping creeps in
    return viewer.replace("/*DATA*/", json.dumps(data).replace("</", "<\\/"))


def graph(
    g: Graph,
    roots: list[Path],
    files: list[str] | None,
    symbol: str | None,
    fmt: str,
    html_out: Path | None = None,
) -> tuple[str, int]:
    root = roots[0]
    rels = None
    if files:
        rels = []
        for p in files:
            rel = _rel(root, p)
            if rel is None:
                return f"not found: {p}", 1
            rels.append(rel)
    if symbol:
        ids, code = _find(g, symbol)
        if code:
            if ids:
                return "\n".join(["ambiguous, candidates:"] + ids), 1
            return f"not found: {symbol}", 1
        symbol = ids[0]
    nodes, edges = _select(g, rels, symbol)
    if html_out:
        try:
            _refuse_inside_repo(roots, html_out)
        except RepoError as exc:
            return str(exc), 2
        try:
            write_atomic(html_out, to_html(g, nodes, edges, community_of(g)))
        except OSError:
            return f"cannot write {html_out}", 2
        return f"wrote {html_out} ({len(nodes)} nodes, {len(edges)} edges)", 0
    # ponytail: exactly one branch runs, so community_of is computed lazily by
    # being inside the lambdas that need it; upgrade: none
    out = {
        "mermaid": lambda: to_mermaid(edges),
        "dot": lambda: to_dot(edges),
        "graphml": lambda: to_graphml(nodes, edges, community_of(g)),
        "cypher": lambda: to_cypher(nodes, edges, community_of(g)),
    }
    return out[fmt](), 0


def _summary_entry(root: Path, f: str, src: bytes) -> Path:
    key = hashlib.sha1(f.encode() + b"\0" + src).hexdigest()
    return cache_dir(root) / "summaries" / f"{key}.txt"


def _cached_summary(root: Path, f: str) -> str:
    try:
        src = (root / f).read_bytes()
    except OSError:
        return ""
    p = _summary_entry(root, f, src)
    return p.read_text() if p.exists() else ""


def summarize(g: Graph, root: Path, files: list[str]) -> tuple[str, int]:
    rels = []
    for p in files:
        f = _rel(root, p)
        if f is None:
            return f"not found: {p}", 1
        rels.append(f)
    out = []
    for f in rels:
        text = _cached_summary(root, f)
        out += [f"## {f}"] + (
            text.splitlines()[:10]
            if text
            else [
                f"(no summary yet — write one: "
                f"wiremap summarize --write {f} < notes.txt)"
            ]
        )
    return "\n".join(out), 0


def summarize_write(root: Path, file: str, text: str) -> tuple[str, int]:
    f = _rel(root, file)
    if f is None:
        return f"not found: {file}", 1
    text = "\n".join(text.strip().splitlines()[:10])
    if not text:
        return "empty summary; nothing written", 1
    try:
        src = (root / f).read_bytes()
    except OSError:
        return f"cannot read {f}", 1
    entry = _summary_entry(root, f, src)
    try:
        write_atomic(entry, text)
    except OSError:
        return f"cannot write {entry}", 2
    return (
        f"stored {len(text.splitlines())} lines for {f} "
        f"(content {entry.stem[:12]})",
        0,
    )


def _refuse_inside_repo(roots: list[Path], out: Path) -> None:
    out = out.resolve()
    for r in roots:
        if not out.is_relative_to(r.resolve()):
            continue
        try:
            _git(r, "check-ignore", "-q", str(out))
        except subprocess.CalledProcessError:
            raise RepoError(
                f"{out} is inside the repo and not gitignored"
                " (a 'dir/' pattern only matches once the directory exists"
                " — drop the trailing slash or mkdir it first)"
            ) from None


def export_vault(g: Graph, roots: list[Path], out: Path) -> str:
    root = roots[0]
    _refuse_inside_repo(roots, out)
    comm = community_of(g)
    by_file: dict[str, list[Symbol]] = defaultdict(list)
    for s in g.symbols.values():
        by_file[s.file].append(s)

    def link(i: str) -> str:
        return f"[[{g.symbols[i].file}|{i}]]" if i in g.symbols else f"`{i}`"

    for f, syms in by_file.items():
        page = out / (f + ".md")
        ids = {s.id for s in syms}
        calls = sorted({e.dst for e in g.edges if e.src in ids and e.dst not in ids})
        called = sorted({e.src for e in g.edges if e.dst in ids and e.src not in ids})
        text = (
            "\n".join(
                [
                    f"# {f}",
                    _cached_summary(root, f),
                    "## Skeleton",
                    "```",
                    skeleton(g, root, [f])[0],
                    "```",
                    f"community: {comm.get(syms[0].id, '-')}",
                    "## Calls",
                ]
                + [f"- {link(i)}" for i in calls]
                + ["## Called by"]
                + [f"- {link(i)}" for i in called]
            )
            + "\n"
        )
        try:
            write_atomic(page, text)
        except OSError as exc:
            raise RepoError(f"cannot write {page}") from exc
    return f"wrote {len(by_file)} pages under {out}"


def report(g: Graph, root: Path) -> str:
    nbrs = _adjacency(g)
    deg = sorted(nbrs, key=lambda n: (-len(nbrs[n]), n))
    dir_of = {s.id: str(Path(s.file).parent) for s in g.symbols.values()}
    dirs = Counter(dir_of.values())
    first: dict[str, str] = {}
    for n in deg:
        d = dir_of.get(n)
        if d is not None and d not in first:
            first[d] = n
    hub_of = {d: first.get(d, "-") for d in dirs}
    return "\n".join(
        [
            f"# wiremap report — HEAD {head_stamp(root)}",
            f"files={g.stats.get('files')} symbols={len(g.symbols)} "
            f"edges={len(g.edges)} unresolved={sum(g.unresolved.values())}",
            "## Hubs",
        ]
        + _top([f"- {n} ({len(nbrs[n])} edges)" for n in deg], 10)
        + ["## Directories"]
        + _top(
            [f"- {d}: {c} symbols, hub {hub_of[d]}" for d, c in dirs.most_common()], 15
        )
        + ["## Communities"]
        + _top(communities(g, nbrs).splitlines(), 10)
        + ["## Entry points"]
        + _top(entrypoints(g).splitlines(), 15)
        + ["## Unresolved (ambiguous names)"]
        + _top(
            [
                f"- {k}: {v} candidates"
                for k, v in sorted(
                    g.unresolved.items(), key=lambda kv: (-kv[1], kv[0])
                )
            ],
            10,
        )
    )


def _ask_rows(g: Graph, text: str) -> list[tuple[Symbol, int, int]]:
    words = {w.lower() for w in re.findall(r"[A-Za-z_]{4,}", text)}
    nbrs = _adjacency(g)
    hits = []
    for s in g.symbols.values():
        ql = s.qualname.lower()
        matches = sum(w in ql for w in words)
        if matches:
            hits.append((s, matches, len(nbrs[s.id])))
    return sorted(hits, key=lambda h: (-h[1], -h[2], h[0].id))


def ask(g: Graph, text: str) -> str:
    rows = [
        f"{s.id}  {s.file}:{s.line_start}  matches={m} edges={d}"
        for s, m, d in _ask_rows(g, text)
    ]
    return "\n".join(_cap(rows) or ["no symbol names match the task text"])


_HUNK = re.compile(r"@@ -\S+ \+(\d+)(?:,(\d+))?")


_DIFF_OPTS = (
    "-c",
    "core.quotePath=false",
    "diff",
    "-U0",
    "--src-prefix=a/",
    "--dst-prefix=b/",
)


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
IMPACT_PER_SYMBOL = 3
CANDIDATES = (
    "Callers are candidates: string dispatch is invisible \u2014 "
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
            e.dst,
        )
        for e in g.edges
        if e.dst in changed
        and e.src not in changed
        and e.kind not in ("mentions", "imports")
    )
    tests = sum(1 for c, t, _, _ in hits if not c and t)
    leads = [f"unconfirmed (name-only): {r}" for c, _, r, _ in hits if c]
    by_dst: dict[str, list[str]] = defaultdict(list)
    for c, t, r, dst in hits:
        if not c and not t:
            by_dst[dst].append(r)
    fact = []
    for dst, rows in by_dst.items():
        fact += rows[:IMPACT_PER_SYMBOL]
        if len(rows) > IMPACT_PER_SYMBOL:
            fact.append(
                f"  \u2026 {len(rows) - IMPACT_PER_SYMBOL} more callers of {dst}: "
                f"wiremap callers {dst}"
            )
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


def triage(g: Graph, root: Path, base: str = "main") -> tuple[str, int]:
    changed = _changed(g, root, base)
    if changed is None:
        return f"unknown --base {base}", 2
    if not changed:
        return "no changes", 0
    by_file: dict[str, set[str]] = defaultdict(set)
    docs: dict[str, set[str]] = defaultdict(set)
    for e in g.edges:
        if e.dst in changed and e.src not in changed:
            bucket = docs if e.kind == "mentions" else by_file
            bucket[e.file].add(e.dst.split(".")[-1])
    rows = [
        f"{f}  {len(v)} changed symbols used: {', '.join(sorted(v)[:3])}"
        for f, v in sorted(by_file.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    ]
    doc_line = []
    if docs:
        ranked = sorted(docs.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        shown = ", ".join(f"{f} ({len(v)})" for f, v in ranked[:10])
        extra = f" … and {len(ranked) - 10} more" if len(ranked) > 10 else ""
        doc_line = [f"docs mentioning changed symbols: {shown}{extra}"]
    eps = sorted(
        {e.dst for e in g.edges if e.kind in ("route", "task") and e.dst in changed}
    )
    return (
        "\n".join(
            [f"changed: {len(changed)} symbols"]
            + _cap(rows)
            + doc_line
            + _cap([f"entrypoint touched: {e}" for e in eps])
        ),
        0,
    )


HOOK_CAP, HOOK_MIN_NAME = 20, 6


def hook_post_edit(g: Graph, root: Path, payload: str) -> str:
    try:
        rel = _rel(root, json.loads(payload)["tool_input"]["file_path"])
    except (ValueError, KeyError, TypeError):
        return ""
    if rel is None:
        return ""
    # ponytail: names shorter than 6 chars (get, run) would dump thousands of rows
    # on a big repo; upgrade: a per-symbol cap instead of a name-length gate
    rows = [
        f"{s.name} <- {e.src} ({e.kind}) {e.file}:{e.line}"
        for s in _symbols_in(g, rel)
        if len(s.name) >= HOOK_MIN_NAME
        for e in g.edges
        if e.dst == s.id
        and e.confidence == "EXTRACTED"
        and e.kind != "mentions"
        and e.file != rel
    ]
    if not rows:
        return ""
    more = [f"… and {len(rows) - HOOK_CAP} more"] if len(rows) > HOOK_CAP else []
    text = "\n".join(
        ["wiremap: cross-file callers of edited symbols"] + rows[:HOOK_CAP] + more
    )
    # ponytail: plain PostToolUse stdout never reaches the model; additionalContext does
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": text,
            }
        }
    )


HOOK_JSON = (
    '{"matcher": "Write|Edit", "hooks": [{"type": "command", "command": '
    '"command -v wiremap >/dev/null && wiremap hook post-edit || true", '
    '"timeout": 10, "statusMessage": '
    '"wiremap: cross-file callers"}]}'
)
STATUS_LINE = (
    "in=$(cat); printf '%s' \"$in\" | <EXISTING statusLine.command>; printf \" \"; "
    "wiremap --repo \"$(printf '%s' \"$in\" | "
    "jq -r '.workspace.current_dir // \".\"' 2>/dev/null)\" status 2>/dev/null || true"
)


def install_hook() -> str:
    return (
        "add to hooks.PostToolUse in ~/.claude/settings.json:\n"
        + HOOK_JSON
        + "\nreplace statusLine.command with (keep your existing command where "
        "marked; stdin is captured once and replayed to it):\n"
        + STATUS_LINE
    )


def status(root: Path) -> str:
    # ponytail: never builds — the statusline runs every few seconds;
    # upgrade: none, a stale segment is better than a stalled prompt
    p = cache_dir(root) / "last_stats.json"
    try:
        d = json.loads(p.read_text())
        return (
            f"wiremap {d['files']}f {d['edges']}e "
            f"unresolved={d['unresolved']} @{d['stamp'].split()[0]}"
        )
    except (OSError, ValueError, KeyError, IndexError):
        return ""

