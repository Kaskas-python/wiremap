import importlib.util
import json
import os
import select
import shutil
import signal
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

LSP_TIMEOUT = 10

SERVERS = {
    "python": ["pyright-langserver", "--stdio"],
    "typescript": ["typescript-language-server", "--stdio"],
    "tsx": ["typescript-language-server", "--stdio"],
}
LANGUAGE_ID = {
    "python": "python",
    "typescript": "typescript",
    "tsx": "typescriptreact",
}
TS_PINS = ["typescript@5.9.3", "typescript-language-server@6.0.0"]


def _node_dir() -> Path | None:
    spec = importlib.util.find_spec("nodejs_wheel")
    return Path(spec.origin).parent if spec and spec.origin else None


def _path_with_node() -> str:
    # ponytail: the TypeScript server's launcher is `#!/usr/bin/env node`; the bundled
    # node must be on the child's PATH on a machine without a system Node
    d = _node_dir()
    return os.pathsep.join(
        filter(None, ([str(d / "bin")] if d else []) + [os.environ.get("PATH", "")])
    )


def _bin(name: str) -> str | None:
    # ponytail: the wiremap[lsp] extra puts pyright-langserver next to the interpreter,
    # which `uv tool` never exposes on PATH; upgrade: none needed
    venv_bin = str(Path(sys.executable).parent)
    return shutil.which(
        name, path=os.pathsep.join(filter(None, [venv_bin, os.environ.get("PATH", "")]))
    )


def _ts_server() -> str | None:
    if exe := _bin("typescript-language-server"):
        return exe
    d = _node_dir()
    if d is None:
        return None
    node_dir = Path.home() / ".cache" / "wiremap" / "node"
    exe = node_dir / "node_modules" / ".bin" / "typescript-language-server"
    if not exe.exists():
        # ponytail: one-time npm install into wiremap's cache (network once); the wheel's
        # bin/npm launcher is a misplaced hard link, so npm-cli.js runs under the bundled node
        node_dir.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(
                [
                    str(d / "bin" / "node"),
                    str(d / "lib/node_modules/npm/bin/npm-cli.js"),
                    "install",
                    "--silent",
                    "--no-audit",
                    "--no-fund",
                    "--prefix",
                    str(node_dir),
                    *TS_PINS,
                ],
                check=True,
                capture_output=True,
                timeout=180,
                env={**os.environ, "PATH": _path_with_node()},
            )
        except subprocess.SubprocessError as exc:
            raise RuntimeError(
                f"npm install failed (exit {getattr(exc, 'returncode', 'timeout')})"
            ) from exc
    return str(exe)


def _command(lang: str) -> list[str]:
    name, *flags = SERVERS[lang]
    exe = _ts_server() if lang != "python" else _bin(name)
    if not exe:
        raise FileNotFoundError(name)
    return [exe, *flags]


class Lsp:
    def __init__(self, root: Path, lang: str):
        self.root, self.lang, self.n, self.opened = root, lang, 0, set()
        self._closed = False
        self.p = subprocess.Popen(
            _command(lang),
            cwd=root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
            env={**os.environ, "PATH": _path_with_node()},
            start_new_session=True,
        )
        try:
            self.call(
                "initialize",
                {
                    "processId": None,
                    "rootUri": root.as_uri(),
                    "workspaceFolders": [
                        {"uri": root.as_uri(), "name": root.name}
                    ],
                    "capabilities": {},
                },
            )
            self.notify("initialized", {})
        except BaseException:
            self.close()
            raise

    def _wait(self) -> None:
        # ponytail: select is only authoritative because stdout is unbuffered
        # (bufsize=0); upgrade: a reader thread if non-blocking reads are needed
        if not select.select([self.p.stdout], [], [], LSP_TIMEOUT)[0]:
            raise RuntimeError("lsp timeout")

    def _readline(self) -> bytes:
        out = b""
        while not out.endswith(b"\n"):
            self._wait()
            c = self.p.stdout.read(1)
            if not c:
                return out
            out += c
        return out

    def _readn(self, n: int) -> bytes:
        out = b""
        while len(out) < n:
            self._wait()
            chunk = self.p.stdout.read(n - len(out))
            if not chunk:
                return out
            out += chunk
        return out

    def _send(self, msg: dict) -> None:
        b = json.dumps(msg).encode()
        self.p.stdin.write(f"Content-Length: {len(b)}\r\n\r\n".encode() + b)
        self.p.stdin.flush()

    def notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def call(self, method: str, params: dict):
        self.n += 1
        self._send(
            {"jsonrpc": "2.0", "id": self.n, "method": method, "params": params}
        )
        # ponytail: server-initiated requests (window/workDoneProgress/create,
        # client/registerCapability) are read and ignored, never answered;
        # upgrade: reply null to them if a server stalls waiting
        while True:
            hdr = self._readline()
            if not hdr:
                raise RuntimeError("lsp exited")
            if hdr.lower().startswith(b"content-length:"):
                n = int(hdr.split(b":")[1])
                while self._readline() not in (b"\r\n", b"\n", b""):
                    pass
                msg = json.loads(self._readn(n))
                if msg.get("id") == self.n and "error" in msg:
                    raise RuntimeError(msg["error"].get("message", "lsp error"))
                if msg.get("id") == self.n and "method" not in msg:
                    return msg.get("result")

    def definition(
        self, rel: str, line: int, col: int, hop: bool = False
    ) -> tuple[str, int] | None:
        uri = (self.root / rel).as_uri()
        if rel not in self.opened:
            self.opened.add(rel)
            self.notify(
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "uri": uri,
                        "languageId": LANGUAGE_ID[self.lang],
                        "version": 1,
                        "text": (self.root / rel).read_text(errors="replace"),
                    }
                },
            )
        r = self.call(
            "textDocument/definition",
            {
                "textDocument": {"uri": uri},
                "position": {"line": line - 1, "character": col},
            },
        )
        loc = self._loc(r)
        if loc is None:
            return None
        f, ln, ch = loc
        if not hop and self.lang != "python" and f == rel and ln != line:
            # ponytail: tsserver answers with the in-file import binding while its project is
            # still loading; retry the hop through the binding for a few seconds, then give up
            for _ in range(3):
                time.sleep(1)
                if (d := self.definition(rel, ln, ch, hop=True)) and d[0] != rel:
                    return d
            return f, ln
        return f, ln

    def _loc(self, r) -> tuple[str, int, int] | None:
        if not r:
            return None
        loc = r[0] if isinstance(r, list) else r
        target = loc.get("uri") or loc["targetUri"]
        path = Path(urllib.parse.unquote(urllib.parse.urlparse(target).path))
        rng = (
            loc.get("range")
            or loc.get("targetSelectionRange")
            or loc["targetRange"]
        )
        try:
            return (
                str(path.relative_to(self.root)),
                rng["start"]["line"] + 1,
                rng["start"]["character"],
            )
        except ValueError:
            # ponytail: a definition outside the repo (site-packages) is dropped
            return None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            os.killpg(self.p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.p.wait()
