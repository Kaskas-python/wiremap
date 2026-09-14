import json
import os
import sys
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path

from wiremap.discover import EXTS, RepoError, files, head_stamp
from wiremap.lsp import SERVERS, Lsp
from wiremap.parse import (
    LANG_SPECS,
    TEXT_LANGS,
    RawEdge,
    Symbol,
    cache_dir,
    language,
    load_or_parse,
    module_of,
)


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
    ambiguous: dict[str, list[str]]
    dangling: list[RawEdge] = field(default_factory=list)


def _lang_of(file: str) -> str:
    return EXTS[Path(file).suffix]


def index(
    symbols: list[Symbol],
) -> tuple[
    dict[str, Symbol], dict[tuple[str, str], list[str]], dict[str, list[str]]
]:
    by_id = {s.id: s for s in symbols}
    by_name: dict[tuple[str, str], list[str]] = {}
    by_bare: dict[str, list[str]] = {}
    for s in symbols:
        # ponytail: a table defined in both schema.sql and __tablename__ shares one
        # id; last file wins in by_id; upgrade: key tables by file if both must survive
        key = ("sql", s.name) if s.kind == "table" else (_lang_of(s.file), s.name)
        if s.id not in by_name.setdefault(key, []):
            by_name[key].append(s.id)
        if s.id not in by_bare.setdefault(s.name, []):
            by_bare[s.name].append(s.id)
    return by_id, by_name, by_bare


def _resolve(
    raw: RawEdge,
    module: str,
    imports: dict[str, str],
    by_id: dict[str, Symbol],
    by_name: dict[tuple[str, str], list[str]],
    by_bare: dict[str, list[str]],
    unresolved: dict[str, int],
) -> tuple[str, str] | None:
    lang = _lang_of(raw.file)
    if lang in TEXT_LANGS and raw.target_text in by_id:
        return raw.target_text, "EXTRACTED"
    t = raw.target_text.split(".")[-1]
    if (cand := f"{module}.{t}") in by_id:
        return cand, "EXTRACTED"
    if t in imports and imports[t] in by_id:
        return imports[t], "EXTRACTED"
    if lang in TEXT_LANGS:
        ids = by_bare.get(t, [])
        # ponytail: prose naming an ambiguous symbol is not a missing edge, so it
        # never writes unresolved; upgrade: report doc ambiguity separately if wanted
        return (ids[0], "INFERRED") if len(ids) == 1 else None
    ids = by_name.get((lang, t), [])
    if len(ids) == 1:
        return ids[0], "INFERRED"
    if len(ids) > 1:
        unresolved[t] = len(ids)
    return None


_FRAMEWORK_KINDS = (
    "depends",
    "route",
    "graph_node",
    "graph_edge",
    "relationship",
    "task",
)


def build(root: Path, lsp: bool = False, dangling: bool = False) -> Graph:
    syms: list[Symbol] = []
    raws: list[RawEdge] = []
    hits = failed = 0
    paths = files(root)
    missing: set[str] = set()
    skipped = 0
    for path, lang in paths:
        if lang in LANG_SPECS and language(lang) is None:
            missing.add(lang)
            skipped += 1
            continue
        try:
            s, e, hit = load_or_parse(path, root, lang)
        except Exception as exc:  # noqa: BLE001
            print(f"warning: {path}: {exc}", file=sys.stderr)
            failed += 1
            continue
        syms += s
        raws += e
        hits += hit
    n_files = len(paths) - skipped
    if failed > n_files * 0.1:
        raise RepoError(f"{failed}/{n_files} files failed to parse")

    by_id, by_name, by_bare = index(syms)
    edges, unresolved, labels, ambiguous = [], {}, {}, {}
    collect = lsp or dangling
    dangling_raws: list[RawEdge] = []
    pairs: list[tuple[RawEdge, int | None]] = []
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
            label, name = raw.target_text[5:].rsplit("=", 1)
            r = _resolve(
                replace(raw, target_text=name),
                module_of(raw.file),
                imports_for(raw.file),
                by_id,
                by_name,
                by_bare,
                unresolved,
            )
            dst, conf = r if r else (None, None)
            if dst:
                labels[(raw.file, label)] = dst
        elif raw.target_text.startswith("label:"):
            a, b = raw.target_text[6:].split("->", 1)
            if (raw.file, b) in labels:
                dst, conf = labels[(raw.file, b)], "EXTRACTED"
            else:
                r = _resolve(
                    replace(raw, target_text=b),
                    module_of(raw.file),
                    imports_for(raw.file),
                    by_id,
                    by_name,
                    by_bare,
                    unresolved,
                )
                dst, conf = r if r else (None, None)
            src = labels.get((raw.file, a), raw.src)
        elif raw.target_text.startswith(("sql:", "sqlref:")):
            dst = "sql:" + raw.target_text.split(":", 1)[1]
            dst = dst if dst in by_id else None
            conf = "EXTRACTED" if raw.target_text.startswith("sql:") else "INFERRED"
        else:
            r = _resolve(
                raw,
                module_of(raw.file),
                imports_for(raw.file),
                by_id,
                by_name,
                by_bare,
                unresolved,
            )
            dst, conf = r if r else (None, None)
            if r is None and collect and raw.kind == "calls":
                dangling_raws.append(raw)
            if r is None and raw.kind != "mentions":
                t = raw.target_text.split(".")[-1]
                if len(by_name.get((_lang_of(raw.file), t), [])) > 1:
                    ambiguous.setdefault(raw.src, []).append(t)
        if dst:
            if raw.kind == "calls" and conf == "INFERRED":
                pairs.append((raw, len(edges)))
            edges.append(Edge(src, dst, raw.kind, conf, raw.file, raw.line))

    pairs += [(raw, None) for raw in dangling_raws]
    rule_counts = Counter(f"rule_{r.kind}" for r in raws if r.kind in _FRAMEWORK_KINDS)
    if missing:
        names = ",".join(sorted(missing))
        print(
            f"no grammar for {', '.join(sorted(missing))}: "
            f"uv tool install 'wiremap[{names}]'",
            file=sys.stderr,
        )
    stats = {
        "files": n_files,
        "skipped_no_grammar": skipped,
        "cache_hits": hits,
        "failed": failed,
        **rule_counts,
    }
    if lsp:
        stats["lsp_upgraded"] = refine_with_lsp(
            root, edges, pairs, {(s.file, s.line_start): s.id for s in syms}
        )
    p = cache_dir(root) / "last_stats.json"
    tmp = p.with_name(f"{p.stem}.{os.getpid()}.tmp")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(
            json.dumps(
                {
                    "files": n_files,
                    "edges": len(edges),
                    "unresolved": sum(unresolved.values()),
                    "stamp": head_stamp(root),
                }
            )
        )
        tmp.replace(p)
    except OSError:
        # ponytail: the statusline cache is best-effort, same as the parse cache;
        # a failed write only means `status` prints nothing
        pass
    return Graph(by_id, edges, unresolved, stats, ambiguous, dangling_raws)


def build_many(roots: list[Path], lsp: bool = False) -> Graph:
    if len(roots) == 1:
        return build(roots[0], lsp)
    gs = [(r.name, build(r, lsp, dangling=True)) for r in roots]

    def pre(n: str, x: str) -> str:
        return f"{n}:{x}"

    symbols = {
        pre(n, s.id): replace(s, id=pre(n, s.id), file=pre(n, s.file))
        for n, g in gs
        for s in g.symbols.values()
    }
    edges = [
        replace(e, src=pre(n, e.src), dst=pre(n, e.dst), file=pre(n, e.file))
        for n, g in gs
        for e in g.edges
    ]
    by_name: dict[tuple[str, str], list[str]] = {}
    for s in symbols.values():
        key = ("sql", s.name) if s.kind == "table" else (_lang_of(s.file), s.name)
        by_name.setdefault(key, []).append(s.id)
    unresolved: dict[str, int] = {}
    for _, g in gs:
        for k, v in g.unresolved.items():
            unresolved[k] = max(unresolved.get(k, 0), v)
    ambiguous = {pre(n, k): v for n, g in gs for k, v in g.ambiguous.items()}
    # ponytail: cross-repo match is a unique bare name in the same language, INFERRED;
    # upgrade: qualified cross-repo imports if this misfires
    for n, g in gs:
        for raw in g.dangling:
            t = raw.target_text.split(".")[-1]
            ids = by_name.get((_lang_of(raw.file), t), [])
            if len(ids) == 1:
                edges.append(
                    Edge(
                        pre(n, raw.src),
                        ids[0],
                        raw.kind,
                        "INFERRED",
                        pre(n, raw.file),
                        raw.line,
                    )
                )
            elif len(ids) > 1:
                unresolved[t] = max(unresolved.get(t, 0), len(ids))
    stats = {
        "roots": len(roots),
        "files": sum(g.stats.get("files", 0) for _, g in gs),
        "cache_hits": sum(g.stats.get("cache_hits", 0) for _, g in gs),
    }
    return Graph(symbols, edges, unresolved, stats, ambiguous, [])


def refine_with_lsp(
    root: Path,
    edges: list[Edge],
    pairs: list[tuple[RawEdge, int | None]],
    by_file_line: dict[tuple[str, int], str],
) -> int:
    servers: dict[str, Lsp] = {}
    unavailable: set[str] = set()
    n = 0
    try:
        for raw, i in pairs:
            lang = _lang_of(raw.file)
            if lang not in SERVERS or lang in unavailable:
                continue
            if lang not in servers:
                try:
                    servers[lang] = Lsp(root, lang)
                except FileNotFoundError:
                    print(
                        f"lsp: {SERVERS[lang][0]} not found "
                        "(npm i -g pyright typescript-language-server typescript)",
                        file=sys.stderr,
                    )
                    unavailable.add(lang)
                    continue
            try:
                d = servers[lang].definition(raw.file, raw.line, raw.col)
            except (RuntimeError, OSError):
                # ponytail: a stalled or dead server stays unusable for the rest of
                # the run; upgrade: restart it once if transient stalls show up
                servers.pop(lang).close()
                unavailable.add(lang)
                continue
            if d and (dst := by_file_line.get(d)):
                edge = Edge(raw.src, dst, raw.kind, "EXTRACTED", raw.file, raw.line)
                n += 1
                if i is None:
                    edges.append(edge)
                else:
                    edges[i] = edge
    finally:
        for s in servers.values():
            s.close()
    return n
