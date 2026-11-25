"""The dtn command: output and exit codes (contract § 6)."""

import io
import json

import pytest

from dtn.cli import main
from dtn.client import Client
from dtn.fake_cluster import FakeCluster
from tests.test_client import fake_encoder


@pytest.fixture
def cluster():
    return FakeCluster()


@pytest.fixture
def run(cluster):
    client = Client(cluster, encoder=fake_encoder)

    def run(*argv):
        out, err = io.StringIO(), io.StringIO()
        code = main(list(argv), client_factory=lambda: client, out=out, err=err)
        return code, out.getvalue(), err.getvalue()

    return run


@pytest.fixture
def song(tmp_path):
    path = tmp_path / "song.wav"
    path.write_bytes(b"RIFF fake audio content " * 10)
    return str(path)


def upload(run, song, title="Mar"):
    code, out, _ = run("upload", song, "--title", title, "--artist", "Ana", "--license", "CC0-1.0")
    assert code == 0
    return out.strip()


def test_upload_prints_the_track_id(run, song):
    assert len(upload(run, song)) == 64


def test_search_text_and_json(run, song):
    track_id = upload(run, song)
    code, out, _ = run("search", "mar")
    assert code == 0 and track_id in out and "resultado completo" in out
    code, out, _ = run("--json", "search", "mar")
    doc = json.loads(out)
    assert doc["complete"] is True and doc["tracks"][0]["track_id"] == track_id
    code, out, _ = run("list", "--json")  # --json is also accepted after the command
    assert json.loads(out)["tracks"][0]["title"] == "Mar"


def test_incomplete_search_still_exits_0_and_says_so(run, cluster, song):
    upload(run, song)
    cluster.down("store-1", "store-2", "store-3")
    code, out, _ = run("list")
    assert code == 0 and "INCOMPLETO" in out and "store-1, store-2, store-3" in out


def test_exit_codes(run, cluster, song):
    assert run("upload", song, "--title", "", "--artist", "Ana", "--license", "CC0-1.0")[0] == 2
    assert run("withdraw", "f" * 64, "--reason", "x")[0] == 3
    track_id = upload(run, song)
    assert run("withdraw", track_id, "--reason", "sin licencia")[0] == 0
    assert run("upload", song, "--title", "Mar", "--artist", "Ana", "--license", "CC0-1.0")[0] == 5
    cluster.down("store-1", "store-2", "store-3")
    code, _, err = run("withdraw", "f" * 64, "--reason", "x")
    assert code == 4 and "incierto" in err


def test_errors_in_json(run):
    code, _, err = run("--json", "withdraw", "f" * 64, "--reason", "x")
    doc = json.loads(err)
    assert code == 3 and doc["error"] == "NotFound" and doc["op_id"]


def test_play_is_declared_but_not_implemented(run):
    code, _, err = run("play", "f" * 64)
    assert code == 1 and "not implemented" in err


def test_without_storage_the_command_says_so(song):
    err = io.StringIO()
    assert main(["list"], err=err, out=io.StringIO()) == 1
    assert "not available" in err.getvalue()
