import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from amagi_env_diff import __version__, cli

REPO = Path(__file__).resolve().parents[1]
DEMO = REPO / "examples" / "nextjs-app"
SENTINEL = "zz-sentinel-value-must-not-leak"


def write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def run(capsys, *argv):
    rc = cli.main(list(argv))
    out, err = capsys.readouterr()
    return rc, out, err


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"amagi-env-diff {__version__}"


def test_default_files_clean(tmp_path, capsys):
    write(tmp_path, ".env.example", "A=\nB=\n")
    write(tmp_path, ".env", "A=1\nB=2\n")
    rc, out, _ = run(capsys, str(tmp_path))
    assert rc == 0
    assert "No differences across 2 keys" in out


def test_env_local_fallback_and_sample_example(tmp_path, capsys):
    write(tmp_path, ".env.sample", "A=\n")
    write(tmp_path, ".env.local", "A=1\nB=1\n")
    rc, out, _ = run(capsys, str(tmp_path))
    assert rc == 1
    assert "EXTRA IN .env.local" in out
    assert "Compared 2 sources: .env.sample (1 key), .env.local (2 keys)" in out


def test_demo_fixture_text_output(capsys):
    rc, out, _ = run(capsys, str(DEMO), "--live", str(DEMO / "vercel-env-ls.txt"))
    assert rc == 1
    assert out.splitlines()[0] == ("Compared 3 sources: .env.example (9 keys), "
                                   ".env.local (8 keys), vercel (7 keys, names only)")
    assert "MISSING IN vercel  listed in .env.example, not in vercel" in out
    assert "  STRIPE_WEBHOOK_SECRET" in out
    assert "  DATABASE_URL  lines 1, 9" in out
    assert out.rstrip().endswith(
        "7 problems (3 missing, 2 extra, 1 empty, 1 duplicate) across 11 keys. No values were printed.")


def test_demo_fixture_never_prints_local_values(capsys):
    local_text = (DEMO / ".env.local").read_text(encoding="utf-8")
    values = [line.split("=", 1)[1] for line in local_text.splitlines() if "=" in line]
    values = [v for v in values if len(v) > 1]
    for fmt in ("text", "json"):
        _, out, err = run(capsys, str(DEMO), "--live", str(DEMO / "vercel-env-ls.txt"),
                          "--matrix", "--format", fmt)
        for v in values:
            assert v not in out and v not in err


@pytest.mark.parametrize("live_text", [
    f"A={SENTINEL}\nB={SENTINEL}\nC=\n",
    json.dumps({"A": SENTINEL, "B": SENTINEL, "C": ""}),
    f"║ A │ {SENTINEL} ║\n║ B │ {SENTINEL} ║\n",
])
def test_no_value_leak_any_format(tmp_path, capsys, live_text):
    write(tmp_path, ".env.example", "A=\nC=\nD=\n")
    write(tmp_path, ".env", f"A={SENTINEL}\nC={SENTINEL}\nD=\nE={SENTINEL}\nbroken {SENTINEL}\n"
                            f"F=\"{SENTINEL}\n")
    live = write(tmp_path, "live.txt", live_text)
    for extra in ([], ["--matrix"], ["--format", "json"], ["--format", "json", "--matrix"]):
        rc, out, err = run(capsys, str(tmp_path), "--live", str(live), *extra)
        assert rc == 1
        assert SENTINEL not in out
        assert SENTINEL not in err


def test_json_output_shape(tmp_path, capsys):
    write(tmp_path, ".env.example", "A=\nB=\n")
    write(tmp_path, ".env", "A=1\n")
    rc, out, _ = run(capsys, str(tmp_path), "--format", "json", "--matrix")
    data = json.loads(out)
    assert rc == 1
    assert data["tool"] == "amagi-env-diff"
    assert data["reference"] == ".env.example"
    assert data["findings"] == [{"category": "missing", "source": ".env", "key": "B", "lines": []}]
    assert data["counts"]["missing"] == 1 and data["problems"] == 1
    assert data["matrix"]["B"] == {".env.example": "listed", ".env": "-"}
    assert [s["kind"] for s in data["sources"]] == ["example", "local"]


def test_matrix_text(tmp_path, capsys):
    write(tmp_path, ".env.example", "A=\n# OPT=\n")
    write(tmp_path, ".env", "A=\n")
    _, out, _ = run(capsys, str(tmp_path), "--matrix")
    assert "KEY  .env.example  .env" in out
    assert any(line.split() == ["A", "listed", "empty"] for line in out.splitlines())
    assert any(line.split() == ["OPT", "optional", "-"] for line in out.splitlines())


def test_live_from_stdin(tmp_path, capsys, monkeypatch):
    write(tmp_path, ".env.example", "A=\nB=\n")
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"A": "1"}'))
    rc, out, _ = run(capsys, str(tmp_path), "--no-env", "--live", "-", "--live-name", "production")
    assert rc == 1
    assert "MISSING IN production" in out
    assert "  B" in out


def test_live_format_and_target(tmp_path, capsys):
    write(tmp_path, ".env.example", "A=\nB=\n")
    live = write(tmp_path, "v.json", json.dumps({"envs": [
        {"key": "A", "target": ["production"]}, {"key": "B", "target": ["preview"]}]}))
    rc, out, _ = run(capsys, str(tmp_path), "--live", str(live), "--target", "production")
    assert rc == 1 and "MISSING IN vercel" in out
    rc, out, _ = run(capsys, str(tmp_path), "--live", str(live), "--target", "preview")
    assert "  A" in out


def test_fail_on_filters_exit_code(tmp_path, capsys):
    write(tmp_path, ".env.example", "A=\n")
    write(tmp_path, ".env", "A=\nEXTRA=1\n")
    assert run(capsys, str(tmp_path), "--fail-on", "missing")[0] == 0
    assert run(capsys, str(tmp_path), "--fail-on", "missing,extra")[0] == 1
    assert run(capsys, str(tmp_path), "--fail-on", "empty")[0] == 1
    assert run(capsys, str(tmp_path), "--fail-on", "none")[0] == 0


def test_fail_on_bad_value(tmp_path, capsys):
    write(tmp_path, ".env.example", "A=\n")
    write(tmp_path, ".env", "A=1\n")
    rc, _, err = run(capsys, str(tmp_path), "--fail-on", "nope")
    assert rc == 2 and "unknown value 'nope'" in err


def test_ignore_flag_and_file(tmp_path, capsys):
    write(tmp_path, ".env.example", "A=\n")
    write(tmp_path, ".env", "A=1\nDEBUG_SQL=1\nLOCAL_X=1\n")
    rc, out, _ = run(capsys, str(tmp_path), "--ignore", "DEBUG_*")
    assert rc == 1 and "LOCAL_X" in out and "DEBUG_SQL" not in out
    write(tmp_path, ".envdiffignore", "# local-only keys\nLOCAL_*\n")
    rc, out, _ = run(capsys, str(tmp_path), "--ignore", "DEBUG_*")
    assert rc == 0


def test_warnings_go_to_stderr(tmp_path, capsys):
    write(tmp_path, ".env.example", "A=\n")
    write(tmp_path, ".env", "A=1\nnot valid\n")
    rc, out, err = run(capsys, str(tmp_path))
    assert rc == 0
    assert "warning: .env: line 2: not a KEY=VALUE line, skipped" in err


def test_needs_two_sources(tmp_path, capsys):
    write(tmp_path, ".env", "A=1\n")
    rc, _, err = run(capsys, str(tmp_path))
    assert rc == 2 and "need at least two sources" in err
    assert ".. " not in err and str(tmp_path.resolve()) in err


def test_missing_paths_are_usage_errors(tmp_path, capsys):
    assert run(capsys, str(tmp_path / "nope"))[0] == 2
    write(tmp_path, ".env", "A=1\n")
    rc, _, err = run(capsys, str(tmp_path), "--live", str(tmp_path / "missing.txt"))
    assert rc == 2 and "live file not found" in err
    rc, _, err = run(capsys, str(tmp_path), "--example", "nope.example")
    assert rc == 2 and "example file not found" in err


def test_live_and_run_are_exclusive(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--live", "x", "--run", "vercel"])
    assert exc.value.code == 2


def test_live_name_collision(tmp_path, capsys):
    write(tmp_path, ".env.example", "A=\n")
    live = write(tmp_path, "pulled.env", "A=1\n")
    rc, out, _ = run(capsys, str(tmp_path), "--live", str(live), "--live-name", ".env.example")
    assert rc == 0 and ".env.example (live)" in out


# ---------- --run ----------

class FakeProc:
    def __init__(self, rc, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = rc, stdout, stderr


def fake_platform(monkeypatch, responses, on_path=("vercel", "railway", "fly")):
    calls = []

    def which(name):
        return f"/bin/{name}" if name in on_path else None

    def run(argv, **kwargs):
        calls.append(argv)
        return responses.pop(0)

    monkeypatch.setattr(cli.shutil, "which", which)
    monkeypatch.setattr(cli.subprocess, "run", run)
    return calls


def test_run_vercel_uses_production_json(tmp_path, capsys, monkeypatch):
    write(tmp_path, ".env.example", "A=\nB=\n")
    calls = fake_platform(monkeypatch, [FakeProc(0, json.dumps({"envs": [{"key": "A", "target": ["production"]}]}))])
    rc, out, _ = run(capsys, str(tmp_path), "--run", "vercel", "--run-args", "--scope myteam")
    assert calls == [["/bin/vercel", "env", "ls", "production", "--format", "json", "--scope", "myteam"]]
    assert rc == 1 and "MISSING IN vercel" in out


def test_run_vercel_falls_back_to_table(tmp_path, capsys, monkeypatch):
    write(tmp_path, ".env.example", "A=\n")
    table = " name   value      environments   created\n A      Encrypted  Preview        1d ago\n"
    calls = fake_platform(monkeypatch, [FakeProc(1, stderr="unknown option --format"), FakeProc(0, table)])
    rc, out, _ = run(capsys, str(tmp_path), "--run", "vercel", "--target", "preview")
    assert calls[1] == ["/bin/vercel", "env", "ls", "preview"]
    assert rc == 0


def test_run_railway_falls_back_to_old_command(tmp_path, capsys, monkeypatch):
    write(tmp_path, ".env.example", "A=\n")
    calls = fake_platform(monkeypatch, [FakeProc(2, stderr="unrecognized subcommand"),
                                        FakeProc(0, '{"A": "1", "RAILWAY_PROJECT_ID": "x"}')])
    rc, out, _ = run(capsys, str(tmp_path), "--run", "railway")
    assert [c[1:] for c in calls] == [["variable", "list", "--json"], ["variables", "--json"]]
    assert rc == 0 and "railway (1 key)" in out


def test_run_fly_uses_flyctl_when_fly_missing(tmp_path, capsys, monkeypatch):
    write(tmp_path, ".env.example", "A=\n")
    calls = fake_platform(monkeypatch, [FakeProc(0, '[{"name": "A", "digest": "d"}]')], on_path=("flyctl",))
    rc, _, _ = run(capsys, str(tmp_path), "--run", "fly", "--run-args", "-a my-app")
    assert calls == [["/bin/flyctl", "secrets", "list", "--json", "-a", "my-app"]]
    assert rc == 0


def test_run_not_installed(tmp_path, capsys, monkeypatch):
    write(tmp_path, ".env.example", "A=\n")
    fake_platform(monkeypatch, [], on_path=())
    rc, _, err = run(capsys, str(tmp_path), "--run", "fly")
    assert rc == 2 and "fly or flyctl not found on PATH" in err


def test_run_keeps_real_error_over_not_found(tmp_path, capsys, monkeypatch):
    write(tmp_path, ".env.example", "A=\n")
    fake_platform(monkeypatch, [FakeProc(1, stderr="Error: no app")], on_path=("fly",))
    rc, _, err = run(capsys, str(tmp_path), "--run", "fly")
    assert rc == 2 and "exited 1: Error: no app" in err


def test_run_failure_reports_exit_code(tmp_path, capsys, monkeypatch):
    write(tmp_path, ".env.example", "A=\n")
    fake_platform(monkeypatch, [FakeProc(1, stderr="Error: not logged in"),
                                FakeProc(1, stderr="\x1b[31mError: not logged in\x1b[0m")])
    rc, _, err = run(capsys, str(tmp_path), "--run", "vercel")
    assert rc == 2 and "exited 1: Error: not logged in" in err


def test_run_timeout(tmp_path, capsys, monkeypatch):
    write(tmp_path, ".env.example", "A=\n")
    monkeypatch.setattr(cli.shutil, "which", lambda n: "/bin/" + n)

    def boom(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 1)

    monkeypatch.setattr(cli.subprocess, "run", boom)
    rc, _, err = run(capsys, str(tmp_path), "--run", "railway")
    assert rc == 2 and "timed out" in err


def test_python_dash_m(tmp_path):
    write(tmp_path, ".env.example", "A=\n")
    write(tmp_path, ".env", "A=1\n")
    proc = subprocess.run([sys.executable, "-m", "amagi_env_diff", str(tmp_path)],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "No differences" in proc.stdout
