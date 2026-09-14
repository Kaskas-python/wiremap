import sys
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path

from wiremap.discover import EXTS, RepoError, files
from wiremap.parse import (
    LANG_SPECS,
    TEXT_LANGS,
    RawEdge,
    Symbol,
    language,
    load_or_parse,
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


def _lang_of(file: str) -> str:
    return EXTS[Path(file).suffix]


def index(
    symbols: list[Symbol],
) -> tuple[dict[str, Symbol], dict[tuple[str, str], list[str]]]:
    by_id = {s.id: s for s in symbols}
    by_name: dict[tuple[str, str], list[str]] = {}
    for s in symbols:
        # ponytail: a table defined in both schema.sql and __tablename__ shares one
        # id; last file wins in by_id; upgrade: key tables by file if both must survive
        key = ("sql", s.name) if s.kind == "table" else (_lang_of(s.file), s.name)
        if s.id not in by_name.setdefault(key, []):
            by_name[key].append(s.id)
    return by_id, by_name


def _resolve(
    raw: RawEdge,
    module: str,
    imports: dict[str, str],
    by_id: dict[str, Symbol],
    by_name: dict[tuple[str, str], list[str]],
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
        # ponytail: scans every name for each doc mention (names x mentions);
        # upgrade: a bare-name index keyed on name alone if docs get large
        ids = [i for (_, n), v in by_name.items() if n == t for i in v]
        # ponytail: prose naming an ambiguous symbol is not a missing edge, so it
        # never writes unresolved; upgrade: report doc ambiguity separately if wanted
        return (ids[0], "INFERRED") if len(ids) == 1 else None
    ids = by_name.get((lang, t), [])
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


def build(root: Path) -> Graph:
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

    by_id, by_name = index(syms)
    edges, unresolved, labels, ambiguous = [], {}, {}, {}
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
                unresolved,
            )
            dst, conf = r if r else (None, None)
            if r is None and raw.kind != "mentions":
                t = raw.target_text.split(".")[-1]
                if len(by_name.get((_lang_of(raw.file), t), [])) > 1:
                    ambiguous.setdefault(raw.src, []).append(t)
        if dst:
            edges.append(Edge(src, dst, raw.kind, conf, raw.file, raw.line))

    rule_counts = Counter(f"rule_{r.kind}" for r in raws if r.kind in _FRAMEWORK_KINDS)
    if missing:
        names = ",".join(sorted(missing))
        print(
            f"no grammar for {', '.join(sorted(missing))}: "
            f"uv tool install 'wiremap[{names}]'",
            file=sys.stderr,
        )
    return Graph(
        by_id,
        edges,
        unresolved,
        {
            "files": n_files,
            "skipped_no_grammar": skipped,
            "cache_hits": hits,
            "failed": failed,
            **rule_counts,
        },
        ambiguous,
    )
