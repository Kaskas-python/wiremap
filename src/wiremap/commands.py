import json
import subprocess
from collections import defaultdict
from pathlib import Path

from wiremap.discover import head_stamp
from wiremap.parse import Symbol
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
    involved = {g.symbols[i].file for i in start | seen if i in g.symbols}
    notices = []
    for file in sorted(involved):
        for ln, line in enumerate((root / file).read_text().splitlines(), 1):
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


def skeleton(g: Graph, paths: list[str]) -> str:
    rows = []
    for s in sorted(g.symbols.values(), key=lambda s: (s.file, s.line_start)):
        if s.file in paths:
            rows.append("  " * s.qualname.count(".") + s.signature)
    return "\n".join(rows or ["no symbols in given files"])


def _enclosing_symbol(g: Graph, file: str, line: int) -> str | None:
    spans = [
        (s.line_end - s.line_start, s.id)
        for s in g.symbols.values()
        if s.file == file and s.line_start <= line <= s.line_end
    ]
    return min(spans)[1] if spans else None


def grep(g: Graph, root: Path, pattern: str) -> tuple[str, int]:
    try:
        out = subprocess.run(
            ["rg", "-n", "--json", pattern],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        ).stdout
    except FileNotFoundError:
        return "rg (ripgrep) is required for grep", 2
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


def pack(g: Graph, root: Path, files: list[str], task: str | None = None) -> str:
    head = [f"HEAD {head_stamp(root)}"] + ([f"task: {task}"] if task else [])
    ep = [
        line
        for line in entrypoints(g).splitlines()
        if any(line.startswith(_id_prefix(f)) for f in files)
    ]
    fl = [f"files: {', '.join(files)}"]
    ca = [
        f"caller: {r}"
        for f in files
        for s in _symbols_in(g, f)
        for r in callers(g, s.id)[0].splitlines()
        if "  " in r and not r.startswith("unresolved")
    ]
    sk = [
        ln
        for ln in skeleton(g, files).splitlines()
        if ln != "no symbols in given files"
    ]
    body = ep + fl + ca + sk
    for drop in (sk, ca, ep):
        if len(head) + len(body) + 1 <= 15:
            break
        body = [line for line in body if line not in drop]
    return "\n".join(head + body + [STOP])
