import argparse
import sys
from pathlib import Path

from wiremap import __version__
from wiremap.commands import (
    ask,
    cache_prune,
    callers,
    communities,
    deps,
    entrypoints,
    export_vault,
    graph,
    grep,
    hook_post_edit,
    install_hook,
    install_skill,
    pack,
    report,
    skeleton,
    status,
    summarize,
    summarize_write,
    triage,
)
from wiremap.discover import RepoError, toplevel
from wiremap.resolve import build, build_many

EXIT_OK, EXIT_NOT_FOUND, EXIT_ERROR = 0, 1, 2
_SINGLE_ROOT = (
    "skeleton",
    "deps",
    "grep",
    "pack",
    "cache",
    "summarize",
    "export",
    "triage",
    "status",
    "hook",
)


def _depth(v: str) -> int:
    n = int(v)
    if n < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return n


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="wiremap")
    p.add_argument(
        "--repo", action="append", help="path inside the git checkout"
    )
    p.add_argument(
        "--stats", action="store_true", help="print parse/cache/rule counts to stderr"
    )
    p.add_argument(
        "--lsp",
        action="store_true",
        help="resolve INFERRED/unresolved calls with a language server "
        "(pyright, typescript-language-server)",
    )
    p.add_argument("--version", action="version", version=f"wiremap {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sk = sub.add_parser("skeleton")
    sk.add_argument("paths", nargs="+", metavar="PATH")

    ca = sub.add_parser("callers")
    ca.add_argument("symbol", metavar="SYMBOL")
    ca.add_argument("--depth", type=_depth, default=1)
    ca.add_argument(
        "--min-confidence",
        type=str.lower,
        choices=("extracted", "inferred"),
        default="inferred",
    )

    dp = sub.add_parser("deps")
    dp.add_argument("target", metavar="TARGET")
    dp.add_argument("--depth", type=_depth, default=1)

    gr = sub.add_parser("grep")
    gr.add_argument("pattern", metavar="PATTERN")

    pk = sub.add_parser("pack")
    pk.add_argument("--files", nargs="+", required=True, metavar="PATH")
    pk.add_argument("--task", metavar="TEXT")

    gp = sub.add_parser("graph")
    gp.add_argument("symbol", nargs="?", metavar="SYMBOL")
    gp.add_argument("--files", nargs="+", metavar="PATH")
    gp.add_argument(
        "--format", choices=("mermaid", "dot", "graphml", "cypher"), default="mermaid"
    )
    gp.add_argument("--html", metavar="PATH")

    ex = sub.add_parser("export")
    ex.add_argument("--obsidian", required=True, metavar="DIR")

    sm = sub.add_parser("summarize")
    smx = sm.add_mutually_exclusive_group(required=True)
    smx.add_argument("--files", nargs="+", metavar="PATH")
    smx.add_argument("--write", metavar="PATH")

    sub.add_parser("communities")
    sub.add_parser("report")
    sub.add_parser("status")

    tr = sub.add_parser("triage")
    tr.add_argument("--base", default="main")

    ak = sub.add_parser("ask")
    ak.add_argument("text", metavar="TEXT")
    sub.add_parser("entrypoints")
    sub.add_parser("install-skill")
    sub.add_parser("install-hook")

    hk = sub.add_parser("hook")
    hks = hk.add_subparsers(dest="hook_cmd", required=True)
    hks.add_parser("post-edit")

    cache = sub.add_parser("cache")
    cache_sub = cache.add_subparsers(dest="cache_cmd", required=True)
    prune = cache_sub.add_parser("prune")
    prune.add_argument("--days", type=int, default=30)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "install-skill":
        print(install_skill())
        return EXIT_OK
    if args.cmd == "install-hook":
        print(install_hook())
        return EXIT_OK
    if args.cmd == "hook":
        # ponytail: an advisory channel — any failure is silence, never a non-zero
        # exit that would surface in the editor; upgrade: none wanted
        try:
            payload = sys.stdin.read()
            root = toplevel(Path((args.repo or ["."])[0]))
            text = hook_post_edit(build(root, args.lsp), root, payload)
            if text:
                print(text)
        except Exception:  # noqa: BLE001
            pass
        return EXIT_OK

    try:
        roots = [toplevel(Path(r)) for r in args.repo or ["."]]
    except RepoError as exc:
        print(exc, file=sys.stderr)
        return EXIT_ERROR
    root = roots[0]

    if len(roots) > 1 and args.cmd in _SINGLE_ROOT:
        print(
            "workspace: single-root command, using " + str(root), file=sys.stderr
        )

    if args.cmd == "status":
        print(status(root))
        return EXIT_OK

    if args.cmd == "summarize" and args.write:
        try:
            text, code = summarize_write(root, args.write, sys.stdin.read())
        except RepoError as exc:
            print(exc, file=sys.stderr)
            return EXIT_ERROR
        print(text)
        return code

    if args.cmd == "cache":
        print(cache_prune(root, args.days))
        return EXIT_OK

    try:
        g = (
            build(root, args.lsp)
            if args.cmd in _SINGLE_ROOT
            else build_many(roots, args.lsp)
        )
    except RepoError as exc:
        print(exc, file=sys.stderr)
        return EXIT_ERROR

    if args.cmd == "skeleton":
        text, code = skeleton(g, root, args.paths)
    elif args.cmd == "callers":
        text, code = callers(g, args.symbol, args.depth, args.min_confidence.upper())
    elif args.cmd == "deps":
        text, code = deps(g, root, args.target, args.depth)
    elif args.cmd == "grep":
        text, code = grep(g, root, args.pattern)
    elif args.cmd == "pack":
        text, code = pack(g, root, args.files, args.task)
    elif args.cmd == "export":
        try:
            text, code = export_vault(g, root, Path(args.obsidian)), EXIT_OK
        except RepoError as exc:
            print(exc, file=sys.stderr)
            return EXIT_ERROR
    elif args.cmd == "summarize":
        try:
            text, code = summarize(g, root, args.files)
        except RepoError as exc:
            print(exc, file=sys.stderr)
            return EXIT_ERROR
    elif args.cmd == "communities":
        text, code = communities(g), EXIT_OK
    elif args.cmd == "report":
        text, code = report(g, root), EXIT_OK
    elif args.cmd == "triage":
        text, code = triage(g, root, args.base)
    elif args.cmd == "ask":
        text, code = ask(g, args.text), EXIT_OK
    elif args.cmd == "graph":
        text, code = graph(
            g,
            root,
            args.files,
            args.symbol,
            args.format,
            Path(args.html) if args.html else None,
        )
    else:
        text, code = entrypoints(g), EXIT_OK

    print(text)
    if args.stats:
        rows = "\n".join(f"{k}={v}" for k, v in sorted(g.stats.items()))
        print(rows, file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
