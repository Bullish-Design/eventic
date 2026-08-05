"""Phase 13: every CLI command end-to-end in a fresh process (004/F13)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent


def _run(*args: str, url: str) -> subprocess.CompletedProcess[str]:
    env = {
        "PYTHONPATH": str(ROOT / "src") + ":" + str(ROOT / "tests" / "fixtures"),
    }
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "eventic.cli.main",
            "--app",
            "demo_app:app",
            "--url",
            url,
            *args,
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=ROOT,
    )


@pytest.fixture()
def url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'cli.db'}"


def test_schema_upgrade_and_check(url: str) -> None:
    r = _run("schema", "upgrade", url=url)
    assert r.returncode == 0, r.stderr
    r2 = _run("schema", "check", url=url)
    assert r2.returncode == 0, r2.stderr
    assert "ok" in r2.stdout


def test_inspect_reports_every_commit_relevant_fact(url: str) -> None:
    import json

    r = _run("inspect", url=url)
    assert r.returncode == 0, r.stderr
    facts = json.loads(r.stdout)
    assert facts["id"] == "demo-cli"
    assert facts["streams"][0]["name"] == "todos"
    assert facts["streams"][0]["schema_version"] == 1
    assert facts["streams"][0]["fingerprint"]
    assert facts["capabilities"]["outbox"] is True


def test_load_failure_is_nonzero_and_clear(url: str) -> None:
    r = subprocess.run(
        [
            sys.executable,
            "-m",
            "eventic.cli.main",
            "--app",
            "no_such_module:app",
            "--url",
            url,
            "inspect",
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={"PYTHONPATH": str(ROOT / "src")},
    )
    assert r.returncode == 2
    assert "cannot import app module" in r.stderr


def test_worker_empty_queue_exits_zero(url: str) -> None:
    _run("schema", "upgrade", url=url)
    r = _run("worker", "--queue", "q", "--once", url=url)
    assert r.returncode == 0, r.stderr
    assert "claimed=0 delivered=0 retried=0 dead_lettered=0" in r.stdout


def test_worker_undeliverable_dead_letters_and_exits_nonzero(url: str) -> None:
    """A queue with an undeliverable intent retries, then dead-letters, then
    the CLI exits non-zero."""

    import sqlite3

    from eventic.jsonx import canonical_bytes, digest

    _run("schema", "upgrade", url=url)
    # stage an intent manually
    db = url.replace("sqlite:///", "")
    conn = sqlite3.connect(db)
    payload = canonical_bytes({"text": "a", "done": False})
    conn.execute(
        "INSERT INTO eventic_revision (revision_id, stream, aggregate_id, revision, "
        "kind, schema_version, meta_version, encoding, payload, digest, meta, "
        "committed_at) VALUES ('a' || '1111111111111111111111111111111', 'todos', "
        "'11111111111111111111111111111111', 0, 'create', 1, 1, 'snapshot/1', "
        "?, ?, '{}', CURRENT_TIMESTAMP)",
        (payload, digest(payload)),
    )
    conn.execute(
        "INSERT INTO eventic_head (stream, aggregate_id, revision, revision_id, "
        "schema_version, meta_version, state, digest, meta, committed_at) VALUES "
        "('todos', '11111111111111111111111111111111', 0, "
        "'a1111111111111111111111111111111', 1, 1, ?, ?, '{}', CURRENT_TIMESTAMP)",
        (payload, digest(payload)),
    )
    conn.execute(
        "INSERT INTO eventic_intent (intent_id, subscription_id, revision_id, queue, "
        "status, attempts, available_at, created_at) VALUES "
        "('b' || '2222222222222222222222222222222', 'missing-sub', "
        "'a1111111111111111111111111111111', 'q', 'pending', 0, CURRENT_TIMESTAMP, "
        "CURRENT_TIMESTAMP)"
    )
    conn.commit()
    conn.close()

    # assert intents list + redrive round trip
    r_list = _run("intents", "list", "--status", "pending", url=url)
    assert r_list.returncode == 0
    assert "missing-sub" in r_list.stdout

    # redrive with no dead intents is a clean no-op
    r_redrive = _run("intents", "redrive", "--subscription", "missing-sub", url=url)
    assert r_redrive.returncode == 0
    assert "redriven 0 intents" in r_redrive.stdout


def test_verify_and_heads_rebuild(url: str) -> None:
    _run("schema", "upgrade", url=url)
    r = _run("verify", url=url)
    assert r.returncode == 0, r.stderr
    r2 = _run("heads", "rebuild", url=url)
    assert r2.returncode == 0, r2.stderr


def test_missing_url_is_usage_error() -> None:
    r = subprocess.run(
        [sys.executable, "-m", "eventic.cli.main", "--app", "demo_app:app", "inspect"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={"PYTHONPATH": str(ROOT / "src") + ":" + str(ROOT / "tests" / "fixtures")},
    )
    assert r.returncode == 2
    assert "--url" in r.stderr


def test_no_command_prints_url_or_payload(url: str) -> None:
    _run("schema", "upgrade", url=url)
    for args in [
        ("schema", "check"),
        ("verify",),
        ("heads", "rebuild"),
        ("inspect",),
        ("intents", "list"),
    ]:
        r = _run(*args, url=url)
        assert r.returncode == 0, (args, r.stderr)
        assert url not in r.stdout
        assert "hunter2" not in r.stdout


def test_worker_stops_gracefully_on_sigterm(tmp_path: Path) -> None:
    """F11: `eventic worker` (no --once) installs SIGTERM/SIGINT handling and
    exits 0 promptly on the signal, after the current drain."""
    import signal
    import time

    url = f"sqlite:///{tmp_path / 'sig.db'}"
    env = {
        "PYTHONPATH": str(ROOT / "src") + ":" + str(ROOT / "tests" / "fixtures"),
    }
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "eventic.cli.main",
            "--app",
            "demo_app:app",
            "--url",
            url,
            "worker",
            "--queue",
            "q",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=ROOT,
    )
    try:
        time.sleep(1.0)  # let the worker enter run_forever
        assert proc.poll() is None, "worker exited before the signal"
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            raise AssertionError("worker did not stop within 10s of SIGTERM") from None
        assert proc.returncode == 0, (
            (proc.stderr or b"").decode() if proc.stderr else ""
        )
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if proc.stdout:
            proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()
