# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""ffmpeg, the one encoder and decoder of the Video Studio.

Every video or audio transformation goes through this module, as an argument
list to the ``ffmpeg`` / ``ffprobe`` binaries (``VIDEO_STUDIO_FFMPEG_PATH`` /
``VIDEO_STUDIO_FFPROBE_PATH``, else the ones on ``PATH``): never a shell, never
a string a model wrote spliced into a filter. The pieces:

* :func:`probe` - duration, size, frame rate, audio of a media file;
* :func:`frames_to_mezzanine` - the variable-frame-rate screencast of a take
  (a frame only when the page painted) as a constant-frame-rate H.264
  mezzanine: each frame lasts until the next one was painted;
* :class:`FramePipeEncoder` - the compositor's frames, piped in as JPEG or
  PNG, encoded with the quality preset's settings (BT.709, limited range,
  ``+faststart`` so a browser plays it while it downloads);
* :class:`FrameDecoder` - one media file decoded forward as JPEG frames, for
  the compositor's frame server (the time maps are monotonic, so one forward
  pass serves a whole scene);
* :func:`condition_voice` - a narration clip cleaned (silences trimmed,
  high-pass, de-esser, gentle compression) and brought to the reference
  loudness;
* :func:`prepare_bed` - a music track as the bed of one video: its length,
  crossfaded loops or a natural ending, at the reference loudness;
* :func:`mix_audio` - the voice stem (``stitch.py``: the narrations already
  placed), the kept sound of AI clips, the bed ducked under them by an
  envelope computed from where they are, the master normalised to -16 LUFS in
  two passes, a scene preview's window cut before its one AAC encode;
* :func:`demux_audio`, :func:`decode_mono`, :func:`iter_gray_frames` - what
  the render's checks hear and watch (``audio_qc``, ``frame_freeze``);
* :func:`transform_frames` - a video re-encoded with each of its frames
  passed through a function (raw RGB in, raw RGB out, streamed): the screen
  replacement of an AI shot (``screens.py``);
* :func:`mux`, :func:`concat`, :func:`extract_frame`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import re
import shutil
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from app.core.config import settings
from app.core.utils import format_exception_message
from app.services.video_studio import resources
from app.services.video_studio.resources import cpu_budget

logger = logging.getLogger(__name__)

#: How long one ffmpeg call may run before it is killed, by default.
DEFAULT_TIMEOUT_SECONDS = 1800.0
_JPEG_SOI = b"\xff\xd8"
_JPEG_EOI = b"\xff\xd9"


class EncoderError(RuntimeError):
    """An ffmpeg call that failed; ``str()`` is the tail of its log, safe to show."""


class EncoderUnavailableError(EncoderError):
    """ffmpeg is not installed where the platform can run it."""


def ffmpeg_path() -> str:
    configured = (settings.video_studio_ffmpeg_path or "").strip()
    found = shutil.which(configured or "ffmpeg")
    if not found:
        raise EncoderUnavailableError(
            "ffmpeg is not installed on this platform: the Video Studio needs it to encode videos "
            "(the platform image ships it; set VIDEO_STUDIO_FFMPEG_PATH for another location)."
        )
    return found


def ffprobe_path() -> str:
    configured = (settings.video_studio_ffprobe_path or "").strip()
    found = shutil.which(configured or "ffprobe")
    if not found:
        raise EncoderUnavailableError(
            "ffprobe is not installed on this platform: the Video Studio needs it to read media "
            "(set VIDEO_STUDIO_FFPROBE_PATH for another location)."
        )
    return found


def encoder_available() -> bool:
    try:
        ffmpeg_path()
        ffprobe_path()
    except EncoderUnavailableError:
        return False
    return True


def _tail(text: str, lines: int = 12) -> str:
    kept = [line for line in text.strip().splitlines() if line.strip()][-lines:]
    return "\n".join(kept)[-2000:]


#: How often a long run says it is still working (``on_wait``): well inside a render's stall window.
WAIT_REPORT_SECONDS = 10.0
#: How often an encode that says how far it got reports its frames muxed (:func:`frames_to_mezzanine`).
PROGRESS_REPORT_SECONDS = 5.0


async def run(
    binary: str,
    args: Sequence[str],
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    cwd: str | None = None,
    on_wait: Callable[[], Awaitable[None]] | None = None,
    nice: int = 0,
    wait_every: float | None = None,
) -> tuple[bytes, str]:
    """Run *binary* with *args*; ``(stdout, stderr)`` or :class:`EncoderError`.

    *on_wait* is awaited every *wait_every* seconds (:data:`WAIT_REPORT_SECONDS` by default) while it runs:
    a long encode is work the render's stall window must see, bounded by *timeout* all the same. *nice* starts it
    that many steps below the worker's CPU priority (``resources.niced``).
    """
    every = WAIT_REPORT_SECONDS if wait_every is None else wait_every
    process = await asyncio.create_subprocess_exec(
        *resources.niced([binary, *args], nice),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    try:
        talking = asyncio.ensure_future(process.communicate())
        try:
            async with asyncio.timeout(timeout):
                while not talking.done():
                    await asyncio.wait({talking}, timeout=every if on_wait is not None else None)
                    if not talking.done() and on_wait is not None:
                        await on_wait()
        finally:
            if not talking.done():
                talking.cancel()
        stdout, stderr = talking.result()
    except (TimeoutError, asyncio.CancelledError):
        process.kill()
        await process.wait()
        raise
    text = stderr.decode("utf-8", errors="replace")
    if process.returncode != 0:
        raise EncoderError(f"{Path(binary).stem} failed (exit {process.returncode}):\n{_tail(text)}")
    return stdout, text


async def ffmpeg(
    args: Sequence[str],
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    cwd: str | None = None,
    on_wait: Callable[[], Awaitable[None]] | None = None,
    nice: int = 0,
    wait_every: float | None = None,
) -> str:
    _, stderr = await run(
        ffmpeg_path(),
        ["-hide_banner", "-nostdin", "-y", *args],
        timeout=timeout,
        cwd=cwd,
        on_wait=on_wait,
        nice=nice,
        wait_every=wait_every,
    )
    return stderr


# ── Probe ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class MediaInfo:
    duration: float
    width: int
    height: int
    fps: float
    frame_count: int
    has_audio: bool
    has_video: bool
    #: The first audio stream's sample rate and channels; 0 without one.
    sample_rate: int = 0
    channels: int = 0


def _rate(value: str | None) -> float:
    if not value or value in ("0/0", "0"):
        return 0.0
    if "/" in value:
        num, den = value.split("/", 1)
        try:
            return float(num) / float(den) if float(den) else 0.0
        except ValueError:
            return 0.0
    try:
        return float(value)
    except ValueError:
        return 0.0


async def probe(path: str | os.PathLike[str]) -> MediaInfo:
    stdout, _ = await run(
        ffprobe_path(),
        ["-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        timeout=120,
    )
    data = json.loads(stdout.decode("utf-8", errors="replace") or "{}")
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    duration = 0.0
    for candidate in (
        data.get("format", {}).get("duration"),
        (video or {}).get("duration"),
        (audio or {}).get("duration"),
    ):
        try:
            duration = float(candidate)
            if duration > 0:
                break
        except (TypeError, ValueError):
            continue
    fps = _rate((video or {}).get("avg_frame_rate")) or _rate((video or {}).get("r_frame_rate"))
    frames = 0
    try:
        frames = int((video or {}).get("nb_frames") or 0)
    except ValueError:
        frames = 0
    if not frames and fps and duration:
        frames = round(duration * fps)
    return MediaInfo(
        duration=duration,
        width=int((video or {}).get("width") or 0),
        height=int((video or {}).get("height") or 0),
        fps=fps,
        frame_count=frames,
        has_audio=audio is not None,
        has_video=video is not None,
        sample_rate=_int((audio or {}).get("sample_rate")),
        channels=_int((audio or {}).get("channels")),
    )


def _int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


# ── Video encoding ────────────────────────────────────────────────────────

#: Every encode tags BT.709 limited range: what browsers, players and editors
#: assume for HD, so colours match the app that was recorded.
_COLOR_TAGS = ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv"]
_TO_BT709 = "scale=flags=lanczos:out_color_matrix=bt709:out_range=tv"


def x264_args(*, crf: int, preset: str, fps: int, threads: int | None = None) -> list[str]:
    """x264's arguments; *threads*: its share of the CPUs (a render page's), else every CPU this process may use."""
    return [
        "-c:v",
        "libx264",
        "-threads",
        str(max(1, int(threads)) if threads else cpu_budget()),
        "-preset",
        preset,
        "-crf",
        str(crf),
        "-profile:v",
        "high",
        "-pix_fmt",
        "yuv420p",
        "-r",
        str(fps),
        "-g",
        str(fps * 2),
        *_COLOR_TAGS,
        "-movflags",
        "+faststart",
    ]


async def frames_to_mezzanine(
    frames: Sequence[tuple[float, str]],
    *,
    end_time: float,
    out_path: str,
    fps: int,
    crf: int = 14,
    preset: str = "slow",
    workdir: str,
    on_wait: Callable[[], Awaitable[None]] | None = None,
    nice: int = 0,
    on_progress: Callable[[int, int], Awaitable[None]] | None = None,
) -> None:
    """Encode the screencast *frames* (``(seconds, file name in workdir)``) at a constant *fps*.

    Each frame lasts until the next one; the last until *end_time*. *on_wait* is awaited while it encodes, and *nice*
    lowers its CPU priority (:func:`run`). *on_progress* is awaited every :data:`PROGRESS_REPORT_SECONDS`
    with the frames muxed so far (0 before ffmpeg's first ``-progress`` report) and the frames the mezzanine holds.
    """
    if not frames:
        raise EncoderError("The recording captured no frame.")
    report: list[str] = []
    waited = on_wait
    if on_progress is not None:
        report_path = Path(workdir) / "encode-progress.txt"
        report = ["-progress", report_path.name]
        total = max(1, math.ceil(end_time * fps))

        async def reported() -> None:
            if on_wait is not None:
                await on_wait()
            await on_progress(min(muxed_frames(report_path) or 0, total), total)

        waited = reported
    lines = ["ffconcat version 1.0"]
    for index, (t, name) in enumerate(frames):
        following = frames[index + 1][0] if index + 1 < len(frames) else max(end_time, t + 1 / fps)
        duration = max(following - t, 1 / (fps * 4))
        lines.append(f"file '{name}'")
        lines.append(f"duration {duration:.6f}")
    # The concat demuxer drops the last entry's duration unless it is repeated.
    lines.append(f"file '{frames[-1][1]}'")
    list_path = Path(workdir) / "frames.ffconcat"
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    await ffmpeg(
        [
            *report,
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            list_path.name,
            "-fps_mode",
            "cfr",
            "-vf",
            "scale=trunc(iw/2)*2:trunc(ih/2)*2:flags=lanczos:out_color_matrix=bt709:out_range=tv",
            *x264_args(crf=crf, preset=preset, fps=fps),
            "-an",
            out_path,
        ],
        cwd=workdir,
        on_wait=waited,
        nice=nice,
        wait_every=PROGRESS_REPORT_SECONDS if on_progress is not None else None,
    )


_PROGRESS_FRAME = re.compile(r"^frame=(\d+)\s*$", re.MULTILINE)


def muxed_frames(report_path: Path) -> int | None:
    """The frames an encode muxed so far, the last ``frame=`` of its ``-progress`` report; ``None`` before its first."""
    try:
        found = _PROGRESS_FRAME.findall(report_path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
    return int(found[-1]) if found else None


#: What :meth:`FramePipeEncoder.write` hands the pipe at once: a 2160p PNG frame is tens of MiB.
_PIPE_CHUNK = 1 << 20


def motion_blur_filter(samples: int) -> str:
    """The filters averaging each run of *samples* sub-frames into one frame (motion blur); empty for 1.

    ``tmix`` averages every window of *samples* frames; ``select`` keeps the window
    closing each frame's own sub-frames, and ``setpts`` puts the frames back on the
    frame rate's clock from the first.
    """
    if samples <= 1:
        return ""
    weights = " ".join("1" for _ in range(samples))
    return (
        f"tmix=frames={samples}:weights='{weights}',select='eq(mod(n\\,{samples})\\,{samples - 1})',"
        "setpts=PTS-STARTPTS,"
    )


class FramePipeEncoder:
    """An ffmpeg process encoding frames written to its stdin, in order, each one as soon as it is drawn.

    Nothing holds a frame on its way: :meth:`write` returns once the pipe took it, so a page draws its next frame
    while x264 holds only its own lookahead. *threads*: its x264 threads (a render page's share of the CPUs).
    *subframes* (motion blur): each output frame is written as that many frames over its shutter, averaged
    here (``tmix``) into one - a still frame averages to itself.
    """

    def __init__(
        self,
        *,
        out_path: str,
        fps: int,
        frame_format: str,
        crf: int,
        preset: str,
        width: int,
        height: int,
        threads: int | None = None,
        subframes: int = 1,
    ) -> None:
        self.out_path = out_path
        self.fps = fps
        self.frame_format = frame_format
        self.crf = crf
        self.preset = preset
        self.width = width
        self.height = height
        self.threads = threads
        self.subframes = max(1, int(subframes))
        self.frames_written = 0
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task | None = None
        self._stderr = bytearray()

    async def start(self) -> None:
        codec = "png" if self.frame_format == "png" else "mjpeg"
        args = [
            ffmpeg_path(),
            "-hide_banner",
            "-nostdin",
            "-y",
            "-f",
            "image2pipe",
            "-framerate",
            str(self.fps * self.subframes),
            "-c:v",
            codec,
            "-i",
            "-",
            "-vf",
            motion_blur_filter(self.subframes)
            + f"scale={self.width}:{self.height}:{_TO_BT709.removeprefix('scale=')},format=yuv420p",
            *x264_args(crf=self.crf, preset=self.preset, fps=self.fps, threads=self.threads),
            "-an",
            self.out_path,
        ]
        self._process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        if self._process is None or self._process.stderr is None:
            return
        while True:
            chunk = await self._process.stderr.read(65536)
            if not chunk:
                return
            self._stderr.extend(chunk)
            if len(self._stderr) > 200_000:
                del self._stderr[:100_000]

    async def write(self, frame: bytes) -> None:
        """Hand *frame* to the encoder, a chunk at a time: the pipe's transport never buffers a copy of a whole frame."""
        if self._process is None or self._process.stdin is None:
            raise EncoderError("The encoder is not running.")
        try:
            view = memoryview(frame)
            for start in range(0, len(view), _PIPE_CHUNK):
                self._process.stdin.write(view[start : start + _PIPE_CHUNK])
                await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise EncoderError(f"The encoder stopped: {_tail(self._stderr.decode('utf-8', 'replace'))}") from exc
        self.frames_written += 1

    async def finish(
        self, *, timeout: float = DEFAULT_TIMEOUT_SECONDS, on_wait: Callable[[], Awaitable[None]] | None = None
    ) -> None:
        """Close the input and wait for the encoder to flush; *on_wait* is awaited every :data:`WAIT_REPORT_SECONDS` meanwhile.

        At 2160p the encoder's lookahead takes long to flush once the frames stop coming: what waits on it
        says it is still working.
        """
        if self._process is None:
            return
        if self._process.stdin is not None:
            self._process.stdin.close()
        exited = asyncio.ensure_future(self._process.wait())
        try:
            async with asyncio.timeout(timeout):
                while not exited.done():
                    await asyncio.wait({exited}, timeout=WAIT_REPORT_SECONDS if on_wait is not None else None)
                    if not exited.done() and on_wait is not None:
                        await on_wait()
        except TimeoutError:
            self._process.kill()
            raise EncoderError("The encoder did not finish in time.") from None
        finally:
            if not exited.done():
                exited.cancel()
        if self._stderr_task is not None:
            await self._stderr_task
        if self._process.returncode != 0:
            raise EncoderError(
                f"ffmpeg failed (exit {self._process.returncode}):\n{_tail(self._stderr.decode('utf-8', 'replace'))}"
            )

    async def abort(self) -> None:
        if self._process is not None and self._process.returncode is None:
            self._process.kill()
            try:
                await asyncio.wait_for(self._process.wait(), 10)
            except Exception as exc:  # pragma: no cover - best effort
                logger.debug("Encoder kill did not complete: %s", format_exception_message(exc))
        if self._stderr_task is not None:
            self._stderr_task.cancel()


class FrameDecoder:
    """One media file decoded forward from a time, as JPEG frames at the file's own rate.

    One thread each way: a decoder feeds one compositor page, which draws far
    fewer frames a second than a thread decodes, and a render keeps one open
    per scene on screen.
    """

    def __init__(self, path: str, *, start: float = 0.0, max_width: int | None = None, quality: int = 2) -> None:
        self.path = path
        self.start = max(0.0, start)
        self.max_width = max_width
        self.quality = quality
        self._process: asyncio.subprocess.Process | None = None
        self._buffer = bytearray()

    async def open(self) -> None:
        args = [ffmpeg_path(), "-hide_banner", "-nostdin", "-loglevel", "error", "-threads", "1"]
        if self.start > 0:
            args += ["-ss", f"{self.start:.4f}"]
        args += ["-i", self.path, "-an"]
        if self.max_width:
            args += ["-vf", f"scale='min({self.max_width},iw)':-2:flags=lanczos"]
        args += ["-f", "image2pipe", "-c:v", "mjpeg", "-threads", "1", "-q:v", str(self.quality), "-"]
        self._process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

    async def frames(self) -> AsyncIterator[bytes]:
        if self._process is None:
            await self.open()
        if self._process is None or self._process.stdout is None:
            raise EncoderError("The decoder did not start.")
        while True:
            end = self._buffer.find(_JPEG_EOI)
            while end == -1:
                chunk = await self._process.stdout.read(1 << 20)
                if not chunk:
                    if self._buffer.startswith(_JPEG_SOI):
                        yield bytes(self._buffer)
                    self._buffer.clear()
                    return
                self._buffer.extend(chunk)
                end = self._buffer.find(_JPEG_EOI)
            frame = bytes(self._buffer[: end + 2])
            del self._buffer[: end + 2]
            if frame.startswith(_JPEG_SOI):
                yield frame

    async def close(self) -> None:
        if self._process is not None and self._process.returncode is None:
            self._process.kill()
            try:
                await asyncio.wait_for(self._process.wait(), 5)
            except Exception as exc:  # pragma: no cover - best effort
                logger.debug("Decoder kill did not complete: %s", format_exception_message(exc))


# ── Audio ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class VoiceTrack:
    """The voice stem (``stitch.stitch``): every narration on one mono track from the video's start, at the mix rate."""

    path: str
    volume: float = 1.0


@dataclass(frozen=True, slots=True)
class ClipTrack:
    """A clip's own sound in the mix: its seconds played, levelled with the voice, under the narration."""

    path: str
    start: float
    trim_start: float
    duration: float  # output seconds
    speed: float = 1.0
    #: Its level against the voice once :attr:`gain_db` brought it to the reference.
    volume: float = 1.0
    #: :func:`clip_gain` of the seconds played.
    gain_db: float = 0.0
    fade_in: float = 0.15
    fade_out: float = 0.3
    #: Where someone speaks, ``[(start, end)]`` seconds of the video: it goes
    #: down by :data:`CLIP_DUCK_DB` there, with the music's envelope.
    windows: tuple[tuple[float, float], ...] = ()


@dataclass(frozen=True, slots=True)
class MusicTrack:
    """A prepared bed (:func:`prepare_bed`): the video's length, at :data:`REFERENCE_LUFS`."""

    path: str
    #: Its level where no one speaks, against the voice (both at the reference loudness).
    volume: float = 0.5
    #: How far it goes down under the voice, in dB; 0 leaves it level.
    duck_db: float = 10.0
    #: Where someone speaks or a clip's own sound plays: ``[(start, end)]`` seconds.
    windows: tuple[tuple[float, float], ...] = ()


#: Narration and music beds are brought to this loudness before the mix,
#: so a bed's ``volume`` is its level against the voice whatever file it came
#: from, and every provider and every scene speaks at the same level.
REFERENCE_LUFS = -18.0
#: The master: what streaming platforms and players expect for speech-led video.
MASTER_LUFS = -16.0
MASTER_TRUE_PEAK = -1.5
MASTER_LRA = 11.0

#: The ducking envelope: down this long before a sentence, back up this long
#: after it; two sentences closer than the bridge keep the music down between.
DUCK_LEAD = 0.35
DUCK_RELEASE = 0.7
DUCK_BRIDGE = 1.1

#: How far a clip's own sound goes down under the narration, in dB: the voice
#: leads, the shot's sound stays under it like an ambience.
CLIP_DUCK_DB = 10.0
#: A clip's sound is brought toward the reference: cut by up to 18 dB, lifted
#: by at most this much, so a quiet ambience stays an ambience.
CLIP_MAX_LIFT_DB = 6.0

#: How a narration clip is cleaned before the mix: the silence a speech engine
#: leaves before and after the words trimmed (a breath kept), the rumble under
#: 75 Hz removed, the sibilants tamed, a gentle compression evening the phrase.
VOICE_CHAIN = (
    "silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.06,"
    "areverse,silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.12,areverse,"
    "highpass=f=75,deesser=i=0.4:m=0.5:f=0.5,"
    "acompressor=threshold=-21dB:ratio=2.5:attack=8:release=140:makeup=1"
)
#: The version of the voice chain: a cached narration made by another one is not reused.
VOICE_CHAIN_VERSION = 1

_LOUDNESS_RE = re.compile(r"I:\s+(-?inf|-?\d+(?:\.\d+)?)\s+LUFS")


async def measure_loudness(path: str, *, start: float = 0.0, seconds: float | None = None) -> float | None:
    """The integrated loudness of *path* (from *start*, *seconds* of it) in LUFS (EBU R128); ``None`` for silence."""
    window = (["-ss", f"{start:.3f}"] if start > 0 else []) + (["-t", f"{seconds:.3f}"] if seconds else [])
    stderr = await ffmpeg(
        ["-nostats", *window, "-i", path, "-af", "ebur128=framelog=quiet", "-f", "null", "-"], timeout=600
    )
    found = _LOUDNESS_RE.findall(stderr)
    if not found or "inf" in found[-1]:
        return None
    value = float(found[-1])
    return value if value > -70.0 else None


_MOMENTARY_RE = re.compile(r"\bt:\s*(\d+(?:\.\d+)?)\s+TARGET:.*?\bM:\s*(-?(?:inf|nan|\d+(?:\.\d+)?))", re.I)


async def momentary_loudness(path: str) -> list[tuple[float, float]]:
    """The momentary loudness of *path* (EBU R128): ``[(t, LUFS)]``, the 400 ms ending at *t*, every 100 ms.

    The gating blocks of BS.1770, read from ``ebur128``'s frame log; a block
    before the first 400 ms are heard is left out, and silence reads -120.7.
    :class:`EncoderError` when the log holds no block: a measure that read
    nothing, never a silent file.
    """
    stderr = await ffmpeg(["-nostats", "-i", path, "-af", "ebur128=framelog=info", "-f", "null", "-"], timeout=600)
    blocks: list[tuple[float, float]] = []
    for at, value in _MOMENTARY_RE.findall(stderr):
        t = float(at)
        if t < 0.4 - 0.01:
            continue
        lufs = float(value)
        blocks.append((t, lufs if math.isfinite(lufs) else -120.7))
    if not blocks:
        raise EncoderError(f"No momentary loudness was read from {Path(path).name}:\n{_tail(stderr)}")
    return blocks


_FRAME_PEAK_RE = re.compile(r"\bt:\s*(\d+(?:\.\d+)?)\s+TARGET:.*?\bTPK:\s*((?:-?(?:inf|nan|\d+(?:\.\d+)?)\s*)+)dBFS", re.I)
_TRUE_PEAK_RE = re.compile(r"True peak:\s*Peak:\s*(-?(?:inf|\d+(?:\.\d+)?))\s*dBFS", re.I | re.S)


@dataclass(frozen=True, slots=True)
class LoudnessLog:
    """What ``ebur128`` read of a file (:func:`loudness_log`): its momentary blocks ``[(t, LUFS)]``, each
    frame's true peak ``[(t, dBTP)]`` (the highest of its channels) and the whole file's true peak."""

    blocks: list[tuple[float, float]]
    peaks: list[tuple[float, float]]
    true_peak: float | None


async def loudness_log(path: str) -> LoudnessLog:
    """The momentary loudness and true peaks of *path*'s sound (EBU R128, ``ebur128=peak=true``): one pass.

    The blocks as :func:`momentary_loudness` reads them; :class:`EncoderError` when the log holds none.
    """
    stderr = await ffmpeg(
        ["-nostats", "-i", path, "-map", "0:a:0", "-af", "ebur128=framelog=info:peak=true", "-f", "null", "-"],
        timeout=900,
    )
    blocks: list[tuple[float, float]] = []
    for at, value in _MOMENTARY_RE.findall(stderr):
        if float(at) < 0.4 - 0.01:
            continue
        lufs = float(value)
        blocks.append((float(at), lufs if math.isfinite(lufs) else -120.7))
    if not blocks:
        raise EncoderError(f"No momentary loudness was read from {Path(path).name}:\n{_tail(stderr)}")
    peaks: list[tuple[float, float]] = []
    for at, values in _FRAME_PEAK_RE.findall(stderr):
        finite = [float(v) for v in values.split() if math.isfinite(float(v))]
        if finite:
            peaks.append((float(at), max(finite)))
    found = _TRUE_PEAK_RE.findall(stderr)
    true_peak = float(found[-1]) if found and math.isfinite(float(found[-1])) else None
    return LoudnessLog(blocks, peaks, true_peak)


def gain_to_reference(loudness: float | None, *, limit: float = 18.0) -> float:
    """The gain in dB that brings *loudness* to :data:`REFERENCE_LUFS`, bounded."""
    if loudness is None:
        return 0.0
    return max(-limit, min(limit, REFERENCE_LUFS - loudness))


def clip_gain(loudness: float | None) -> float:
    """The gain in dB a clip's own sound of *loudness* takes: toward the voice, lifted :data:`CLIP_MAX_LIFT_DB` at most."""
    return min(gain_to_reference(loudness), CLIP_MAX_LIFT_DB)


async def condition_voice(source_path: str, out_path: str) -> None:
    """A narration as the mix takes it: :data:`VOICE_CHAIN`, at the reference loudness, 48 kHz stereo float."""
    chained = f"{out_path}.chain.wav"
    try:
        await ffmpeg(
            ["-i", source_path, "-af", VOICE_CHAIN, "-ar", "48000", "-ac", "2", "-c:a", "pcm_f32le", chained],
            timeout=300,
        )
        if (await probe(chained)).duration < 0.05:
            # All of it under the silence threshold: the clip as it came.
            await ffmpeg(["-i", source_path, "-ar", "48000", "-ac", "2", "-c:a", "pcm_f32le", chained], timeout=300)
        gain = gain_to_reference(await measure_loudness(chained))
        await ffmpeg(["-i", chained, "-af", f"volume={gain:.2f}dB", "-c:a", "pcm_f32le", out_path], timeout=300)
    finally:
        Path(chained).unlink(missing_ok=True)


# ── Music beds ────────────────────────────────────────────────────────────


def bed_segments(
    length: float, duration: float, *, start: float = 0.0, loop: bool = True, ending: str = "fade", crossfade: float = 2.0
) -> list[tuple[float, float]]:
    """The parts of a track, in source seconds, that crossfaded one into the next make a bed of *duration*.

    Longer than the video: from *start* (an ``ending="natural"`` keeps the
    track's last bars for the video's end, the middle edited out under a
    crossfade). Shorter: repeated whole, crossfaded (``loop``), the first
    repeat starting at *start*; a natural ending makes the last repeat end with
    the track, the cut taken off the first one. Without ``loop`` it ends where
    the track does, never repeated: :func:`bed_filter` fades it out on
    its own end and holds silence after.
    """
    if length <= 0.1:
        return [(0.0, max(0.1, length))]
    start = start if 0.0 <= start < length - 0.5 else 0.0
    usable = length - start
    if usable >= duration:
        if ending == "natural" and usable > duration + crossfade + 2.0:
            tail = min(12.0, duration / 3.0)
            return [(start, start + duration - tail + crossfade), (length - tail, length)]
        return [(start, start + duration)]
    if not loop:
        return [(start, length)]
    segments = [(start, length)]
    total = usable
    whole = length - crossfade
    while total < duration and whole > 0.5:
        segments.append((0.0, length))
        total += whole
    if ending == "natural" and total > duration and len(segments) > 1:
        cut = min(total - duration, max(0.0, segments[0][1] - segments[0][0] - crossfade - 0.5))
        segments[0] = (segments[0][0] + cut, segments[0][1])
    return segments


def _crossfade_of(segments: Sequence[tuple[float, float]], index: int, crossfade: float) -> float:
    """The crossfade :func:`bed_filter` joins segment *index* to the one before with: half the shorter at most."""
    shortest = min(segments[index - 1][1] - segments[index - 1][0], segments[index][1] - segments[index][0])
    return max(0.05, min(crossfade, shortest / 2.0))


def bed_crossfade(length: float, crossfade: float = 2.0) -> float:
    """The crossfade :func:`prepare_bed` joins the repeats of a *length* seconds track with: a quarter of it at most."""
    return min(crossfade, max(0.05, length / 4.0))


def bed_repeat(
    length: float, duration: float, *, start: float = 0.0, loop: bool = True, ending: str = "fade", crossfade: float = 2.0
) -> tuple[float, float, float] | None:
    """Where the bed :func:`prepare_bed` makes of a *length* seconds track first repeats itself.

    ``(lag, at, clean)``: the seconds between a moment of the first pass and the same moment of the second, which
    enters from the track's start one crossfade early (*length* less that crossfade, whatever *start*); the bed's
    second the repeat starts entering at (its first pass less that crossfade); the bed's first second whose repeat is
    heard past that crossfade. ``None`` when the bed plays the track once.
    """
    crossfade = bed_crossfade(length, crossfade)
    segments = bed_segments(length, duration, start=start, loop=loop, ending=ending, crossfade=crossfade)
    if len(segments) < 2 or segments[1][0] != 0.0:
        return None
    entry = _crossfade_of(segments, 1, crossfade)
    first = segments[0][1] - segments[0][0]
    return length - entry, first - entry, max(0.0, entry - segments[0][0])


def joined_seconds(segments: Sequence[tuple[float, float]], crossfade: float) -> float:
    """How long *segments* play once crossfaded one into the next (:func:`bed_filter`)."""
    played = sum(b - a for a, b in segments)
    return max(0.0, played - sum(_crossfade_of(segments, i, crossfade) for i in range(1, len(segments))))


def bed_filter(
    segments: Sequence[tuple[float, float]],
    *,
    duration: float,
    crossfade: float,
    gain_db: float,
    fade_in: float,
    fade_out: float,
) -> str:
    """The ``filter_complex`` from the track to ``[bed]``: the segments crossfaded, levelled, faded, exactly *duration*.

    Each segment reads an input of its own, ``[i:a]`` - the track given once per segment (:func:`prepare_bed`): an
    ``acrossfade`` fed by two branches of one ``asplit`` gives silence after its first segment on ffmpeg 7.1,
    so a looping bed went quiet after its first pass. The fade out ends where the music does: the video's end, or the
    end of a track played once that stops before it, silence held after it.
    """
    chains: list[str] = []

    def head(index: int) -> str:
        return f"[{index}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"

    if len(segments) == 1:
        a, b = segments[0]
        joined = f"{head(0)},atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS"
    else:
        for i, (a, b) in enumerate(segments):
            chains.append(f"{head(i)},atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS[g{i}]")
        current = "g0"
        for i in range(1, len(segments)):
            chains.append(f"[{current}][g{i}]acrossfade=d={_crossfade_of(segments, i, crossfade):.3f}:c1=qsin:c2=qsin[x{i}]")
            current = f"x{i}"
        joined = f"[{current}]anull"
    ends = min(duration, joined_seconds(segments, crossfade))
    fade_out = min(fade_out, ends)
    tail = (
        f",volume={gain_db:.2f}dB"
        + (f",afade=t=in:d={min(fade_in, ends):.3f}" if fade_in > 0 else "")
        + (f",afade=t=out:st={max(ends - fade_out, 0.0):.3f}:d={fade_out:.3f}" if fade_out > 0 else "")
        + f",apad=whole_dur={duration:.3f},atrim=duration={duration:.3f}[bed]"
    )
    chains.append(joined + tail)
    return ";".join(chains)


async def prepare_bed(
    source_path: str,
    out_path: str,
    *,
    duration: float,
    start: float = 0.0,
    loop: bool = True,
    ending: str = "fade",
    fade_in: float = 1.0,
    fade_out: float = 2.5,
    crossfade: float = 2.0,
) -> None:
    """The bed a video plays (48 kHz stereo float WAV): *duration* long, at :data:`REFERENCE_LUFS`."""
    info = await probe(source_path)
    crossfade = bed_crossfade(info.duration, crossfade)
    segments = bed_segments(info.duration, duration, start=start, loop=loop, ending=ending, crossfade=crossfade)
    gain = gain_to_reference(await measure_loudness(source_path))
    graph = bed_filter(
        segments, duration=duration, crossfade=crossfade, gain_db=gain, fade_in=fade_in, fade_out=fade_out
    )
    # The track once per segment: each is an input of the graph's own (:func:`bed_filter`).
    inputs = [arg for _segment in segments for arg in ("-i", source_path)]
    await ffmpeg(
        [*inputs, "-filter_complex", graph, "-map", "[bed]", "-c:a", "pcm_f32le", "-ar", "48000", out_path],
        timeout=900,
    )


async def decode_pcm(path: str, out_path: str, *, rate: int, channels: int) -> None:
    """*path*'s first audio stream as raw 32-bit float samples, interleaved, at *rate* and *channels*.

    At the stream's own rate and channels nothing is resampled or mixed: the
    samples are the decoder's, as every decode of the file gives them.
    """
    await ffmpeg(
        ["-i", path, "-map", "0:a:0", "-ar", str(rate), "-ac", str(channels), "-c:a", "pcm_f32le", "-f", "f32le", out_path],
        timeout=600,
    )


async def encode_pcm(raw_path: str, out_path: str, *, rate: int, channels: int, codec: str = "pcm_f32le") -> None:
    """Raw interleaved 32-bit float samples (:func:`decode_pcm`) as a WAV whose samples are *codec*."""
    await ffmpeg(
        ["-f", "f32le", "-ar", str(rate), "-ac", str(channels), "-i", raw_path, "-c:a", codec, out_path],
        timeout=600,
    )


async def encode_preview_bed(bed_path: str, out_path: str) -> None:
    """A prepared bed for a browser player: AAC, its peaks held under full scale."""
    await ffmpeg(
        ["-i", bed_path, "-af", "alimiter=limit=0.95:level=false", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out_path],
        timeout=900,
    )


def merge_windows(
    windows: Sequence[tuple[float, float]], *, bridge: float = DUCK_BRIDGE
) -> list[tuple[float, float]]:
    """*windows* in time order, those closer than *bridge* joined: the music stays down between two sentences."""
    merged: list[tuple[float, float]] = []
    for start, end in sorted((float(a), float(b)) for a, b in windows if b > a):
        if merged and start - merged[-1][1] < bridge:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _duck_gain(t: float, merged: Sequence[tuple[float, float]], dip: float) -> float:
    """The envelope :func:`duck_expression` writes for ffmpeg, at *t*."""
    down = 0.0
    for start, end in merged:
        down += min(
            min(max((t - (start - DUCK_LEAD)) / DUCK_LEAD, 0.0), 1.0),
            min(max(((end + DUCK_RELEASE) - t) / DUCK_RELEASE, 0.0), 1.0),
        )
    return 1.0 - dip * min(max(down, 0.0), 1.0)


def duck_points(
    windows: Sequence[tuple[float, float]], *, depth_db: float, duration: float
) -> list[tuple[float, float]]:
    """The ducking envelope as ``[(seconds, gain)]``, linear between the points: the editor's preview of it.

    The points are where :func:`duck_expression` bends, its value read there,
    so the preview and the render dip at the same instants by the same amount.
    """
    merged = merge_windows(windows)
    dip = 1.0 - 10 ** (-max(0.0, depth_db) / 20.0)
    times = {0.0, duration}
    for start, end in merged:
        times.update((start - DUCK_LEAD, start, end, end + DUCK_RELEASE))
    return [
        (round(t, 3), round(_duck_gain(t, merged, dip), 4)) for t in sorted(times) if 0.0 <= t <= duration
    ]


def duck_expression(windows: Sequence[tuple[float, float]], *, depth_db: float) -> str | None:
    """The ffmpeg ``volume`` expression of the envelope, of ``t``; ``None`` when nothing ducks it."""
    merged = merge_windows(windows)
    if not merged or depth_db <= 0:
        return None
    dip = 1.0 - 10 ** (-depth_db / 20.0)
    terms = [
        f"min(clip((t-{start - DUCK_LEAD:.3f})/{DUCK_LEAD},0,1),clip(({end + DUCK_RELEASE:.3f}-t)/{DUCK_RELEASE},0,1))"
        for start, end in merged
    ]
    return f"1-{dip:.4f}*clip({'+'.join(terms)},0,1)"


def clip_duck_windows(track: ClipTrack) -> list[tuple[float, float]]:
    """The narration over *track*, in the clip's own seconds: the windows its envelope bends on."""
    return [
        (start - track.start, end - track.start)
        for start, end in track.windows
        if end + DUCK_RELEASE > track.start and start - DUCK_LEAD < track.start + track.duration
    ]


def _atempo_chain(speed: float) -> str:
    """``atempo`` takes 0.5-2.0 per filter; a larger change is a chain."""
    parts: list[str] = []
    remaining = speed
    while remaining > 2.0:
        parts.append("atempo=2.0")
        remaining /= 2.0
    while remaining < 0.5:
        parts.append("atempo=0.5")
        remaining /= 0.5
    if abs(remaining - 1.0) > 1e-3:
        parts.append(f"atempo={remaining:.4f}")
    return ",".join(parts)


def build_mix_filter(
    *, duration: float, voice: VoiceTrack | None, clips: Sequence[ClipTrack], music: MusicTrack | None
) -> tuple[list[str], str]:
    """``(input paths, filter_complex)`` of the final mix; the output pad is ``[mix]``.

    The voice comes in as its stem, already placed and at the mix rate
    (``stitch``): it is neither delayed nor resampled here, only spread to
    both channels.
    """
    inputs: list[str] = []
    chains: list[str] = []
    other_labels: list[str] = []
    voice_bus = None
    if voice is not None:
        inputs.append(voice.path)
        chains.append(f"[0:a]pan=stereo|c0=c0|c1=c0,volume={voice.volume:.3f}[voice]")
        voice_bus = "voice"
    for track in clips:
        index = len(inputs)
        inputs.append(track.path)
        delay = max(0, round(track.start * 1000))
        tempo = _atempo_chain(track.speed)
        label = f"c{index}"
        fade_in = max(0.01, min(track.fade_in, track.duration / 2))
        fade_out = max(0.01, min(track.fade_out, track.duration / 2))
        level = (f"volume={track.gain_db:.2f}dB," if abs(track.gain_db) >= 0.01 else "") + f"volume={track.volume:.3f}"
        # Under the narration like the music, on the clip's own clock (before its delay).
        expression = duck_expression(clip_duck_windows(track), depth_db=CLIP_DUCK_DB)
        duck = f",asetnsamples=n=240:p=0,volume='{expression}':eval=frame" if expression else ""
        chains.append(
            f"[{index}:a]atrim=start={track.trim_start:.4f}:duration={track.duration * track.speed:.4f},"
            f"asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo"
            + (f",{tempo}" if tempo else "")
            + f",{level},afade=t=in:d={fade_in:.3f},"
            f"afade=t=out:st={max(track.duration - fade_out, 0):.3f}:d={fade_out:.3f}{duck},"
            f"adelay={delay}|{delay}[{label}]"
        )
        other_labels.append(label)
    if music is not None:
        index = len(inputs)
        inputs.append(music.path)
        # The envelope is computed from where the narration is, not heard from
        # it: the music is down before the first word, never pumping after it.
        expression = duck_expression(music.windows, depth_db=music.duck_db)
        level = f"volume={music.volume:.3f}"
        if expression:
            level = f"asetnsamples=n=240:p=0,volume='{music.volume:.3f}*({expression})':eval=frame"
        chains.append(
            f"[{index}:a]aresample=48000,aformat=channel_layouts=stereo,{level},"
            f"apad=whole_dur={duration:.3f},atrim=duration={duration:.3f}[music]"
        )
        other_labels.append("music")
    final_labels = ([voice_bus] if voice_bus else []) + other_labels
    if not final_labels:
        return [], f"anullsrc=r=48000:cl=stereo,atrim=duration={duration:.3f}[mix]"
    mixed = (
        "".join(f"[{label}]" for label in final_labels)
        + f"amix=inputs={len(final_labels)}:normalize=0:dropout_transition=0"
        if len(final_labels) > 1
        else f"[{final_labels[0]}]anull"
    )
    chains.append(f"{mixed},aresample=48000,apad=whole_dur={duration:.3f},atrim=duration={duration:.3f}[mix]")
    return inputs, ";".join(chains)


_LOUDNORM_JSON_RE = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.S)


async def loudnorm_measure(path: str) -> dict | None:
    """The first pass of the master's loudness normalisation; ``None`` for silence."""
    stderr = await ffmpeg(
        [
            "-nostats",
            "-i",
            path,
            "-af",
            f"loudnorm=I={MASTER_LUFS}:TP={MASTER_TRUE_PEAK}:LRA={MASTER_LRA}:print_format=json",
            "-f",
            "null",
            "-",
        ],
        timeout=900,
    )
    found = _LOUDNORM_JSON_RE.findall(stderr)
    if not found:
        return None
    try:
        stats = json.loads(found[-1])
        if float(stats["input_i"]) <= -70.0:
            return None
        return stats
    except (ValueError, KeyError, TypeError):
        return None


def master_filter(stats: dict | None) -> str:
    """The second pass: a linear gain to :data:`MASTER_LUFS` with the true peak held (the measured values)."""
    if not stats:
        return "anull"
    return (
        f"loudnorm=I={MASTER_LUFS}:TP={MASTER_TRUE_PEAK}:LRA={MASTER_LRA}"
        f":measured_I={float(stats['input_i']):.2f}:measured_TP={float(stats['input_tp']):.2f}"
        f":measured_LRA={float(stats['input_lra']):.2f}:measured_thresh={float(stats['input_thresh']):.2f}"
        f":offset={float(stats['target_offset']):.2f}:linear=true,aresample=48000"
    )


async def mix_audio(
    *,
    out_path: str,
    duration: float,
    voice: VoiceTrack | None,
    clips: Sequence[ClipTrack],
    music: MusicTrack | None,
    bitrate: str = "256k",
    span: tuple[float, float] | None = None,
) -> None:
    """The soundtrack of a render, AAC at 48 kHz, exactly *duration* long, at :data:`MASTER_LUFS`.

    Two passes: the mix to a float file, then its loudness measured and
    brought to the target with one linear gain (a single dynamic pass pumps).
    *span* ``(start, seconds)`` keeps only that part of the master (a scene
    preview's window), cut sample-exact before the one AAC encode, so a
    preview sounds exactly as the same seconds of a whole render.
    """
    inputs, graph = build_mix_filter(duration=duration, voice=voice, clips=clips, music=music)
    args: list[str] = []
    for path in inputs:
        args += ["-i", path]
    if not inputs:
        args += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        graph = f"[0:a]atrim=duration={duration:.3f}[mix]"
    cut = f"atrim=duration={duration:.3f}"
    if span is not None:
        cut += f",atrim=start={max(span[0], 0.0):.4f}:duration={max(span[1], 0.0):.4f},asetpts=PTS-STARTPTS"
    premaster = f"{out_path}.premaster.wav"
    try:
        await ffmpeg([*args, "-filter_complex", graph, "-map", "[mix]", "-c:a", "pcm_f32le", "-ar", "48000", premaster])
        stats = await loudnorm_measure(premaster)
        await ffmpeg(
            [
                "-i",
                premaster,
                "-af",
                f"{master_filter(stats)},{cut}",
                "-c:a",
                "aac",
                "-b:a",
                bitrate,
                "-ar",
                "48000",
                out_path,
            ]
        )
    finally:
        Path(premaster).unlink(missing_ok=True)


async def encode_voice_like_mix(
    stem_path: str, out_path: str, *, bitrate: str = "256k", span: tuple[float, float] | None = None
) -> None:
    """The voice stem through the final mix's encode: AAC at 48 kHz and the mix's bitrate, cut to *span*.

    Written to an MP4 container (*out_path* ``.m4a``), so what is heard back
    is demuxed and decoded exactly as the render's own sound track is.
    """
    graph = "aresample=48000"
    if span is not None:
        graph += f",atrim=start={max(span[0], 0.0):.4f}:duration={max(span[1], 0.0):.4f},asetpts=PTS-STARTPTS"
    await ffmpeg(["-i", stem_path, "-af", graph, "-c:a", "aac", "-b:a", bitrate, "-ar", "48000", out_path], timeout=600)


async def demux_audio(video_path: str, out_path: str) -> MediaInfo:
    """The sound track of a muxed video as it is (its AAC, copied, not decoded), and what it is."""
    await ffmpeg(["-i", video_path, "-map", "0:a:0", "-c:a", "copy", "-vn", out_path], timeout=300)
    return await probe(out_path)


async def decode_mono(path: str, *, rate: int) -> bytes:
    """The first sound track of *path* decoded as mono 32-bit float samples at *rate* (little-endian)."""
    stdout, _ = await run(
        ffmpeg_path(),
        ["-hide_banner", "-nostdin", "-v", "error", "-i", path, "-map", "0:a:0", "-ac", "1", "-ar", str(rate), "-f", "f32le", "-"],
        timeout=900,
    )
    return stdout


async def iter_gray_frames(
    video_path: str, *, width: int, height: int, fps: float, start: float = 0.0
) -> AsyncIterator[bytes]:
    """The frames of *video_path* at *fps* from *start* seconds, each ``width x height`` pixels of 8-bit grey
    (area-averaged), streamed. The seek is decoded, so the first frame is the one at *start*."""
    seek = ["-ss", f"{start:.6f}"] if start > 0 else []
    process = await asyncio.create_subprocess_exec(
        ffmpeg_path(),
        *["-hide_banner", "-nostdin", "-v", "error", "-threads", "1", *seek, "-i", video_path, "-map", "0:v:0"],
        *["-vf", f"fps={fps:.6f},scale={width}:{height}:flags=area,format=gray", "-f", "rawvideo", "-"],
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    errors = asyncio.create_task(process.stderr.read())
    size = width * height
    try:
        while True:
            try:
                yield await process.stdout.readexactly(size)
            except asyncio.IncompleteReadError:
                break
        await process.wait()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        text = (await errors).decode("utf-8", errors="replace")
    if process.returncode != 0:
        raise EncoderError(f"ffmpeg failed decoding (exit {process.returncode}):\n{_tail(text)}")


async def remux(*, source_path: str, out_path: str) -> None:
    """*source_path*'s streams copied into *out_path* as they are, its index at the front: a kept video encoded into a
    file of its own (``kept_previews.serve``), no frame decoded."""
    await ffmpeg(["-i", source_path, "-map", "0", "-c", "copy", "-movflags", "+faststart", out_path])


async def mux(*, video_path: str, audio_path: str, out_path: str) -> None:
    await ffmpeg(
        [
            "-i",
            video_path,
            "-i",
            audio_path,
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "copy",
            "-shortest",
            "-movflags",
            "+faststart",
            out_path,
        ]
    )


async def without_sound(video_path: str, out_path: str) -> None:
    """*video_path* with its sound tracks dropped, the picture copied as it is."""
    await ffmpeg(["-i", video_path, "-map", "0:v", "-c:v", "copy", "-an", "-movflags", "+faststart", out_path], timeout=300)


async def iter_rgb_frames(
    video_path: str, *, width: int, height: int, resize: bool = False, start: float = 0.0
) -> AsyncIterator[bytes]:
    """The frames of *video_path*, decoded forward, each ``width x height`` pixels of RGB (``rgb24``).

    *resize* scales each frame to that size (area averaging) instead of reading
    it at the video's own, which *width* and *height* must then be. *start* (seconds)
    seeks the input first: the seek is decoded, so the first frame yielded is the
    first one at or past *start*.
    """
    scaled = ["-vf", f"scale={width}:{height}:flags=area"] if resize else []
    seek = ["-ss", f"{start:.6f}"] if start > 0 else []
    process = await asyncio.create_subprocess_exec(
        ffmpeg_path(),
        *["-hide_banner", "-nostdin", "-v", "error", *seek, "-i", video_path, "-map", "0:v:0", *scaled],
        *["-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    errors = asyncio.create_task(process.stderr.read())
    size = width * height * 3
    try:
        while True:
            try:
                yield await process.stdout.readexactly(size)
            except asyncio.IncompleteReadError:
                break
        await process.wait()
    finally:
        if process.returncode is None:
            process.kill()
            # A reader that stopped early left the pipe paused, full: it would never see its end, and
            # the process is only reaped once every pipe has closed. Drained, it closes.
            with contextlib.suppress(OSError, ValueError):
                await process.stdout.read()
            await process.wait()
        text = (await errors).decode("utf-8", errors="replace")
    if process.returncode != 0:
        raise EncoderError(f"ffmpeg failed decoding (exit {process.returncode}):\n{_tail(text)}")


async def transform_frames(
    video_path: str,
    out_path: str,
    transform: Callable[[int, bytes], bytes],
    *,
    width: int,
    height: int,
    fps: float,
    crf: int = 16,
    preset: str = "medium",
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> int:
    """*video_path* re-encoded with every frame passed through *transform* ``(index, rgb24) -> rgb24``.

    Streamed (never the whole video in memory), *transform* run in a thread
    so the event loop keeps turning; the sound copied as it is. Returns how
    many frames were written.
    """
    process = await asyncio.create_subprocess_exec(
        ffmpeg_path(),
        *["-hide_banner", "-nostdin", "-y", "-v", "error"],
        *["-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}", "-framerate", f"{fps:.6f}", "-i", "-"],
        *["-i", video_path, "-map", "0:v:0", "-map", "1:a?", "-c:a", "copy", "-vf", _TO_BT709],
        *["-c:v", "libx264", "-threads", str(cpu_budget()), "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p"],
        *_COLOR_TAGS,
        *["-movflags", "+faststart", out_path],
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    errors = asyncio.create_task(process.stderr.read())
    written = 0
    try:
        async with asyncio.timeout(timeout):
            async for frame in iter_rgb_frames(video_path, width=width, height=height):
                process.stdin.write(await asyncio.to_thread(transform, written, frame))
                await process.stdin.drain()
                written += 1
            process.stdin.close()
            await process.wait()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        text = (await errors).decode("utf-8", errors="replace")
    if process.returncode != 0:
        raise EncoderError(f"ffmpeg failed encoding (exit {process.returncode}):\n{_tail(text)}")
    return written


async def rescale(
    source_path: str,
    out_path: str,
    *,
    width: int,
    height: int,
    fps: int,
    crf: int = 16,
    preset: str = "medium",
    on_wait: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """*source_path* at *width* x *height* and *fps* frames a second (a figure drawn sharper): no page opened.

    A drawing at a higher scale and a multiple of the frame rate holds every frame the smaller one would draw.
    """
    await ffmpeg(
        [
            "-i",
            source_path,
            "-an",
            "-vf",
            f"fps={fps},scale={width}:{height}:flags=lanczos,format=yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            preset,
            "-crf",
            str(crf),
            "-movflags",
            "+faststart",
            out_path,
        ],
        timeout=1800,
        on_wait=on_wait,
    )


async def concat(parts: Sequence[str], *, out_path: str, workdir: str) -> None:
    """Join encoded chunks of one render (same codec settings) without re-encoding."""
    if len(parts) == 1:
        shutil.copyfile(parts[0], out_path)
        return
    list_path = Path(workdir) / "chunks.ffconcat"
    list_path.write_text(
        "ffconcat version 1.0\n" + "".join(f"file '{Path(p).name}'\n" for p in parts), encoding="utf-8"
    )
    await ffmpeg(
        ["-f", "concat", "-safe", "0", "-i", list_path.name, "-c", "copy", "-movflags", "+faststart", out_path],
        cwd=workdir,
    )


async def extract_frame(video_path: str, *, at: float, out_path: str, width: int | None = None) -> None:
    args = ["-ss", f"{max(at, 0.0):.3f}", "-i", video_path, "-frames:v", "1"]
    if width:
        args += ["-vf", f"scale={width}:-2:flags=lanczos"]
    args += ["-q:v", "2", out_path]
    await ffmpeg(args, timeout=120)


#: The most terms one ``select`` expression of :func:`extract_frames` holds: ffmpeg's expression parser
#: refuses a sum of about 130 ``eq(n,k)`` terms (exit 244, "Error while parsing expression"), so a pass picks at most
#: this many ranges of consecutive frames and the next pass seeks again.
MAX_SELECT_TERMS = 32
#: The most frames one pass of :func:`extract_frames` writes: its output pattern numbers them on three digits.
MAX_PASS_FRAMES = 900


def select_runs(
    frames: Sequence[int], *, rate: float, max_terms: int = MAX_SELECT_TERMS, max_frames: int = MAX_PASS_FRAMES
) -> list[list[int]]:
    """*frames* (0-based indices) grouped into the passes of :func:`extract_frames`: sorted and deduplicated, a pass
    holding frames within *rate* frames (a second) of the one before, never more than *max_terms* ranges of
    consecutive frames (:func:`select_expression`) nor *max_frames* frames, so its ``select`` expression stays short
    and its output numbering fits whatever the count."""
    runs: list[list[int]] = []
    terms = 0
    for frame in sorted(set(int(n) for n in frames if n >= 0)):
        if runs and frame - runs[-1][-1] <= rate:
            adds = 0 if frame == runs[-1][-1] + 1 else 1
            if terms + adds <= max_terms and len(runs[-1]) < max_frames:
                runs[-1].append(frame)
                terms += adds
                continue
        runs.append([frame])
        terms = 1
    return runs


def select_expression(run: Sequence[int]) -> str:
    """The ``select`` expression picking the frames *run* (sorted indices) counted from its first: consecutive frames
    as one ``between(n,a,b)``, a lone frame as ``eq(n,a)``; commas escaped for the filter graph."""
    first = run[0]
    ranges: list[list[int]] = []
    for frame in run:
        if ranges and frame == ranges[-1][1] + 1:
            ranges[-1][1] = frame
        else:
            ranges.append([frame, frame])
    return "+".join(
        f"eq(n\\,{a - first})" if a == b else f"between(n\\,{a - first}\\,{b - first})" for a, b in ranges
    )


async def extract_frames(video_path: str, frames: Sequence[int], *, fps: float, out_dir: str) -> dict[int, str]:
    """Frames *frames* of *video_path* (0-based, at *fps*) as lossless PNGs in *out_dir*, by index: the frames
    within a second of each other read in one pass from a seek half a frame before the first, each picked by its
    number, at most :data:`MAX_SELECT_TERMS` ranges a pass (:func:`select_runs`); a frame past the video's end
    is left out. The PNGs a pass writes are cleared before it runs: a frame an earlier attempt wrote there is
    never read for one this pass did not yield."""
    if not frames:
        return {}
    rate = max(float(fps), 1.0)
    runs = select_runs(frames, rate=rate)
    out: dict[int, str] = {}
    for run in runs:
        first = run[0]
        picks = select_expression(run)
        pattern = Path(out_dir) / f"frame-{first:07d}-%03d.png"
        for stale in Path(out_dir).glob(f"frame-{first:07d}-*.png"):
            stale.unlink(missing_ok=True)
        await ffmpeg(
            [
                "-ss",
                f"{max(0.0, (first - 0.5) / rate):.4f}",
                # Decoded on two threads: a 3200x1800 frame per decoding thread is what a pass holds.
                "-threads",
                "2",
                "-i",
                video_path,
                "-vf",
                f"select='{picks}'",
                "-fps_mode",
                "passthrough",
                "-frames:v",
                str(len(run)),
                str(pattern),
            ],
            timeout=300,
        )
        for count, frame in enumerate(run, start=1):
            path = Path(out_dir) / f"frame-{first:07d}-{count:03d}.png"
            if path.is_file():
                out[frame] = str(path)
    return out


async def extract_still(
    video_path: str, *, at: float | None, out_path: str, max_side: int, crop_to: tuple[int, int] | None = None
) -> None:
    """One frame of *video_path* at *at* seconds, else its last, no side larger than *max_side*.

    *crop_to* ``(w, h)``: first cut to that aspect ratio around the centre, then scaled.
    """
    fit = f"scale='min({max_side},iw)':'min({max_side},ih)':force_original_aspect_ratio=decrease:flags=lanczos"
    if crop_to:
        w, h = crop_to
        fit = f"crop='min(iw,ih*{w}/{h})':'min(ih,iw*{h}/{w})',{fit}"
    if at is None:
        # A seek to the duration can land past the last frame: decode the last second, keep its final frame.
        args = ["-sseof", "-1", "-i", video_path, "-vf", fit, "-update", "1"]
    else:
        args = ["-ss", f"{max(at, 0.0):.3f}", "-i", video_path, "-frames:v", "1", "-vf", fit]
    args += ["-q:v", "2", out_path]
    await ffmpeg(args, timeout=120)


async def gray_frames(
    video_path: str, *, start: float, seconds: float, fps: int, size: tuple[int, int], out_path: str
) -> list[bytes]:
    """*seconds* of *video_path* from *start*, *fps* frames a second, each *size* pixels of 8-bit gray."""
    width, height = size
    args = ["-ss", f"{max(start, 0.0):.3f}", "-i", video_path, "-t", f"{max(seconds, 0.0):.3f}"]
    args += ["-vf", f"fps={fps},scale={width}:{height}:flags=area,format=gray", "-f", "rawvideo", out_path]
    await ffmpeg(args, timeout=120)
    data = Path(out_path).read_bytes()
    step = width * height
    return [data[i : i + step] for i in range(0, len(data) - step + 1, step)]
