import json
import select
import subprocess
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


class Lsp:
    def __init__(self, root: Path, lang: str):
        self.root, self.lang, self.n, self.opened = root, lang, 0, set()
        self.p = subprocess.Popen(
            SERVERS[lang],
            cwd=root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        self.call(
            "initialize",
            {"processId": None, "rootUri": root.as_uri(), "capabilities": {}},
        )
        self.notify("initialized", {})

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
                if msg.get("id") == self.n and "method" not in msg:
                    return msg.get("result")

    def definition(self, rel: str, line: int, col: int) -> tuple[str, int] | None:
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
            return str(path.relative_to(self.root)), rng["start"]["line"] + 1
        except ValueError:
            # ponytail: a definition outside the repo (site-packages) is dropped;
            # upgrade: return an external marker if third-party jumps matter
            return None

    def close(self) -> None:
        self.p.kill()
        self.p.wait()
