import io
import json
import subprocess
import sys

import pytest

from wiremap.cli import main
from wiremap.commands import _fit
from wiremap.lsp import _bin


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return out.out, out.err, code


def test_framework_edges(repo, capsys):
    out, _, _ = run(capsys, "--repo", str(repo), "callers", "app.db.get_db")
    assert "app.api.list_orders  depends  EXTRACTED" in out
    out, _, _ = run(capsys, "--repo", str(repo), "callers", "app.graph.handle")
    assert "graph_edge  EXTRACTED" in out
    out, _, _ = run(capsys, "--repo", str(repo), "deps", "app.db.Order")
    assert "app.db.Customer  relationship" in out
    out, _, _ = run(capsys, "--repo", str(repo), "callers", "app.tasks.send_mail")
    assert "app.tasks.notify  calls  EXTRACTED" in out


def test_ambiguous_name_lists_candidates(repo, capsys):
    out, _, code = run(capsys, "--repo", str(repo), "callers", "get")
    assert code == 1 and "app.api.get" in out and "app.other.get" in out


def test_callers_none_is_explicit(repo, capsys):
    out, _, code = run(capsys, "--repo", str(repo), "callers", "app.unused.orphan")
    assert code == 0 and "no callers found" in out and "unresolved: 0" in out
    out, _, _ = run(capsys, "--repo", str(repo), "callers", "send_mail")
    assert "name hits without an edge: 1" in out and "app/dispatch.py:2" in out
    out, _, _ = run(capsys, "--repo", str(repo), "callers", "app.db.get_db"); assert "name hits without an edge: 0" in out


def test_pack_fits_15_lines(repo, capsys):
    out, _, _ = run(
        capsys, "--repo", str(repo), "pack", "--files", "app/db.py", "app/svc.py"
    )
    lines = out.rstrip().splitlines()
    assert len(lines) <= 15 and lines[0].startswith("HEAD ") and "STOP" in lines[-1]
    assert "caller: get_db <- app.api.list_orders" in out
    assert "def load( o: Order, ):" in out
    assert "… " not in out
    assert "graph_root" in run(capsys, "--repo", str(repo), "pack", "--files", "app/graph.py")[0]
    assert out.index("def load( o: Order, ):") > out.index("  def total(self):")
    assert _fit([list("abcdefgh"), list("ijklmnop"), []], 8) == [
        list("abc"), list("ijk"), [],
    ]


def test_cache_hit_skips_parse(repo, capsys):
    run(capsys, "--repo", str(repo), "--stats", "entrypoints")
    _, err, _ = run(capsys, "--repo", str(repo), "--stats", "entrypoints")
    assert "cache_hits=15" in err


def test_language_dropin(repo, capsys):
    out, _, _ = run(capsys, "--repo", str(repo), "callers", "svc.main.helper")
    assert "svc.main.main  calls  EXTRACTED" in out
    out, _, _ = run(capsys, "--repo", str(repo), "callers", "lib.core.helper")
    assert "lib.core.run  calls  EXTRACTED" in out
    out, _, _ = run(capsys, "--repo", str(repo), "callers", "pkg.a.f")
    assert "pkg.b.g  calls  EXTRACTED" in out


def test_cross_artifact_edges(repo, capsys):
    out, _, _ = run(capsys, "--repo", str(repo), "deps", "app.db.Order")
    assert "sql:orders  table_ref  EXTRACTED" in out
    out, _, _ = run(capsys, "--repo", str(repo), "callers", "app.api.list_orders")
    assert "docs.arch  mentions  INFERRED" in out
    out, _, _ = run(capsys, "--repo", str(repo), "callers", "sql:orders")
    assert "app.api.list_orders  table_ref  INFERRED" in out


def test_workspace_prefixes_ids(repo, capsys, tmp_path_factory):
    b = tmp_path_factory.mktemp("ws") / "b"
    b.mkdir()
    (b / "other.py").write_text("def caller():\n    list_orders()\n")
    subprocess.run(["git", "-C", str(b), "init", "-q"], check=True)
    out, _, _ = run(
        capsys, "--repo", str(repo), "--repo", str(b), "callers", "list_orders"
    )
    assert "b:other.caller  calls  INFERRED" in out
    out, _, _ = run(capsys, "--repo", str(repo), "--repo", str(b), "entrypoints")
    assert f"{repo.name}:app.api.list_orders" in out


@pytest.mark.skipif(
    _bin("pyright-langserver") is None, reason="pyright not installed"
)
def test_lsp_upgrades_confidence(repo, capsys):
    out, _, _ = run(capsys, "--repo", str(repo), "callers", "app.db.Order.total")
    assert "app.svc.load  calls  INFERRED" in out
    out, _, _ = run(
        capsys, "--repo", str(repo), "--lsp", "callers", "app.db.Order.total"
    )
    assert "app.svc.load  calls  EXTRACTED" in out


def test_summarize_roundtrip(repo, capsys, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("one\ntwo\n"))
    _, _, code = run(capsys, "--repo", str(repo), "summarize", "--write", "app/api.py")
    assert code == 0
    out, _, _ = run(capsys, "--repo", str(repo), "summarize", "--files", "app/api.py")
    assert "## app/api.py" in out and "one" in out
    (repo / "app/api.py").write_text((repo / "app/api.py").read_text() + "\n")
    out, _, _ = run(capsys, "--repo", str(repo), "summarize", "--files", "app/api.py")
    assert "no summary yet" in out


def test_communities_and_graph(repo, capsys, tmp_path_factory):
    out, _, _ = run(capsys, "--repo", str(repo), "communities")
    row = next(r for r in out.splitlines() if "app.graph.classify" in r)
    assert "app.graph.route_fn" in row
    out, _, _ = run(capsys, "--repo", str(repo), "graph", "--files", "app/graph.py")
    assert out.startswith("graph LR")
    out, _, _ = run(
        capsys, "--repo", str(repo), "graph", "--files", "app/graph.py",
        "--format", "dot",
    )
    assert out.startswith("digraph")
    html = tmp_path_factory.mktemp("html") / "out.html"
    run(capsys, "--repo", str(repo), "graph", "--html", str(html))
    t = html.read_text()
    assert "<canvas" in t and "app.graph.handle" in t
    out, _, _ = run(capsys, "--repo", str(repo), "report")
    assert "## Hubs" in out
    out, _, _ = run(capsys, "--repo", str(repo), "ask", "list orders")
    assert out.splitlines()[0].startswith("app.api.list_orders")
    vault = tmp_path_factory.mktemp("vault")
    run(capsys, "--repo", str(repo), "export", "--obsidian", str(vault))
    assert "## Called by" in (vault / "app/graph.py.md").read_text()


def test_triage_lists_changed_callers(repo, capsys):
    p = repo / "app/db.py"
    p.write_text(
        p.read_text().replace(
            "    yield None\n", "    yield None\n    return None\n"
        )
    )
    out, _, _ = run(capsys, "--repo", str(repo), "triage", "--base", "HEAD")
    assert "app/api.py" in out
    _, _, code = run(capsys, "--repo", str(repo), "triage", "--base", "nosuch")
    assert code == 2
    out, _, _ = run(capsys, "--repo", str(repo), "impact", "--base", "HEAD")
    assert "get_db <- app.api.list_orders" in out and "candidates" in out
    before = run(capsys, "--repo", str(repo), "triage", "--base", "HEAD")[0].split()[1]
    (repo / "app/new.py").write_text("def fresh():\n    return 1\n")
    after = run(capsys, "--repo", str(repo), "triage", "--base", "HEAD")[0].split()[1]
    assert int(after) > int(before)


def test_hook_post_edit_capped(repo, capsys, monkeypatch):
    payload = json.dumps({"tool_input": {"file_path": str(repo / "app/db.py")}})
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    out, _, code = run(capsys, "--repo", str(repo), "hook", "post-edit")
    assert code == 0
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "get_db <- app.api.list_orders" in ctx
    assert len(ctx.splitlines()) <= 22
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    out, _, code = run(capsys, "--repo", str(repo), "hook", "post-edit")
    assert code == 0 and out.strip() == ""
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    assert run(capsys, "--repo", str(repo), "hook")[2] == 0


def test_status_reads_stats_only(repo, capsys):
    out, _, _ = run(capsys, "--repo", str(repo), "status")
    assert out.strip() == ""
    run(capsys, "--repo", str(repo), "entrypoints")
    out, _, _ = run(capsys, "--repo", str(repo), "status")
    assert out.startswith("wiremap ")

