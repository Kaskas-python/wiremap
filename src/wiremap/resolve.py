import sys
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path

from wiremap.discover import RepoError, files
from wiremap.parse import RawEdge, Symbol, load_or_parse


@dataclass(frozen=True)
class Edge:
    src: str
    dst: str
    kind: str
    confidence: str
    file: str
    line: int


@dataclass
class Graph:
    symbols: dict[str, Symbol]
    edges: list[Edge]
    unresolved: dict[str, int]
    stats: dict[str, int]


def index(symbols: list[Symbol]) -> tuple[dict[str, Symbol], dict[str, list[str]]]:
    by_id = {s.id: s for s in symbols}
    by_name: dict[str, list[str]] = {}
    for s in symbols:
        if s.id not in by_name.setdefault(s.name, []):
            by_name[s.name].append(s.id)
    return by_id, by_name


def _resolve(
    raw: RawEdge,
    module: str,
    imports: dict[str, str],
    by_id: dict[str, Symbol],
    by_name: dict[str, list[str]],
    unresolved: dict[str, int],
) -> tuple[str, str] | None:
    t = raw.target_text.split(".")[-1]
    if (cand := f"{module}.{t}") in by_id:
        return cand, "EXTRACTED"
    if t in imports and imports[t] in by_id:
        return imports[t], "EXTRACTED"
    ids = by_name.get(t, [])
    if len(ids) == 1:
        return ids[0], "INFERRED"
    if len(ids) > 1:
        unresolved[t] = len(ids)
    return None


def module_of(file: str) -> str:
    return file.rsplit(".", 1)[0].replace("/", ".")


_FRAMEWORK_KINDS = (
    "depends",
    "route",
    "graph_node",
    "graph_edge",
    "relationship",
    "task",
)


def build(root: Path, stats: bool = False) -> Graph:
    # ponytail: stats always collected; flag kept for the CLI contract
    syms: list[Symbol] = []
    raws: list[RawEdge] = []
    hits = failed = 0
    paths = files(root)
    for path, lang in paths:
        try:
            s, e, hit = load_or_parse(path, root, lang)
        except Exception as exc:  # noqa: BLE001
            print(f"warning: {path}: {exc}", file=sys.stderr)
            failed += 1
            continue
        syms += s
        raws += e
        hits += hit
    n_files = len(paths)
    if failed > n_files * 0.1:
        raise RepoError(f"{failed}/{n_files} files failed to parse")

    by_id, by_name = index(syms)
    edges, unresolved, labels = [], {}, {}
    imports: dict[str, dict[str, str]] = {}
    for raw in raws:
        if raw.kind == "imports":
            local = raw.target_text.split(".")[-1]
            imports.setdefault(raw.file, {})[local] = raw.target_text

    def imports_for(file: str) -> dict[str, str]:
        return imports.get(file, {})

    node_first = [r for r in raws if r.kind == "graph_node"]
    node_first += [r for r in raws if r.kind != "graph_node"]
    for raw in node_first:
        if raw.kind == "imports":
            continue
        src = raw.src
        if raw.target_text.startswith("self:"):
            dst = raw.target_text[5:]
            conf = "EXTRACTED"
        elif raw.target_text.startswith("node:"):
            label, name = raw.target_text[5:].split("=", 1)
            r = _resolve(
                replace(raw, target_text=name),
                module_of(raw.file),
                imports_for(raw.file),
                by_id,
                by_name,
                unresolved,
            )
            dst, conf = r if r else (None, None)
            if dst:
                labels[label] = dst
        elif raw.target_text.startswith("label:"):
            a, b = raw.target_text[6:].split("->")
            dst = labels.get(b) or (by_name.get(b) or [None])[0]
            conf = "EXTRACTED" if b in labels else "INFERRED"
            src = labels.get(a, raw.src)
        else:
            r = _resolve(
                raw,
                module_of(raw.file),
                imports_for(raw.file),
                by_id,
                by_name,
                unresolved,
            )
            dst, conf = r if r else (None, None)
        if dst:
            edges.append(Edge(src, dst, raw.kind, conf, raw.file, raw.line))

    rule_counts = Counter(f"rule_{r.kind}" for r in raws if r.kind in _FRAMEWORK_KINDS)
    return Graph(
        by_id,
        edges,
        unresolved,
        {"files": n_files, "cache_hits": hits, "failed": failed, **rule_counts},
    )
