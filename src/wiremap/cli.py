import argparse
import sys
from pathlib import Path

from wiremap.commands import (
    cache_prune,
    callers,
    deps,
    entrypoints,
    grep,
    install_skill,
    pack,
    skeleton,
)
from wiremap.discover import RepoError, toplevel
from wiremap.resolve import build

EXIT_OK, EXIT_NOT_FOUND, EXIT_ERROR = 0, 1, 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="wiremap")
    p.add_argument("--repo", default=".", help="path inside the git checkout")
    p.add_argument(
        "--stats", action="store_true", help="print parse/cache/rule counts to stderr"
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sk = sub.add_parser("skeleton")
    sk.add_argument("paths", nargs="+", metavar="PATH")

    ca = sub.add_parser("callers")
    ca.add_argument("symbol", metavar="SYMBOL")
    ca.add_argument("--depth", type=int, default=1)
    ca.add_argument(
        "--min-confidence", choices=("extracted", "inferred"), default="inferred"
    )

    dp = sub.add_parser("deps")
    dp.add_argument("target", metavar="TARGET")
    dp.add_argument("--depth", type=int, default=1)

    gr = sub.add_parser("grep")
    gr.add_argument("pattern", metavar="PATTERN")

    pk = sub.add_parser("pack")
    pk.add_argument("--files", nargs="+", required=True, metavar="PATH")
    pk.add_argument("--task", metavar="TEXT")

    sub.add_parser("entrypoints")
    sub.add_parser("install-skill")

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

    try:
        root = toplevel(Path(args.repo))
    except RepoError as exc:
        print(exc, file=sys.stderr)
        return EXIT_ERROR

    if args.cmd == "cache":
        print(cache_prune(root, args.days))
        return EXIT_OK

    try:
        g = build(root)
    except RepoError as exc:
        print(exc, file=sys.stderr)
        return EXIT_ERROR

    if args.cmd == "skeleton":
        text, code = skeleton(g, args.paths), EXIT_OK
    elif args.cmd == "callers":
        text, code = callers(g, args.symbol, args.depth, args.min_confidence.upper())
    elif args.cmd == "deps":
        text, code = deps(g, root, args.target, args.depth)
    elif args.cmd == "grep":
        text, code = grep(g, root, args.pattern)
    elif args.cmd == "pack":
        text, code = pack(g, root, args.files, args.task), EXIT_OK
    else:
        text, code = entrypoints(g), EXIT_OK

    print(text)
    if args.stats:
        rows = "\n".join(f"{k}={v}" for k, v in sorted(g.stats.items()))
        print(rows, file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
