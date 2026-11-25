"""Re-encoding with ffmpeg: validation, segmenting and determinism."""

import hashlib
import shutil
import subprocess

import pytest

from dtn.errors import Invalid
from dtn.media import EncodingSettings, MediaLimits, check_file, encode, ffmpeg_version

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                                reason="ffmpeg is not installed")


def make_audio(path, seconds=7, *extra):
    """Real-looking audio: a tone mixed with seeded noise, so it compresses like music."""
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-f", "lavfi", "-i", f"anoisesrc=d={seconds}:a=0.05:seed=3",
         "-filter_complex", "amix=inputs=2", *extra, str(path)],
        check=True,
    )
    return path


def digest(encoded):
    h = hashlib.sha256(encoded.init)
    for data, ms in encoded.segments:
        h.update(data)
        h.update(str(ms).encode())
    return h.hexdigest()


@pytest.mark.parametrize("name,extra", [("a.wav", []), ("a.mp3", ["-c:a", "libmp3lame", "-b:a", "192k"]), ("a.flac", [])])
def test_encoding_is_deterministic(tmp_path, name, extra):
    path = make_audio(tmp_path / name, 7, *extra)
    first = encode(path, EncodingSettings(), timeout=60)
    second = encode(path, EncodingSettings(), timeout=60)
    assert digest(first) == digest(second)


def test_segments_cover_the_audio(tmp_path):
    path = make_audio(tmp_path / "a.wav", 7)
    encoded = encode(path, EncodingSettings(segment_seconds=2), timeout=60)
    durations = [ms for _, ms in encoded.segments]
    assert len(durations) == 4
    assert all(ms == 2000 for ms in durations[:-1])
    assert abs(sum(durations) - 7000) < 50
    assert encoded.init[4:8] == b"ftyp"


def test_tags_and_cover_art_do_not_change_the_audio(tmp_path):
    plain = make_audio(tmp_path / "plain.mp3", 5, "-c:a", "libmp3lame", "-b:a", "192k")
    cover = tmp_path / "cover.png"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=32x32",
                    "-frames:v", "1", str(cover)], check=True)
    tagged = tmp_path / "tagged.mp3"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(plain), "-i", str(cover),
                    "-map", "0:a", "-map", "1:v", "-c", "copy", "-metadata", "title=Otro título",
                    "-disposition:v", "attached_pic", str(tagged)], check=True)
    assert digest(encode(plain, EncodingSettings(), timeout=60)) == digest(encode(tagged, EncodingSettings(), timeout=60))


def test_check_file_returns_duration(tmp_path):
    assert check_file(make_audio(tmp_path / "a.wav", 3), MediaLimits(), timeout=30) == pytest.approx(3, abs=0.1)


def test_too_long_is_invalid(tmp_path):
    with pytest.raises(Invalid):
        check_file(make_audio(tmp_path / "a.wav", 3), MediaLimits(max_seconds=2), timeout=30)


def test_too_big_is_invalid(tmp_path):
    with pytest.raises(Invalid):
        check_file(make_audio(tmp_path / "a.wav", 3), MediaLimits(max_bytes=1000), timeout=30)


@pytest.mark.parametrize("content", [b"", b"esto no es audio\n" * 100])
def test_not_audio_is_invalid(tmp_path, content):
    path = tmp_path / "x.mp3"
    path.write_bytes(content)
    with pytest.raises(Invalid):
        check_file(path, MediaLimits(), timeout=30)


def test_missing_file_is_invalid(tmp_path):
    with pytest.raises(Invalid):
        check_file(tmp_path / "nope.wav", MediaLimits(), timeout=30)


def test_ffmpeg_version_is_reported():
    assert ffmpeg_version().startswith("ffmpeg version")
