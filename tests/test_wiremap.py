from wiremap.cli import main


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


def test_pack_fits_15_lines(repo, capsys):
    out, _, _ = run(
        capsys, "--repo", str(repo), "pack", "--files", "app/api.py", "app/graph.py"
    )
    lines = out.rstrip().splitlines()
    assert len(lines) <= 15 and lines[0].startswith("HEAD ") and "STOP" in lines[-1]


def test_cache_hit_skips_parse(repo, capsys):
    run(capsys, "--repo", str(repo), "--stats", "entrypoints")
    _, err, _ = run(capsys, "--repo", str(repo), "--stats", "entrypoints")
    assert "cache_hits=11" in err


def test_language_dropin(repo, capsys):
    out, _, _ = run(capsys, "--repo", str(repo), "callers", "svc.main.helper")
    assert "svc.main.main  calls  EXTRACTED" in out
    out, _, _ = run(capsys, "--repo", str(repo), "callers", "lib.core.helper")
    assert "lib.core.run  calls  EXTRACTED" in out


def test_cross_artifact_edges(repo, capsys):
    out, _, _ = run(capsys, "--repo", str(repo), "deps", "app.db.Order")
    assert "sql:orders  table_ref  EXTRACTED" in out
    out, _, _ = run(capsys, "--repo", str(repo), "callers", "app.api.list_orders")
    assert "docs.arch  mentions  INFERRED" in out
    out, _, _ = run(capsys, "--repo", str(repo), "callers", "sql:orders")
    assert "app.api.list_orders  table_ref  INFERRED" in out
