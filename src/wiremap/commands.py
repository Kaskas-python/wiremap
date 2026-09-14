import json
import subprocess
import time
from collections import defaultdict
from pathlib import Path

from wiremap.discover import head_stamp
from wiremap.parse import Symbol, cache_dir
from wiremap.resolve import Graph

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


_DYNAMIC = ("getattr(", "importlib", "globals()[")


def deps(g: Graph, root: Path, target: str, depth: int = 1) -> tuple[str, int]:
    frontier = {s.id for s in g.symbols.values() if s.file == target}
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
    return file.rsplit(".", 1)[0].replace("/", ".") + "."


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
        ln
        for ln in skeleton(g, root, rels)[0].splitlines()
        if ln != "no symbols in given files"
    ]
    sections = [ep, fl, ca, sk]
    for sec in (sk, ca, ep):
        while len(head) + sum(map(len, sections)) + 1 > 15 and sec:
            sec.pop()
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
    for p in cache_dir(root).glob("*.json"):
        if p.stat().st_mtime < cutoff:
            p.unlink()
            n += 1
    return f"pruned {n} entries"
