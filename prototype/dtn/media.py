"""Re-encoding of uploaded audio into segments (decisions: «Cómo se divide el
audio en fragmentos», «Cómo se garantiza que repetir una subida produzca lo
mismo»).

The uploaded file is re-encoded with ffmpeg to Opus in fragmented MP4, split
into segments of about ``segment_seconds``: one initialization segment plus
independently decodable audio segments, with their durations taken from the
HLS playlist ffmpeg writes. Re-encoding the same file with the same settings
and the same ffmpeg version produces the same bytes, so repeating an upload
produces the same identifiers.
"""

import json
import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from dtn.errors import Invalid, Unavailable

log = logging.getLogger("dtn.media")

FFMPEG = os.environ.get("DTN_FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("DTN_FFPROBE", "ffprobe")


@dataclass(frozen=True)
class MediaLimits:
    max_seconds: float = 1200
    max_bytes: int = 209_715_200


@dataclass(frozen=True)
class EncodingSettings:
    segment_seconds: int = 2
    bitrate: str = "128k"


@dataclass(frozen=True)
class EncodedAudio:
    """Result of re-encoding: bytes of the init segment and of each audio
    segment with its duration in integer milliseconds."""

    init: bytes
    segments: tuple[tuple[bytes, int], ...]


def ffmpeg_version() -> str:
    """First line of ``ffmpeg -version``. Part of the configuration: another
    version may produce other bytes for the same file (principle P6)."""
    try:
        out = subprocess.run([FFMPEG, "-version"], capture_output=True, text=True, check=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        raise Unavailable(f"ffmpeg is not usable: {exc}") from exc
    return out.stdout.splitlines()[0] if out.stdout else "unknown"


def check_file(path: str | os.PathLike, limits: MediaLimits, *, timeout: float) -> float:
    """Check that ``path`` is decodable audio within ``limits``.

    Returns its duration in seconds. Raises ``Invalid`` if the file is too big,
    is not audio, or is too long; ``Unavailable`` if probing exceeds ``timeout``.
    """
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise Invalid(f"cannot read {path}: {exc.strerror}") from exc
    if size > limits.max_bytes:
        raise Invalid(f"file is {size} bytes, more than the limit of {limits.max_bytes}")
    if size == 0:
        raise Invalid("file is empty")
    try:
        out = subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_type:format=duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise Unavailable("probing the file took too long") from exc
    except OSError as exc:
        raise Unavailable(f"ffprobe is not usable: {exc}") from exc
    try:
        info = json.loads(out.stdout or "{}")
    except json.JSONDecodeError:
        info = {}
    if out.returncode != 0 or not info.get("streams"):
        raise Invalid("file is not decodable audio")
    try:
        duration = float(info.get("format", {}).get("duration", "nan"))
    except (TypeError, ValueError):
        duration = float("nan")
    if not duration > 0:
        raise Invalid("cannot determine the duration of the audio")
    if duration > limits.max_seconds:
        raise Invalid(f"audio lasts {duration:.0f} s, more than the limit of {limits.max_seconds:.0f} s")
    return duration


_EXTINF = re.compile(r"#EXTINF:([0-9.]+),")


def _parse_playlist(text: str) -> list[tuple[str, int]]:
    """(segment file name, duration in ms) in playback order."""
    entries, pending = [], None
    for line in text.splitlines():
        line = line.strip()
        m = _EXTINF.match(line)
        if m:
            # Round in decimal to milliseconds: the playlist always writes the
            # same text for the same encoding, so this is deterministic.
            pending = round(float(m.group(1)) * 1000)
        elif line and not line.startswith("#") and pending is not None:
            entries.append((line, pending))
            pending = None
    return entries


def encode(path: str | os.PathLike, settings: EncodingSettings, *, timeout: float) -> EncodedAudio:
    """Re-encode the first audio stream of ``path`` into Opus fMP4 segments.

    Only the audio is kept: cover art, other streams, metadata and chapters are
    dropped, and ffmpeg writes no version strings (bitexact), so the output
    depends only on the audio and on the settings. Raises ``Invalid`` if ffmpeg
    cannot decode the file and ``Unavailable`` if it exceeds ``timeout``.
    """
    with tempfile.TemporaryDirectory(prefix="dtn-encode-") as tmp:
        cmd = [
            FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(Path(path).resolve()),
            "-map", "0:a:0", "-vn", "-sn", "-dn",
            "-map_metadata", "-1", "-map_chapters", "-1",
            "-c:a", "libopus", "-b:a", settings.bitrate, "-ar", "48000", "-ac", "2",
            "-fflags", "+bitexact", "-flags:a", "+bitexact",
            "-f", "hls", "-hls_time", str(settings.segment_seconds),
            "-hls_segment_type", "fmp4", "-hls_fmp4_init_filename", "init.mp4",
            "-hls_playlist_type", "vod", "-hls_segment_filename", "seg%05d.m4s",
            "list.m3u8",
        ]
        try:
            out = subprocess.run(cmd, cwd=tmp, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise Unavailable("re-encoding took longer than the upload deadline") from exc
        except OSError as exc:
            raise Unavailable(f"ffmpeg is not usable: {exc}") from exc
        if out.returncode != 0:
            raise Invalid(f"ffmpeg could not re-encode the file: {out.stderr.strip()[-300:]}")
        tmp = Path(tmp)
        entries = _parse_playlist((tmp / "list.m3u8").read_text())
        if not entries:
            raise Invalid("re-encoding produced no audio")
        segments = tuple(((tmp / name).read_bytes(), ms) for name, ms in entries)
        return EncodedAudio(init=(tmp / "init.mp4").read_bytes(), segments=segments)
