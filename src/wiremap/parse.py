import bisect
import dataclasses
import functools
import hashlib
import importlib
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from tree_sitter import Language, Node, Parser, Query, QueryCursor

from wiremap.frameworks import RULES

LANG_SPECS = {
    "python": ("tree_sitter_python", "language"),
    "typescript": ("tree_sitter_typescript", "language_typescript"),
    "tsx": ("tree_sitter_typescript", "language_tsx"),
    "go": ("tree_sitter_go", "language"),
    "rust": ("tree_sitter_rust", "language"),
}
_QUERY_FILE = {
    "python": "python.scm",
    "typescript": "typescript.scm",
    "tsx": "typescript.scm",
    "go": "go.scm",
    "rust": "rust.scm",
}


@functools.cache
def language(lang: str) -> Language | None:
    mod, fn = LANG_SPECS[lang]
    try:
        return Language(getattr(importlib.import_module(mod), fn)())
    except ImportError:
        return None


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
    col: int = 0


@functools.cache
def _query(lang: str, text: str | None = None) -> Query:
    if text is None:
        text = (Path(__file__).parent / "queries" / _QUERY_FILE[lang]).read_text()
    return Query(language(lang), text)


_CLASS_TYPES = (
    "class_definition",
    "class_declaration",
    "type_spec",
    "struct_item",
    "enum_item",
    "trait_item",
)
_QUOTES = "\"'"

_SQL_DEF = re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`\"]?(\w+)", re.I)
_SQL_REF = re.compile(r"\b(?:FROM|JOIN|INTO|UPDATE)\s+[`\"]?(\w+)", re.I)
# ponytail: requires an opening quote earlier on the line, so a plain
# `from x import y` never matches; prose false positives resolve to no edge;
# upgrade: match the string-literal node from the Python grammar if false
# positives matter
_SQL_IN_STR = re.compile(
    r"""["'][^"'\n]*?\b(?:FROM|JOIN|INTO|UPDATE)\s+(\w+)""", re.I
)
_MD_REF = re.compile(r"`([A-Za-z_][\w.]*)`")
_TABLENAME = re.compile(r"__tablename__\s*=\s*[\"'](\w+)[\"']")
TEXT_LANGS = {"sql", "markdown"}


def module_of(rel: str) -> str:
    # ponytail: strips a leading src/ (pypa src layout);
    # upgrade: read [tool.setuptools]/[tool.hatch] source roots
    return rel.removeprefix("src/").rsplit(".", 1)[0].replace("/", ".")


def _table(name: str, rel: str, i: int, sig: str) -> Symbol:
    n = name.lower()
    return Symbol(
        id=f"sql:{n}",
        file=rel,
        kind="table",
        name=n,
        qualname=n,
        line_start=i,
        line_end=i,
        signature=sig,
    )


def parse_text_file(
    path: Path, root: Path, lang: str
) -> tuple[list[Symbol], list[RawEdge]]:
    rel = str(path.relative_to(root))
    module = module_of(rel)
    symbols: list[Symbol] = []
    edges: list[RawEdge] = []
    for i, l in enumerate(path.read_text(errors="replace").splitlines(), 1):
        if lang == "sql":
            symbols += [_table(t, rel, i, l.strip()) for t in _SQL_DEF.findall(l)]
            edges += [
                RawEdge(module, "table_ref", f"sql:{t.lower()}", i, rel)
                for t in _SQL_REF.findall(l)
            ]
        else:
            edges += [
                RawEdge(module, "mentions", t, i, rel) for t in _MD_REF.findall(l)
            ]
    return symbols, edges


def matches(lang: str, node: Node, text: str | None = None) -> list[dict[str, Node]]:
    return [
        {k: v[0] for k, v in m.items()}
        for _, m in QueryCursor(_query(lang, text)).matches(node)
    ]


def _enclosing(
    defs: list[tuple[Node, str]], spans: tuple[list[int], list[int]], node: Node
) -> str | None:
    # ponytail: defs are appended in start order and nest properly, so the nearest
    # preceding def that contains the node is the innermost; maxends[i] is the
    # furthest end among defs[0..i], so once it falls short of the node nothing
    # earlier can contain it and the walk stops; upgrade: none
    starts, maxends = spans
    i = bisect.bisect_right(starts, node.start_byte) - 1
    while i >= 0 and maxends[i] >= node.end_byte:
        d, q = defs[i]
        if d is not node and d.end_byte >= node.end_byte:
            return q
        i -= 1
    return None


def parse_file(path: Path, root: Path, lang: str) -> tuple[list[Symbol], list[RawEdge]]:
    src = path.read_bytes()
    rel = str(path.relative_to(root))
    module = module_of(rel)
    root_node = Parser(language(lang)).parse(src).root_node
    if root_node.has_error:
        print(f"warning: {rel}: syntax errors, partial parse", file=sys.stderr)
    ms = matches(lang, root_node)

    defs: list[tuple[Node, str]] = []
    starts: list[int] = []
    maxends: list[int] = []
    spans = (starts, maxends)
    kinds: list[str] = []
    class_quals: set[str] = set()
    for m in sorted(
        (m for m in ms if "def.node" in m), key=lambda m: m["def.node"].start_byte
    ):
        n = m["def.node"]
        parent = _enclosing(defs, spans, n)
        name = m["def.name"].text.decode()
        qualname = f"{parent}.{name}" if parent else name
        if n.type in _CLASS_TYPES:
            kinds.append("class")
            class_quals.add(qualname)
        # ponytail: Go receivers and Rust impl blocks do not nest — qualname is the
        # bare name, and Go `type_spec` kinds every type declaration as a class;
        # nest via receiver/impl type if callers on methods matter.
        elif (
            n.type in ("method_definition", "method_declaration")
            or parent in class_quals
        ):
            kinds.append("method")
        else:
            kinds.append("function")
        defs.append((n, qualname))
        starts.append(n.start_byte)
        maxends.append(max(maxends[-1], n.end_byte) if maxends else n.end_byte)

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
    def utf16_col(n: Node) -> int:
        # ponytail: LSP positions are UTF-16 code units, tree-sitter gives bytes;
        # upgrade: none — this is the protocol
        pre = src[n.start_byte - n.start_point[1] : n.start_byte]
        return len(pre.decode("utf-8", "replace").encode("utf-16-le")) // 2

    edges = [
        RawEdge(
            src=(
                f"{module}.{q}"
                if (q := _enclosing(defs, spans, m["call.node"]))
                else module
            ),
            kind="calls",
            target_text=m["call.callee"].text.decode(),
            line=m["call.callee"].start_point[0] + 1,
            file=rel,
            col=utf16_col(m["call.callee"]),
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
        for m in matches(lang, root_node, r.query):
            anchor = m.get("self") or m.get("target") or m.get("label") or m.get("a")
            q = (
                qualname_of(m["self"])
                if r.target == "self"
                else _enclosing(defs, spans, anchor)
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

    if lang == "python":
        enclosing_defs = list(symbols)

        def owner(i: int) -> str:
            inner = [s for s in enclosing_defs if s.line_start <= i <= s.line_end]
            return max(inner, key=lambda s: s.line_start).id if inner else module

        for i, line in enumerate(src.decode(errors="replace").splitlines(), 1):
            for t in _TABLENAME.findall(line):
                symbols.append(_table(t, rel, i, line.strip()))
                edges.append(RawEdge(owner(i), "table_ref", f"sql:{t.lower()}", i, rel))
            for t in _SQL_IN_STR.findall(line):
                edges.append(
                    RawEdge(owner(i), "table_ref", f"sqlref:{t.lower()}", i, rel)
                )
    return symbols, edges


SCHEMA = hashlib.sha1(
    (
        "".join(r.query for r in RULES)
        + "".join(
            (Path(__file__).parent / "queries" / f).read_text()
            for f in sorted(set(_QUERY_FILE.values()))
        )
        + ",".join(f.name for f in dataclasses.fields(RawEdge))
        + "calls:callee-pos"
        + "src-root"
    ).encode()
).hexdigest()[:12]


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.stem}.{os.getpid()}.tmp")
    tmp.write_text(text)
    tmp.replace(path)


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
    rel = str(path.relative_to(root))
    key = hashlib.sha1(rel.encode() + b"\0" + src).hexdigest()
    entry = cache_dir(root) / f"{key}.json"
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
    symbols, edges = (parse_text_file if lang in TEXT_LANGS else parse_file)(
        path, root, lang
    )
    try:
        write_atomic(
            entry,
            json.dumps(
                {
                    "schema": SCHEMA,
                    "symbols": [asdict(s) for s in symbols],
                    "edges": [asdict(e) for e in edges],
                }
            ),
        )
    except OSError:
        # ponytail: cache is an accelerator; a failed write (race loser, read-only
        # ponytail: HOME, full disk) never changes the answer
        pass
    return symbols, edges, False
