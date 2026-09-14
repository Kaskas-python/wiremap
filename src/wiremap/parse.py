import functools
import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

import tree_sitter_python
import tree_sitter_typescript
from tree_sitter import Language, Node, Parser, Query, QueryCursor

from wiremap.frameworks import RULES

_LANGS = {
    "python": Language(tree_sitter_python.language()),
    "typescript": Language(tree_sitter_typescript.language_typescript()),
    "tsx": Language(tree_sitter_typescript.language_tsx()),
}
_QUERY_FILE = {
    "python": "python.scm",
    "typescript": "typescript.scm",
    "tsx": "typescript.scm",
}


@dataclass(frozen=True)
class Symbol:
    id: str
    file: str
    kind: str
    name: str
    qualname: str
    line_start: int
    line_end: int
    signature: str


@dataclass(frozen=True)
class RawEdge:
    src: str
    kind: str
    target_text: str
    line: int
    file: str


@functools.cache
def _query(lang: str, text: str | None = None) -> Query:
    if text is None:
        text = (Path(__file__).parent / "queries" / _QUERY_FILE[lang]).read_text()
    return Query(_LANGS[lang], text)


def captures(lang: str, src: bytes, text: str | None = None) -> dict[str, list[Node]]:
    tree = Parser(_LANGS[lang]).parse(src)
    return QueryCursor(_query(lang, text)).captures(tree.root_node)


_CLASS_TYPES = ("class_definition", "class_declaration")
_QUOTES = "\"'"


def matches(lang: str, src: bytes, text: str | None = None) -> list[dict[str, Node]]:
    tree = Parser(_LANGS[lang]).parse(src)
    return [
        {k: v[0] for k, v in m.items()}
        for _, m in QueryCursor(_query(lang, text)).matches(tree.root_node)
    ]


def _enclosing(defs: list[tuple[Node, str]], node: Node) -> str | None:
    containing = [
        (d.start_byte, q)
        for d, q in defs
        if d is not node
        and d.start_byte <= node.start_byte
        and node.end_byte <= d.end_byte
    ]
    return max(containing)[1] if containing else None


def parse_file(path: Path, root: Path, lang: str) -> tuple[list[Symbol], list[RawEdge]]:
    src = path.read_bytes()
    rel = str(path.relative_to(root))
    module = rel.rsplit(".", 1)[0].replace("/", ".")
    ms = matches(lang, src)

    defs: list[tuple[Node, str]] = []
    kinds: list[str] = []
    class_quals: set[str] = set()
    for m in sorted(
        (m for m in ms if "def.node" in m), key=lambda m: m["def.node"].start_byte
    ):
        n = m["def.node"]
        parent = _enclosing(defs, n)
        name = m["def.name"].text.decode()
        qualname = f"{parent}.{name}" if parent else name
        if n.type in _CLASS_TYPES:
            kinds.append("class")
            class_quals.add(qualname)
        elif n.type == "method_definition" or parent in class_quals:
            kinds.append("method")
        else:
            kinds.append("function")
        defs.append((n, qualname))

    symbols = [
        Symbol(
            id=f"{module}.{q}",
            file=rel,
            kind=kind,
            name=q.rsplit(".", 1)[-1],
            qualname=q,
            line_start=n.start_point[0] + 1,
            line_end=n.end_point[0] + 1,
            signature=n.text.decode().splitlines()[0],
        )
        for (n, q), kind in zip(defs, kinds)
    ]
    edges = [
        RawEdge(
            src=f"{module}.{q}" if (q := _enclosing(defs, m["call.node"])) else module,
            kind="calls",
            target_text=m["call.callee"].text.decode(),
            line=m["call.node"].start_point[0] + 1,
            file=rel,
        )
        for m in ms
        if "call.node" in m and m["call.node"].parent.type != "decorator"
    ]
    for m in ms:
        if "import.module" not in m:
            continue
        mod = m["import.module"].text.decode().strip(_QUOTES)
        name = m["import.name"].text.decode() if "import.name" in m else None
        edges.append(
            RawEdge(
                src=module,
                kind="imports",
                target_text=f"{mod}.{name}" if name else mod,
                line=m["import.module"].start_point[0] + 1,
                file=rel,
            )
        )

    def text(n: Node) -> str:
        return n.text.decode().strip(_QUOTES)

    def qualname_of(n: Node) -> str:
        span = (n.start_byte, n.end_byte)
        return next(q for d, q in defs if (d.start_byte, d.end_byte) == span)

    for r in (r for r in RULES if r.language == lang):
        for m in matches(lang, src, r.query):
            anchor = m.get("self") or m.get("target") or m.get("label") or m.get("a")
            q = (
                qualname_of(m["self"])
                if r.target == "self"
                else _enclosing(defs, anchor)
            )
            src_id = f"{module}.{q}" if q else module
            if r.target == "self":
                target_text = f"self:{module}.{q}"
            elif r.target.startswith("label:"):
                target_text = (
                    f"label:{text(m['a'])}->{text(m.get('b') or m.get('target'))}"
                )
            elif r.edge_kind == "graph_node":
                target_text = f"node:{text(m['label'])}={text(m['target'])}"
            else:
                target_text = text(m[r.target])
            edges.append(
                RawEdge(
                    src=src_id,
                    kind=r.edge_kind,
                    target_text=target_text,
                    line=anchor.start_point[0] + 1,
                    file=rel,
                )
            )
    return symbols, edges


SCHEMA = 1


def cache_dir(root: Path) -> Path:
    return (
        Path.home()
        / ".cache"
        / "wiremap"
        / hashlib.sha1(str(root).encode()).hexdigest()
    )


def load_or_parse(
    path: Path, root: Path, lang: str
) -> tuple[list[Symbol], list[RawEdge], bool]:
    src = path.read_bytes()
    entry = cache_dir(root) / f"{hashlib.sha1(src).hexdigest()}.json"
    if entry.exists():
        try:
            d = json.loads(entry.read_text())
        except ValueError:
            d = {}
        if d.get("schema") == SCHEMA:
            return (
                [Symbol(**s) for s in d["symbols"]],
                [RawEdge(**e) for e in d["edges"]],
                True,
            )
    symbols, edges = parse_file(path, root, lang)
    entry.parent.mkdir(parents=True, exist_ok=True)
    tmp = entry.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(
            {
                "schema": SCHEMA,
                "symbols": [asdict(s) for s in symbols],
                "edges": [asdict(e) for e in edges],
            }
        )
    )
    tmp.replace(entry)
    return symbols, edges, False
