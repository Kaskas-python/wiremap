import argparse
import sys

EXIT_OK, EXIT_NOT_FOUND, EXIT_ERROR = 0, 1, 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="wiremap")
    p.add_argument("--repo", default=".", help="path inside the git checkout")
    p.add_argument(
        "--stats", action="store_true", help="print parse/cache/rule counts to stderr"
    )
    # ponytail: subcommands registered in T9
    p.add_subparsers(dest="cmd", required=True)
    return p


def main(argv=None) -> int:
    build_parser().parse_args(argv)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
