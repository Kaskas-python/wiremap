import subprocess
from pathlib import Path


class RepoError(Exception): ...


def _git(root, *a) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *a],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    ).stdout.strip()


def toplevel(path: Path) -> Path:
    try:
        return Path(_git(path, "rev-parse", "--show-toplevel"))
    except subprocess.CalledProcessError:
        raise RepoError(f"not a git repository: {path}")


def head_stamp(root: Path) -> str:
    try:
        sha = _git(root, "rev-parse", "--short", "HEAD")
    except subprocess.CalledProcessError:
        return "no-commit"
    branch = _git(root, "branch", "--show-current")
    rows = _git(
        root, "--no-optional-locks", "status", "--porcelain", "--untracked-files=all"
    ).splitlines()
    unt = sum(r.startswith("??") for r in rows)
    mod = len(rows) - unt
    drift = (f" +{mod} modified" if mod else "") + (
        f" +{unt} untracked" if unt else ""
    )
    return f"{sha} {branch or '(detached)'}{drift}"


EXTS = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".sql": "sql",
    ".md": "markdown",
}
MAX_BYTES = 1_000_000


def _listed(root: Path) -> list[str]:
    listed = _git(root, "ls-files", "-z").split("\0") + _git(
        root, "ls-files", "-z", "--others", "--exclude-standard"
    ).split("\0")
    return sorted(set(filter(None, listed)))


def oversized(root: Path) -> list[str]:
    out = []
    for rel in _listed(root):
        p = root / rel
        if EXTS.get(p.suffix) and p.is_file() and p.stat().st_size > MAX_BYTES:
            out.append(rel)
    return out


def files(root: Path) -> list[tuple[Path, str]]:
    out = []
    for rel in _listed(root):
        p = root / rel
        lang = EXTS.get(p.suffix)
        if lang and p.is_file() and p.stat().st_size <= MAX_BYTES:
            out.append((p, lang))
    return out
