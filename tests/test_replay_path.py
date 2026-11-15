"""Tests for the replay endpoint's path confinement.

The replay endpoint takes a filesystem path from the client. Without
confinement it is an arbitrary-file-read and file-existence oracle, so
these tests pin the behaviour: outside the capture roots is refused, the
error never echoes a resolved path back, and a symlink out of the root
is refused too.
"""

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "webapp" / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import pytest  # noqa: E402

app = pytest.importorskip("app")


def _configure(tmp_path, monkeypatch, extra_roots=()):
    """Point the resolver at a temporary capture root."""
    root = tmp_path / "captures"
    root.mkdir()
    monkeypatch.setattr(app, "RESULTS", root)
    monkeypatch.setattr(app, "REPLAY_CAP", root / "replay")
    monkeypatch.setattr(app, "ELRS_CAPTURES", tmp_path / "elrs")
    monkeypatch.setitem(app.CONFIG, "replay_roots", [str(p) for p in extra_roots])
    return root


def test_allows_a_file_inside_the_capture_root(tmp_path, monkeypatch):
    root = _configure(tmp_path, monkeypatch)
    capture = root / "sample.cs8"
    capture.write_bytes(b"\x00" * 16)
    resolved, reason = app.resolve_replay_source(str(capture))
    assert resolved == capture.resolve()
    assert reason == ""


def test_refuses_a_file_outside_the_capture_root(tmp_path, monkeypatch):
    root = _configure(tmp_path, monkeypatch)
    outside = tmp_path / "secret.txt"
    outside.write_text("classified")
    resolved, reason = app.resolve_replay_source(str(outside))
    assert resolved is None
    assert "outside" in reason


def test_error_does_not_echo_the_path(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    secret = tmp_path / "id_rsa"
    secret.write_text("PRIVATE KEY")
    resolved, reason = app.resolve_replay_source(str(secret))
    assert resolved is None
    assert str(secret) not in reason
    assert "id_rsa" not in reason


def test_refuses_a_symlink_that_escapes_the_root(tmp_path, monkeypatch):
    root = _configure(tmp_path, monkeypatch)
    target = tmp_path / "outside.cs8"
    target.write_bytes(b"\x00" * 16)
    link = root / "link.cs8"
    link.symlink_to(target)
    resolved, reason = app.resolve_replay_source(str(link))
    assert resolved is None
    assert "outside" in reason


def test_refuses_a_directory(tmp_path, monkeypatch):
    root = _configure(tmp_path, monkeypatch)
    (root / "subdir").mkdir()
    resolved, reason = app.resolve_replay_source(str(root / "subdir"))
    assert resolved is None
    assert reason


def test_refuses_a_file_that_is_not_a_capture(tmp_path, monkeypatch):
    """The endpoint replays IQ captures, so it takes capture files only.

    Without this, the home directory in the allowed roots would make it
    a reader for every other file the operator keeps there.
    """
    root = _configure(tmp_path, monkeypatch)
    doc = root / "notes.txt"
    doc.write_text("not a capture")
    resolved, reason = app.resolve_replay_source(str(doc))
    assert resolved is None
    assert reason


def test_refuses_a_key_file_by_name(tmp_path, monkeypatch):
    root = _configure(tmp_path, monkeypatch)
    key = root / "id_rsa"
    key.write_text("PRIVATE KEY")
    resolved, reason = app.resolve_replay_source(str(key))
    assert resolved is None
    assert reason == "path is not permitted"


def test_refuses_a_path_inside_a_dot_directory(tmp_path, monkeypatch):
    root = _configure(tmp_path, monkeypatch)
    hidden = root / ".ssh"
    hidden.mkdir()
    key = hidden / "id_ed25519"
    key.write_text("PRIVATE KEY")
    resolved, reason = app.resolve_replay_source(str(key))
    assert resolved is None
    assert reason == "path is not permitted"


def test_empty_path_is_rejected(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    resolved, reason = app.resolve_replay_source("")
    assert resolved is None
    assert reason == "path is required"
