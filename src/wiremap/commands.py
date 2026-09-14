import hashlib
import html
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

from wiremap.discover import RepoError, _git, head_stamp
from wiremap.parse import Symbol, cache_dir, module_of
from wiremap.resolve import Edge, Graph

CAP = 40


def _cap(lines: list[str]) -> list[str]:
    return (
        lines if len(lines) <= CAP else lines[:CAP] + [f"… and {len(lines) - CAP} more"]
    )


def _find(g: Graph, symbol: str) -> tuple[list[str], int]:
    if symbol in g.symbols:
        return [symbol], 0
    ids = [s.id for s in g.symbols.values() if s.name == symbol]
    if len(ids) == 1:
        return ids, 0
    return ids, 1


def callers(
    g: Graph, symbol: str, depth: int = 1, min_conf: str = "INFERRED"
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
    return "\n".join(body + [f"unresolved: {unresolved}"]), 0


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


_DYNAMIC = ("getattr(", "importlib", "globals()[")


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
        if s.file in rels:
            rows.append("  " * s.qualname.count(".") + s.signature)
    return "\n".join(_cap(rows) or ["no symbols in given files"]), 0


def _enclosing_symbol(g: Graph, file: str, line: int) -> str | None:
    spans = [
        (s.line_end - s.line_start, s.id)
        for s in g.symbols.values()
        if s.file == file and s.line_start <= line <= s.line_end
    ]
    return min(spans)[1] if spans else None


def grep(g: Graph, root: Path, pattern: str) -> tuple[str, int]:
    try:
        proc = subprocess.run(
            ["rg", "-n", "--json", pattern],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return "rg (ripgrep) is required for grep", 2
    if proc.returncode == 2:
        return proc.stderr.strip(), 2
    out = proc.stdout
    hits = [
        (
            m["data"]["path"]["text"],
            m["data"]["line_number"],
            m["data"]["lines"]["text"].rstrip(),
        )
        for m in map(json.loads, out.splitlines())
        if m["type"] == "match"
    ]
    groups = defaultdict(list)
    for f, ln, text in hits:
        groups[_enclosing_symbol(g, f, ln) or f].append(f"  {f}:{ln}: {text}")
    body = [
        row
        for k, v in sorted(groups.items())
        for row in [f"{k} ({len(v)} hits)"] + v[:3]
    ]
    return "\n".join(_cap(body) or ["no matches"]), 0


STOP = "If this pack contradicts the code or HEAD differs, STOP and report BLOCKED."


def _id_prefix(file: str) -> str:
    return module_of(file) + "."


def _symbols_in(g: Graph, file: str) -> list[Symbol]:
    return sorted(
        (s for s in g.symbols.values() if s.file == file), key=lambda s: s.line_start
    )


def pack(
    g: Graph, root: Path, files: list[str], task: str | None = None
) -> tuple[str, int]:
    rels = []
    for p in files:
        rel = _rel(root, p)
        if rel is None:
            return f"not found: {p}", 1
        rels.append(rel)
    head = [f"HEAD {head_stamp(root)}"] + ([f"task: {task}"] if task else [])
    ep = [
        line
        for line in entrypoints(g).splitlines()
        if any(line.startswith(_id_prefix(f)) for f in rels)
    ]
    fl = [f"files: {', '.join(rels)}"]
    ca = [
        f"caller: {r}"
        for f in rels
        for s in _symbols_in(g, f)
        for r in callers(g, s.id)[0].splitlines()
        if "  " in r and not r.startswith("unresolved")
    ]
    sk = [
        "  " * s.qualname.count(".") + s.signature
        for f in rels
        for s in _symbols_in(g, f)
    ]
    matched = ask(g, task) if task else ""
    rel = (
        [f"related: {r}" for r in matched.splitlines()[:5]]
        if matched and not matched.startswith("no symbol")
        else []
    )
    sections = [ep, fl, ca, rel, sk]
    for sec in (rel, sk, ca, ep):
        dropped = 0
        while len(head) + sum(map(len, sections)) + 1 > 15 and sec:
            sec.pop()
            dropped += 1
        if dropped and sec:
            sec[-1] = f"… and {dropped + 1} more"
    return "\n".join(head + [ln for s in sections for ln in s] + [STOP]), 0


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
    entries = list(cache_dir(root).glob("*.json"))
    summaries = cache_dir(root) / "summaries"
    entries += list(summaries.glob("*.txt")) + list(summaries.glob("*.tmp"))
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


def community_of(g: Graph) -> dict[str, str]:
    nbrs = _adjacency(g)
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


def communities(g: Graph) -> str:
    nbrs, label = _adjacency(g), community_of(g)
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


NODE_CAP = 5000


def _select(
    g: Graph, files: list[str] | None = None, symbol: str | None = None
) -> tuple[list[str], list[Edge]]:
    keep = (
        {symbol}
        if symbol
        else {s.id for s in g.symbols.values() if not files or s.file in files}
    )
    edges = [e for e in g.edges if e.src in keep or e.dst in keep]
    nodes = sorted({e.src for e in edges} | {e.dst for e in edges} | keep)
    if len(nodes) > NODE_CAP:
        print(
            f"graph: {len(nodes)} nodes, capped to {NODE_CAP}; use --files",
            file=sys.stderr,
        )
        nodes = nodes[:NODE_CAP]
        ns = set(nodes)
        edges = [e for e in edges if e.src in ns and e.dst in ns]
    return nodes, edges


def to_mermaid(nodes: list[str], edges: list[Edge]) -> str:
    def nid(i: str) -> str:
        return re.sub(r"\W", "_", i)

    rows = [
        f'  {nid(e.src)}["{e.src}"] -->'
        f'|{e.kind}{"" if e.confidence == "EXTRACTED" else "?"}| '
        f'{nid(e.dst)}["{e.dst}"]'
        for e in edges[:CAP]
    ]
    omitted = (
        [f"  %% {len(edges) - CAP} more edges omitted"] if len(edges) > CAP else []
    )
    return "\n".join(["graph LR"] + rows + omitted)


def to_dot(nodes: list[str], edges: list[Edge]) -> str:
    return (
        "digraph wiremap {\n  rankdir=LR;\n"
        + "".join(
            f'  "{e.src}" -> "{e.dst}" [label="{e.kind}"'
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
    root: Path,
    files: list[str] | None,
    symbol: str | None,
    fmt: str,
    html_out: Path | None = None,
) -> tuple[str, int]:
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
    comm = community_of(g)
    if html_out:
        tmp = html_out.with_name(f"{html_out.stem}.{os.getpid()}.tmp")
        try:
            tmp.write_text(to_html(g, nodes, edges, comm))
            tmp.replace(html_out)
        except OSError:
            return f"cannot write {html_out}", 2
        return f"wrote {html_out} ({len(nodes)} nodes, {len(edges)} edges)", 0
    out = {
        "mermaid": lambda: to_mermaid(nodes, edges),
        "dot": lambda: to_dot(nodes, edges),
        "graphml": lambda: to_graphml(nodes, edges, comm),
        "cypher": lambda: to_cypher(nodes, edges, comm),
    }
    return out[fmt](), 0


def _cached_summary(root: Path, f: str) -> str:
    try:
        src = (root / f).read_bytes()
    except OSError:
        return ""
    p = cache_dir(root) / "summaries" / f"{hashlib.sha1(src).hexdigest()}.txt"
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
    src = (root / f).read_bytes()
    entry = cache_dir(root) / "summaries" / f"{hashlib.sha1(src).hexdigest()}.txt"
    tmp = entry.with_name(f"{entry.stem}.{os.getpid()}.tmp")
    try:
        entry.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(text)
        tmp.replace(entry)
    except OSError:
        return f"cannot write {entry}", 2
    return (
        f"stored {len(text.splitlines())} lines for {f} "
        f"(content {entry.stem[:12]})",
        0,
    )


def export_vault(g: Graph, root: Path, out: Path) -> str:
    inside = out.resolve().is_relative_to(root.resolve())
    if (
        inside
        and subprocess.run(
            ["git", "-C", str(root), "check-ignore", "-q", str(out)]
        ).returncode
        != 0
    ):
        raise RepoError(
            f"{out} is inside the repo and not gitignored"
            " (a 'vault/' pattern only matches once the directory exists"
            " — use 'vault' or mkdir it first)"
        )
    comm = community_of(g)
    by_file: dict[str, list[Symbol]] = defaultdict(list)
    for s in g.symbols.values():
        by_file[s.file].append(s)

    def link(i: str) -> str:
        return f"[[{g.symbols[i].file}|{i}]]" if i in g.symbols else f"`{i}`"

    for f, syms in by_file.items():
        page = out / (f + ".md")
        page.parent.mkdir(parents=True, exist_ok=True)
        ids = {s.id for s in syms}
        calls = sorted({e.dst for e in g.edges if e.src in ids and e.dst not in ids})
        called = sorted({e.src for e in g.edges if e.dst in ids and e.src not in ids})
        page.write_text(
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
    return f"wrote {len(by_file)} pages under {out}"


def _top(rows: list[str], n: int) -> list[str]:
    return rows if len(rows) <= n else rows[:n] + [f"… and {len(rows) - n} more"]


def report(g: Graph, root: Path) -> str:
    nbrs = _adjacency(g)
    deg = sorted(nbrs, key=lambda n: (-len(nbrs[n]), n))
    dirs = Counter(str(Path(s.file).parent) for s in g.symbols.values())
    hub_of = {
        d: next(
            (
                n
                for n in deg
                if n in g.symbols and str(Path(g.symbols[n].file).parent) == d
            ),
            "-",
        )
        for d in dirs
    }
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
        + _top(communities(g).splitlines(), 10)
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


def ask(g: Graph, text: str) -> str:
    words = {w.lower() for w in re.findall(r"[A-Za-z_]{4,}", text)}
    nbrs = _adjacency(g)
    hits = [
        (sum(w in s.qualname.lower() for w in words), len(nbrs[s.id]), s)
        for s in g.symbols.values()
    ]
    rows = [
        f"{s.id}  {s.file}:{s.line_start}  matches={m} edges={d}"
        for m, d, s in sorted(hits, key=lambda t: (-t[0], -t[1], t[2].id))
        if m
    ]
    return "\n".join(_cap(rows) or ["no symbol names match the task text"])


_HUNK = re.compile(r"@@ -\S+ \+(\d+)(?:,(\d+))?")


def triage(g: Graph, root: Path, base: str = "main") -> str:
    try:
        diff = _git(root, "diff", "-U0", f"{base}...HEAD")
    except subprocess.CalledProcessError:
        diff = ""
    diff += "\n" + _git(root, "diff", "-U0")
    # ponytail: untracked files are invisible to triage;
    # upgrade: add `git ls-files --others` symbols as changed
    changed: set[str] = set()
    f = None
    for l in diff.splitlines():
        if l.startswith("+++ "):
            f = l[6:] if l.startswith("+++ b/") else None
        elif l.startswith("@@") and f and (m := _HUNK.match(l)):
            a = int(m[1])
            b = a + max(int(m[2] or 1), 1) - 1
            changed |= {
                s.id
                for s in g.symbols.values()
                if s.file == f and s.line_start <= b and a <= s.line_end
            }
    if not changed:
        return "no changes"
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
    return "\n".join(
        [f"changed: {len(changed)} symbols"]
        + _cap(rows)
        + doc_line
        + [f"entrypoint touched: {e}" for e in eps]
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
        if e.dst == s.id and e.confidence == "EXTRACTED" and e.file != rel
    ]
    if not rows:
        return ""
    more = [f"… and {len(rows) - HOOK_CAP} more"] if len(rows) > HOOK_CAP else []
    return "\n".join(
        ["wiremap: cross-file callers of edited symbols"] + rows[:HOOK_CAP] + more
    )


HOOK_JSON = (
    '{"matcher": "Write|Edit", "hooks": [{"type": "command", "command": '
    '"wiremap hook post-edit", "timeout": 5, "statusMessage": '
    '"wiremap: cross-file callers"}]}'
)
STATUS_LINE = (
    "in=$(cat); printf '%s' \"$in\" | <EXISTING statusLine.command>; printf \" \"; "
    "wiremap --repo \"$(printf '%s' \"$in\" | "
    "jq -r '.workspace.current_dir // \".\"' 2>/dev/null)\" status 2>/dev/null"
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

