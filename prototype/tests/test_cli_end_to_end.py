"""The ``dtn`` command against real stores, as a user runs it.

The stores run in this test session's Ray instance, in a namespace of their
own; ``python -m dtn`` runs as a separate process that attaches to that Ray
instance, like any command typed in a terminal.
"""

import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from dtn import admin
from dtn.config import StorageConfig

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")

PROTOTYPE = Path(__file__).resolve().parents[1]


@pytest.fixture
def dtn(ray_runtime, tmp_path):
    """Start real stores and return a function that runs ``python -m dtn``."""
    namespace = f"cli-{uuid.uuid4().hex[:8]}"
    config = StorageConfig(data_dir=tmp_path / "data")
    admin.start_stores(config, namespace=namespace)

    def run(*args):
        # Read the environment at each call: a test may change it.
        env = {**os.environ, "DTN_RAY_NAMESPACE": namespace, "PYTHONPATH": str(PROTOTYPE)}
        result = subprocess.run(
            [sys.executable, "-m", "dtn", *args],
            cwd=PROTOTYPE,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
        return result.returncode, result.stdout, result.stderr

    yield run
    admin.stop_stores(config, namespace=namespace)


@pytest.fixture
def song(tmp_path):
    path = tmp_path / "song.flac"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=7", str(path)],
        check=True,
    )
    return path


def test_upload_search_withdraw_through_the_command(dtn, song):
    meta = ["--title", "Canción del mar", "--artist", "Ana", "--license", "CC0-1.0"]

    code, out, err = dtn("upload", str(song), *meta)
    assert code == 0, err
    track_id = out.strip()

    code, out, _ = dtn("upload", str(song), *meta)
    assert (code, out.strip()) == (0, track_id)  # repeating an upload is safe

    code, out, _ = dtn("search", "cancion", "--json")
    doc = json.loads(out)
    assert code == 0 and doc["complete"] is True
    assert [t["track_id"] for t in doc["tracks"]] == [track_id]

    code, _, err = dtn("withdraw", track_id, "--reason", "no license")
    assert code == 0, err

    code, out, _ = dtn("list", "--json")
    assert json.loads(out)["tracks"] == []

    code, _, err = dtn("upload", str(song), "--title", "Otro", "--artist", "Ana", "--license", "CC0-1.0")
    assert code == 5 and "Conflict" in err  # the recording is withdrawn


def test_a_client_with_another_n_is_refused(dtn, song, monkeypatch):
    monkeypatch.setenv("DTN_REPLICAS", "2")
    code, _, err = dtn("list")
    assert code == 1 and "ConfigError" in err
