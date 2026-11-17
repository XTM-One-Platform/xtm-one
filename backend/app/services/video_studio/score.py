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

"""Built-in scores: royalty-free music composed for the video it plays under.

A built-in track is a :class:`Style` - tempo, key, chord progressions,
instruments, drum patterns, transitions - and a score is that style rendered
for ONE video: synthesised by the platform (numpy and scipy, no sample, no
third-party audio, nothing to license) at the video's exact length.

The arrangement is computed from the video, never prompted (invariant 16):

* the tempo stays the style's; the bars are counted so the final chord lands
  on the last scene and rings out on the last frame;
* each scene change starts a section on the nearest bar line, and what plays
  follows what the scene is: a title opens sparse, a statement or an AI shot
  without narration lifts, a narrated scene leaves the voice room (fewer
  notes, softer keys, no lead);
* a riser or a swell leads into a lift, an impact marks the peak, the last
  bars cadence home.

The render is deterministic - a seed from the style, the length and the
scenes - so the same video gets the same music, the preview and the render
agree and the result caches under the digest of what decides it
(:func:`score_digest`). It runs in bounded chunks, the ringing notes, the
filters' state and the reverb's tail carried from one chunk to the next: a
20-minute video needs no more memory than a short one.
"""

from __future__ import annotations

import functools
import hashlib
import itertools
import math
import wave
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np
from scipy import signal

SAMPLE_RATE = 48_000
_SR = SAMPLE_RATE
#: Samples rendered and written at a time.
_CHUNK = 16 * _SR
#: The longest a sound rings past its start (a pad chord and its release, the
#: final chord): what a chunk carries into the next.
_RING = 12 * _SR
#: The shortest the final chord rings, in seconds.
_MIN_TAIL = 1.6

Chord = tuple[int, str]

#: Chord qualities as semitones above the root.
_QUALITIES: dict[str, tuple[int, ...]] = {
    "maj": (0, 4, 7),
    "min": (0, 3, 7),
    "sus2": (0, 2, 7),
    "sus4": (0, 5, 7),
    "add9": (0, 4, 7, 14),
    "madd9": (0, 3, 7, 14),
    "maj7": (0, 4, 7, 11),
    "min7": (0, 3, 7, 10),
    "dom7": (0, 4, 7, 10),
    "maj9": (0, 4, 7, 11, 14),
    "min9": (0, 3, 7, 10, 14),
    "six": (0, 4, 7, 9),
}

#: What a scene asks of the music, 1 (sparse) to 3 (full), before narration.
_SCENE_ENERGY = {"title": 1, "statement": 3, "clip": 3, "image": 2, "slide": 2, "recording": 2, "outro": 3}


@dataclass(frozen=True)
class Style:
    """A built-in track: what it is for and how it is played."""

    id: str
    name: str
    mood: str
    genre: str
    description: str
    #: The kinds of video it suits (the video templates' purposes).
    purposes: tuple[str, ...]
    bpm: float
    #: The key's root as a MIDI note in the bass octave.
    root: int
    #: Progressions as (semitones above the key root, quality).
    main: tuple[Chord, ...]
    lift: tuple[Chord, ...]
    #: The bars before the final chord, one chord a bar.
    cadence: tuple[Chord, ...]
    final: Chord
    bars_per_chord: int
    #: ``electronic`` | ``lofi`` | ``cinematic`` | ``pulse`` | ``house`` | ``none``.
    kit: str
    #: ``warm`` | ``strings`` | ``dark`` | ``glass``.
    pad: str
    #: ``epiano`` | ``piano`` | ``pluck`` | ``stab``.
    keys: str
    #: ``comp`` | ``arp8`` | ``arp16`` | ``ostinato`` | ``offbeat`` | ``broken``.
    keys_pattern: str
    #: ``sub`` | ``pulse16`` | ``pluck`` | ``offbeat`` | ``drone``.
    bass: str
    #: A sparse bell motif in the open sections.
    lead: bool = False
    swing: float = 0.0
    reverb: float = 0.3
    room_seconds: float = 2.4
    #: Sidechain depth: the pads and bass breathe with the kick.
    pump: float = 0.0
    risers: bool = False
    impacts: bool = False
    crackle: bool = False
    #: Scales the pads' filter: below 1 darker, above brighter.
    brightness: float = 1.0
    gain: float = 1.0

    def summary(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "mood": self.mood,
            "genre": self.genre,
            "bpm": round(self.bpm),
            "description": self.description,
            "purposes": list(self.purposes),
        }


STYLES: tuple[Style, ...] = (
    Style(
        id="bright-horizon",
        name="Bright Horizon",
        mood="uplifting",
        genre="corporate electronic",
        description="Optimistic and clean: warm pads, a plucked arpeggio, a light four-on-the-floor in the lifts. "
        "Product launches, demos and explainers that should feel confident.",
        purposes=("launch", "product_demo", "explainer"),
        bpm=112,
        root=38,  # D
        main=((0, "add9"), (7, "maj"), (9, "min7"), (5, "maj9")),
        lift=((9, "min7"), (5, "maj7"), (0, "add9"), (7, "sus4")),
        cadence=((5, "maj9"), (7, "sus4")),
        final=(0, "add9"),
        bars_per_chord=1,
        kit="electronic",
        pad="warm",
        keys="pluck",
        keys_pattern="arp16",
        bass="sub",
        lead=True,
        reverb=0.28,
        room_seconds=2.2,
        pump=0.32,
        risers=True,
        impacts=False,
        brightness=1.1,
    ),
    Style(
        id="deep-focus",
        name="Deep Focus",
        mood="calm",
        genre="ambient tech",
        description="Calm and spacious: soft electric piano chords over a deep pad, a gentle pulse. "
        "Leaves the narration all the room: product walkthroughs and long explanations.",
        purposes=("product_demo", "explainer"),
        bpm=86,
        root=45,  # A
        main=((0, "min9"), (8, "maj7"), (3, "maj7"), (10, "add9")),
        lift=((5, "min9"), (8, "maj7"), (3, "add9"), (10, "six")),
        cadence=((8, "maj7"), (10, "add9")),
        final=(0, "min9"),
        bars_per_chord=1,
        kit="lofi",
        pad="glass",
        keys="epiano",
        keys_pattern="comp",
        bass="sub",
        reverb=0.36,
        room_seconds=3.0,
        brightness=0.85,
    ),
    Style(
        id="signal",
        name="Signal",
        mood="tense",
        genre="dark minimal tech",
        description="Dark and focused: a filtered sixteenth-note bass pulse, a low drone, ticking hats and impacts "
        "on the turns. Threat briefings, security research, incident stories.",
        purposes=("explainer", "event", "brand_film"),
        bpm=100,
        root=38,  # D
        main=((0, "min"), (8, "maj"), (5, "min"), (7, "maj")),
        lift=((0, "min"), (10, "maj"), (8, "maj"), (7, "sus4")),
        cadence=((8, "maj"), (7, "sus4")),
        final=(0, "min"),
        bars_per_chord=2,
        kit="pulse",
        pad="dark",
        keys="pluck",
        keys_pattern="arp8",
        bass="pulse16",
        lead=True,
        reverb=0.3,
        room_seconds=2.8,
        pump=0.25,
        risers=True,
        impacts=True,
        brightness=0.75,
    ),
    Style(
        id="ascent",
        name="Ascent",
        mood="inspiring",
        genre="cinematic",
        description="A cinematic build: ensemble strings, a piano ostinato, taiko drums and a big hit at the peak. "
        "Brand films, keynote openers and launch films that need scale.",
        purposes=("brand_film", "launch", "event"),
        bpm=92,
        root=45,  # A
        main=((0, "min"), (8, "maj"), (3, "maj"), (10, "maj")),
        lift=((8, "maj"), (3, "maj"), (10, "maj"), (5, "min")),
        cadence=((8, "maj"), (10, "maj")),
        final=(0, "min"),
        bars_per_chord=2,
        kit="cinematic",
        pad="strings",
        keys="piano",
        keys_pattern="ostinato",
        bass="drone",
        reverb=0.4,
        room_seconds=3.4,
        risers=True,
        impacts=True,
        brightness=0.95,
        gain=0.95,
    ),
    Style(
        id="late-notes",
        name="Late Notes",
        mood="relaxed",
        genre="lo-fi",
        description="Warm and laid back: jazzy electric piano sevenths, a swung dusty beat and vinyl crackle. "
        "Social clips, tips and friendly explainers.",
        purposes=("social", "explainer"),
        bpm=80,
        root=38,  # D (ii-V-I in C)
        main=((0, "min7"), (5, "dom7"), (10, "maj7"), (7, "min7")),
        lift=((3, "maj7"), (0, "min7"), (8, "maj7"), (5, "dom7")),
        cadence=((0, "min7"), (5, "dom7")),
        final=(10, "maj9"),
        bars_per_chord=1,
        kit="lofi",
        pad="glass",
        keys="epiano",
        keys_pattern="comp",
        bass="pluck",
        swing=0.22,
        reverb=0.26,
        room_seconds=1.8,
        crackle=True,
        brightness=0.7,
    ),
    Style(
        id="momentum",
        name="Momentum",
        mood="energetic",
        genre="upbeat pop electronic",
        description="Punchy and bright: offbeat chord stabs, a bouncing bass, claps and a driving kick. "
        "Social teasers, event recaps and launch countdowns.",
        purposes=("social", "event", "launch"),
        bpm=124,
        root=43,  # G
        main=((9, "min"), (5, "maj"), (0, "maj"), (7, "maj")),
        lift=((5, "maj"), (7, "maj"), (9, "min"), (4, "min")),
        cadence=((5, "maj"), (7, "sus4")),
        final=(0, "add9"),
        bars_per_chord=1,
        kit="house",
        pad="warm",
        keys="stab",
        keys_pattern="offbeat",
        bass="offbeat",
        lead=True,
        reverb=0.22,
        room_seconds=1.7,
        pump=0.42,
        risers=True,
        brightness=1.2,
    ),
    Style(
        id="quiet-light",
        name="Quiet Light",
        mood="reflective",
        genre="minimal piano",
        description="Intimate and human: a felt piano's broken chords over a faint pad, no drums. "
        "Customer stories, testimonials and heartfelt brand moments.",
        purposes=("brand_film", "explainer", "product_demo"),
        bpm=72,
        root=40,  # E
        main=((0, "add9"), (9, "min7"), (5, "maj7"), (7, "sus4")),
        lift=((5, "maj7"), (0, "add9"), (9, "min7"), (7, "maj")),
        cadence=((5, "maj7"), (7, "sus4")),
        final=(0, "add9"),
        bars_per_chord=2,
        kit="none",
        pad="glass",
        keys="piano",
        keys_pattern="broken",
        bass="drone",
        lead=False,
        reverb=0.42,
        room_seconds=3.2,
        brightness=0.8,
        gain=1.05,
    ),
)
_BY_ID = {style.id: style for style in STYLES}


def get_style(track_id: str | None) -> Style | None:
    return _BY_ID.get((track_id or "").strip().lower())


def track_ids() -> tuple[str, ...]:
    return tuple(_BY_ID)


# ── What the score plays under ────────────────────────────────────────────


@dataclass(frozen=True)
class Cue:
    """A scene the score plays under: where it starts, what it is, whether someone speaks over it."""

    start: float
    kind: str
    narrated: bool = False


@dataclass(frozen=True)
class _Section:
    start: int  # bar
    end: int  # bar, exclusive
    energy: int  # 1..3
    #: Someone speaks over it: the arrangement leaves the voice room.
    room: bool
    variant: int


@dataclass(frozen=True)
class _Plan:
    bar: float
    bars: int
    #: Seconds of the final chord's ring-out after the last bar.
    tail: float
    duration: float
    sections: tuple[_Section, ...]
    chords: tuple[Chord, ...]  # one per bar, then the final chord

    @property
    def beat(self) -> float:
        return self.bar / 4.0

    @property
    def final_at(self) -> float:
        return self.bars * self.bar


def _layout(style: Style, duration: float) -> tuple[float, int, float]:
    """``(bar seconds, bars, tail seconds)``: the style's tempo, the final chord ringing on the last frame."""
    bar = 240.0 / style.bpm
    if duration < _MIN_TAIL + bar:
        # Shorter than a bar and its ring-out: one bar, the tempo follows.
        return max(0.25, duration * 0.6), 1, duration - max(0.25, duration * 0.6)
    # The last bar line at least _MIN_TAIL before the end: the chord rings
    # between _MIN_TAIL and _MIN_TAIL plus a bar (under five seconds).
    bars = max(1, int((duration - _MIN_TAIL) // bar))
    return bar, bars, duration - bars * bar


def _default_cues(duration: float) -> list[Cue]:
    """An arc for a score heard without its video (an audition): open, build, peak, breathe, peak."""
    return [
        Cue(0.0, "title"),
        Cue(duration * 0.14, "image"),
        Cue(duration * 0.38, "statement"),
        Cue(duration * 0.62, "image"),
        Cue(duration * 0.78, "outro"),
    ]


def plan_sections(style: Style, duration: float, cues: Sequence[Cue] | None) -> _Plan:
    """The bars, the sections and the chord of every bar of a score of *duration* under *cues*."""
    bar, bars, tail = _layout(style, duration)
    cues = sorted(cues or _default_cues(duration), key=lambda c: c.start)
    starts: list[tuple[int, int, bool]] = []
    for cue in cues:
        at = min(bars, max(0, round(cue.start / bar)))
        energy = _SCENE_ENERGY.get(cue.kind, 2)
        if cue.narrated:
            energy = min(energy, 2)
        if starts and at <= starts[-1][0]:
            # Two scenes on one bar line: the later one decides.
            starts[-1] = (starts[-1][0], energy, cue.narrated)
            continue
        if at >= bars:
            continue
        starts.append((at, energy, cue.narrated))
    if not starts or starts[0][0] != 0:
        starts.insert(0, (0, 1, False))
    # A section shorter than two bars joins the one before it (a quick cut is not a new section).
    merged: list[tuple[int, int, bool]] = []
    for at, energy, narrated in starts:
        if merged and bars >= 8 and at - merged[-1][0] < 2:
            prev = merged[-1]
            merged[-1] = (prev[0], max(prev[1], energy), prev[2] or narrated)
            continue
        merged.append((at, energy, narrated))
    sections: list[_Section] = []
    for index, (at, energy, narrated) in enumerate(merged):
        end = merged[index + 1][0] if index + 1 < len(merged) else bars
        if index == 0 and bars >= 8:
            energy = 1  # every score opens: the first section is an intro
        elif index == 0:
            energy = max(1, energy - 1)
        variant = 0 if energy == 3 else index % 2
        sections.append(_Section(at, end, energy, narrated, variant))
    chords: list[Chord] = []
    for section in sections:
        for b in range(section.start, section.end):
            local = b - section.start
            variant = section.variant ^ ((local // 16) % 2)
            progression = style.main if variant == 0 else style.lift
            chords.append(progression[(local // style.bars_per_chord) % len(progression)])
    if bars >= len(style.cadence) + 2:
        for i, chord in enumerate(style.cadence):
            chords[bars - len(style.cadence) + i] = chord
    chords.append(style.final)
    return _Plan(bar, bars, tail, duration, tuple(sections), tuple(chords))


def score_digest(track_id: str, duration: float, cues: Sequence[Cue] | None) -> str:
    """The digest of everything that decides a score: its cache key and its seed."""
    parts = [track_id, f"{duration:.2f}"]
    for cue in cues or ():
        parts.append(f"{cue.start:.2f}:{cue.kind}:{int(cue.narrated)}")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


# ── Sound sources ─────────────────────────────────────────────────────────


def _hz(midi: float) -> float:
    return 440.0 * 2.0 ** ((midi - 69.0) / 12.0)


def _seconds(n: float) -> int:
    return max(1, round(n * _SR))


def _envelope(n: int, attack: int, hold: int, release: int) -> np.ndarray:
    """A linear attack, a sustain until *hold*, then an exponential release to -40 dB over *release*."""
    env = np.ones(n, dtype=np.float32)
    attack = min(attack, n)
    if attack > 0:
        env[:attack] = np.linspace(0.0, 1.0, attack, endpoint=False, dtype=np.float32)
    if hold < n:
        k = np.arange(n - hold, dtype=np.float32)
        env[hold:] *= np.exp(-k / max(1.0, release / 4.6)).astype(np.float32)
    return env


def _saw(freq: float | np.ndarray, n: int, phase: float) -> np.ndarray:
    """A band-limited sawtooth (polyBLEP): bright without aliasing. *freq* may move (a vibrato)."""
    if np.ndim(freq):
        dt = np.asarray(freq, dtype=np.float64) / _SR
        t = (phase + np.cumsum(dt)) % 1.0
    else:
        dt = float(freq) / _SR
        t = (phase + dt * np.arange(n, dtype=np.float64)) % 1.0
    out = 2.0 * t - 1.0
    low = t < dt
    x = t[low] / (dt[low] if np.ndim(dt) else dt)
    out[low] -= x + x - x * x - 1.0
    high = t > 1.0 - dt
    x = (t[high] - 1.0) / (dt[high] if np.ndim(dt) else dt)
    out[high] -= x * x + x + x + 1.0
    return out.astype(np.float32)


def _sine(freq: float, n: int, phase: float = 0.0) -> np.ndarray:
    return np.sin(phase + 2.0 * math.pi * freq * np.arange(n, dtype=np.float64) / _SR).astype(np.float32)


def _swept_sine(freqs: np.ndarray) -> np.ndarray:
    return np.sin(2.0 * math.pi * np.cumsum(freqs) / _SR).astype(np.float32)


_SOS_CACHE: dict[tuple[str, int, int, int], np.ndarray] = {}


def _sos(kind: str, low: float, high: float = 0.0, order: int = 2) -> np.ndarray:
    key = (kind, int(low), int(high), order)
    found = _SOS_CACHE.get(key)
    if found is None:
        nyquist = _SR * 0.45
        if kind == "band":
            found = signal.butter(
                order, [min(low, nyquist - 10), min(high, nyquist)], btype="bandpass", fs=_SR, output="sos"
            )
        else:
            found = signal.butter(order, min(low, nyquist), btype=kind, fs=_SR, output="sos")
        if len(_SOS_CACHE) > 4096:
            _SOS_CACHE.clear()
        _SOS_CACHE[key] = found
    return found


def _filter(x: np.ndarray, kind: str, low: float, high: float = 0.0, order: int = 2) -> np.ndarray:
    return signal.sosfilt(_sos(kind, low, high, order), x).astype(np.float32)


class _Sounds:
    """Every sound of one render, made once and reused: a progression repeats, so do its chords."""

    def __init__(self, style: Style, rng: np.random.Generator) -> None:
        self.style = style
        self.rng = rng
        self._cache: dict[tuple, np.ndarray] = {}
        self._noise = rng.standard_normal(6 * _SR).astype(np.float32)

    def get(self, key: tuple, make: Callable[[], np.ndarray]) -> np.ndarray:
        found = self._cache.get(key)
        if found is None:
            found = make()
            if len(self._cache) > 3000:
                self._cache.clear()
            self._cache[key] = found
        return found

    def noise(self, n: int, offset: int = 0) -> np.ndarray:
        offset %= max(1, len(self._noise) - n)
        if n <= len(self._noise) - offset:
            return self._noise[offset : offset + n].copy()
        return self.rng.standard_normal(n).astype(np.float32)

    # Pads: stereo, the filter is the bus's.

    def pad(self, notes: tuple[int, ...], length: int, release: int) -> np.ndarray:
        return self.get(("pad", notes, length, release), lambda: self._pad(notes, length, release))

    def _pad(self, notes: tuple[int, ...], length: int, release: int) -> np.ndarray:
        kind = self.style.pad
        n = length + release
        out = np.zeros((2, n), dtype=np.float32)
        attack = _seconds({"strings": 1.1, "dark": 0.9, "glass": 0.7}.get(kind, 0.5))
        env = _envelope(n, attack, length, release)
        for note in notes:
            f = _hz(note)
            phase = (note * 0.618) % 1.0
            if kind == "glass":
                # Soft partials, no edge: a pad that sits behind a piano.
                tone = (
                    _sine(f, n, phase)
                    + 0.32 * _sine(2 * f, n, phase * 2)
                    + 0.12 * _sine(3 * f, n, phase * 3)
                    + 0.05 * _sine(4 * f, n, phase * 4)
                )
                detuned = _sine(f * 1.004, n, phase + 0.3) + 0.25 * _sine(2.008 * f, n) + 0.06 * _sine(4.012 * f, n)
                out[0] += tone * 0.7 + detuned * 0.3
                out[1] += tone * 0.3 + detuned * 0.7
                continue
            if kind == "strings":
                t = np.arange(n, dtype=np.float64) / _SR
                onset = np.clip((t - 0.35) / 0.8, 0.0, 1.0)
                cents = (-7.0, -2.0, 3.0, 8.0)
                voices = []
                for i, c in enumerate(cents):
                    vib = 1.0 + 0.0032 * np.sin(2 * math.pi * (5.0 + 0.35 * i) * t + i) * onset
                    voices.append(_saw(f * 2 ** (c / 1200.0) * vib, n, (phase + 0.23 * i) % 1.0))
                out[0] += (voices[0] + voices[1] * 0.8 + voices[2] * 0.4) * 0.45
                out[1] += (voices[3] + voices[2] * 0.8 + voices[1] * 0.4) * 0.45
                continue
            cents = (-9.0, 0.0, 9.0) if kind == "warm" else (-6.0, 0.0, 6.0)
            a = _saw(f * 2 ** (cents[0] / 1200.0), n, phase)
            b = _saw(f, n, (phase + 0.37) % 1.0)
            c = _saw(f * 2 ** (cents[2] / 1200.0), n, (phase + 0.71) % 1.0)
            sub = _sine(f / 2.0, n) * (0.18 if kind == "dark" else 0.1)
            out[0] += (a + 0.7 * b) * 0.5 + sub
            out[1] += (c + 0.7 * b) * 0.5 + sub
        out *= env / max(1.0, len(notes) ** 0.8)
        return out

    # Keys: mono, panned by the player.

    def key(self, midi: int, length: int, velocity: float) -> np.ndarray:
        bucket = round(velocity * 8) / 8
        return self.get(("key", self.style.keys, midi, length, bucket), lambda: self._key(midi, length, bucket))

    def _key(self, midi: int, length: int, velocity: float) -> np.ndarray:
        kind = self.style.keys
        f = _hz(midi)
        if kind == "epiano":
            return self._epiano(f, length, velocity)
        if kind == "piano":
            return self._piano(f, length, velocity)
        if kind == "stab":
            return self._stab(f, length, velocity)
        return self._pluck(f, length, velocity)

    def _epiano(self, f: float, length: int, velocity: float) -> np.ndarray:
        """The tine piano: two-operator FM, its brightness falling as the note rings."""
        release = _seconds(0.28)
        n = length + release
        t = np.arange(n, dtype=np.float64) / _SR
        index = (0.6 + 1.9 * velocity) * np.exp(-t / 0.42) + 0.18
        body = np.sin(2 * math.pi * f * t + index * np.sin(2 * math.pi * f * t))
        if 14 * f < 16_000:
            body += 0.22 * velocity * np.sin(2 * math.pi * 14 * f * t) * np.exp(-t / 0.011)
        decay = min(4.0, max(0.7, 2.6 * (220.0 / f) ** 0.5))
        amp = np.exp(-t / decay) * _envelope(n, _seconds(0.003), length, release)
        return (body * amp * (0.35 + 0.65 * velocity)).astype(np.float32)

    def _piano(self, f: float, length: int, velocity: float) -> np.ndarray:
        """A felt piano: slightly inharmonic partials, the upper ones dying first, a muted hammer."""
        release = _seconds(0.45)
        n = length + release
        t = np.arange(n, dtype=np.float64) / _SR
        tau = min(6.0, max(0.9, 3.6 * (261.6 / f) ** 0.6))
        out = np.zeros(n, dtype=np.float64)
        brightness = 0.32 + 0.5 * velocity
        for k in range(1, 11):
            fk = k * f * math.sqrt(1.0 + 0.00032 * k * k)
            if fk > 14_000:
                break
            amp = k**-1.15 * brightness ** (k - 1)
            tk = tau / (1.0 + 0.5 * (k - 1))
            out += amp * np.sin(2 * math.pi * fk * t + k) * (0.55 * np.exp(-t / (tk * 0.22)) + 0.45 * np.exp(-t / tk))
        hammer = _filter(self.noise(min(n, _seconds(0.03)), int(f * 7)), "low", 1800) * 0.04 * velocity
        out[: len(hammer)] += hammer * np.exp(-np.arange(len(hammer)) / (_SR * 0.006))
        env = _envelope(n, _seconds(0.002), length, release)
        return (out * env * (0.3 + 0.7 * velocity) * 0.55).astype(np.float32)

    def _pluck(self, f: float, length: int, velocity: float) -> np.ndarray:
        """A soft pluck, mallet-like: harmonic partials decaying faster as they rise."""
        n = max(length, _seconds(0.9))
        t = np.arange(n, dtype=np.float64) / _SR
        tau = 0.55 * (440.0 / f) ** 0.35
        out = np.zeros(n, dtype=np.float64)
        for k in range(1, 10):
            if k * f > 15_000:
                break
            out += k**-1.25 * np.sin(2 * math.pi * k * f * t + 0.5 * k) * np.exp(-t * (1.0 + 1.15 * (k - 1)) / tau)
        out *= _envelope(n, _seconds(0.002), n, 1)
        return (out * (0.3 + 0.7 * velocity) * 0.6).astype(np.float32)

    def _stab(self, f: float, length: int, velocity: float) -> np.ndarray:
        n = length + _seconds(0.08)
        tone = _saw(f * 0.997, n, 0.1) + _saw(f * 1.003, n, 0.6) + 0.5 * _sine(f, n)
        tone = _filter(tone, "low", 2600)
        env = _envelope(n, _seconds(0.003), length, _seconds(0.08))
        env *= np.exp(-np.arange(n) / (_SR * 0.22)).astype(np.float32)
        return tone * env * (0.25 + 0.5 * velocity) * 0.4

    def bell(self, midi: int) -> np.ndarray:
        return self.get(("bell", midi), lambda: self._bell(_hz(midi)))

    def _bell(self, f: float) -> np.ndarray:
        """A glockenspiel-like bell: inharmonic partials, the fundamental ringing longest."""
        n = _seconds(2.8)
        t = np.arange(n, dtype=np.float64) / _SR
        out = np.zeros(n, dtype=np.float64)
        for ratio, amp, tau in ((1.0, 1.0, 1.2), (2.76, 0.42, 0.45), (5.4, 0.2, 0.18), (8.93, 0.08, 0.08)):
            if ratio * f < 16_000:
                out += amp * np.sin(2 * math.pi * ratio * f * t) * np.exp(-t / tau)
        out *= _envelope(n, _seconds(0.001), n, 1)
        return (out * 0.22).astype(np.float32)

    # Bass: mono, centred.

    def bass(self, midi: int, length: int, cutoff: int = 800) -> np.ndarray:
        return self.get(("bass", self.style.bass, midi, length, cutoff), lambda: self._bass(midi, length, cutoff))

    def _bass(self, midi: int, length: int, cutoff: int) -> np.ndarray:
        kind = self.style.bass
        f = _hz(midi)
        if kind in ("pulse16", "offbeat"):
            n = length + _seconds(0.04)
            tone = _saw(f, n, 0.0) * 0.6 + _sine(f, n) * 0.7
            tone = _filter(tone, "low", cutoff)
            env = _envelope(n, _seconds(0.003), length, _seconds(0.04))
            env *= (0.55 + 0.45 * np.exp(-np.arange(n) / (_SR * 0.09))).astype(np.float32)
            return np.tanh(tone * env * 1.4).astype(np.float32) * 0.5
        if kind == "pluck":
            n = length + _seconds(0.1)
            t = np.arange(n, dtype=np.float64) / _SR
            tone = np.sin(2 * math.pi * f * t) + 0.25 * np.sin(4 * math.pi * f * t) + 0.08 * np.sin(6 * math.pi * f * t)
            env = np.exp(-t / 0.55) * _envelope(n, _seconds(0.006), length, _seconds(0.1))
            return (tone * env * 0.55).astype(np.float32)
        if kind == "drone":
            n = length + _seconds(1.2)
            tone = _sine(f, n) + 0.3 * _sine(2 * f, n) + 0.12 * _filter(_saw(f, n, 0.2), "low", 300)
            return tone * _envelope(n, _seconds(0.9), length, _seconds(1.2)) * 0.32
        n = length + _seconds(0.09)
        tone = _sine(f, n) + 0.18 * _sine(2 * f, n, 0.4)
        env = _envelope(n, _seconds(0.012), length, _seconds(0.09))
        return (np.tanh(tone * env * 1.6) / math.tanh(1.6) * 0.42).astype(np.float32)

    # Drums and effects: mono hits, made once each.

    def drum(self, kind: str, variant: int = 0) -> np.ndarray:
        return self.get(("drum", kind, variant), lambda: self._drum(kind, variant))

    def _drum(self, kind: str, variant: int) -> np.ndarray:
        noise = self.noise(_seconds(3.5), 9173 * (variant + 1) + len(kind) * 4421)
        if kind in ("kick", "lofi_kick"):
            n = _seconds(0.5)
            t = np.arange(n, dtype=np.float64) / _SR
            soft = kind == "lofi_kick"
            freqs = (50.0 if soft else 46.0) + (70.0 if soft else 118.0) * np.exp(-t / (0.035 if soft else 0.028))
            body = _swept_sine(freqs) * np.exp(-t / (0.3 if soft else 0.24)).astype(np.float32)
            if soft:
                return _filter(body, "low", 1600) * 0.62
            click = _filter(noise[:n], "high", 2500) * np.exp(-t / 0.0022).astype(np.float32) * 0.22
            return (np.tanh((body + click) * 1.7) / math.tanh(1.7) * 0.55).astype(np.float32)
        if kind in ("snare", "lofi_snare"):
            n = _seconds(0.35)
            t = np.arange(n, dtype=np.float64) / _SR
            tone = _sine(186.0, n) * np.exp(-t / 0.07).astype(np.float32) * 0.45
            rattle = _filter(noise[:n], "band", 1300, 8000) * np.exp(-t / 0.11).astype(np.float32)
            out = tone + rattle * 0.8
            return (_filter(out, "low", 5200) * 0.5) if kind == "lofi_snare" else out * 0.42
        if kind == "clap":
            n = _seconds(0.32)
            t = np.arange(n, dtype=np.float64) / _SR
            env = np.zeros(n)
            for offset in (0.0, 0.009, 0.018):
                mask = t >= offset
                env[mask] += np.exp(-(t[mask] - offset) / 0.0055)
            tail = t >= 0.024
            env[tail] += 0.55 * np.exp(-(t[tail] - 0.024) / 0.09)
            return _filter(noise[:n], "band", 900, 3200) * env.astype(np.float32) * 0.38
        if kind in ("hat", "open_hat", "shaker"):
            n = _seconds({"hat": 0.12, "open_hat": 0.6, "shaker": 0.16}[kind])
            t = np.arange(n, dtype=np.float64) / _SR
            if kind == "shaker":
                env = np.minimum(t / 0.012, 1.0) * np.exp(-t / 0.05)
                return _filter(noise[:n], "band", 4500, 12000) * env.astype(np.float32) * 0.3
            tau = 0.026 if kind == "hat" else 0.2
            return (
                _filter(noise[:n], "high", 6800)
                * np.exp(-t / tau).astype(np.float32)
                * (0.27 if kind == "hat" else 0.18)
            )
        if kind == "tom":
            n = _seconds(0.9)
            t = np.arange(n, dtype=np.float64) / _SR
            body = _swept_sine(72.0 + 64.0 * np.exp(-t / 0.05)) * np.exp(-t / 0.42).astype(np.float32)
            thump = _filter(noise[:n], "low", 600) * np.exp(-t / 0.05).astype(np.float32) * 0.5
            return (np.tanh((body + thump) * 1.8) / math.tanh(1.8) * 0.6).astype(np.float32)
        if kind == "crash":
            n = _seconds(3.2)
            t = np.arange(n, dtype=np.float64) / _SR
            return _filter(noise[:n], "high", 4200) * np.exp(-t / 1.05).astype(np.float32) * 0.1
        if kind == "impact":
            n = _seconds(3.4)
            t = np.arange(n, dtype=np.float64) / _SR
            boom = _swept_sine(36.0 + 34.0 * np.exp(-t / 0.22)) * np.exp(-t / 1.25).astype(np.float32)
            hit = _filter(noise[:n], "low", 1300) * np.exp(-t / 0.32).astype(np.float32) * 0.45
            return (np.tanh((boom + hit) * 1.5) / math.tanh(1.5) * 0.55).astype(np.float32)
        raise ValueError(f"unknown drum {kind}")

    def riser(self, length: int) -> np.ndarray:
        """Noise rising in pitch and level into the next section."""
        return self.get(("riser", length), lambda: self._riser(length))

    def _riser(self, length: int) -> np.ndarray:
        noise = self.noise(length, 31337)
        out = np.empty(length, dtype=np.float32)
        zi = None
        block = 1024
        for start in range(0, length, block):
            stop = min(length, start + block)
            progress = start / max(1, length)
            centre = 350.0 * (7500.0 / 350.0) ** progress
            sos = _sos("band", centre * 0.7, centre * 1.4)
            if zi is None:
                zi = signal.sosfilt_zi(sos) * 0.0
            out[start:stop], zi = signal.sosfilt(sos, noise[start:stop], zi=zi)
        ramp = (np.arange(length, dtype=np.float32) / max(1, length)) ** 2.4
        return out * ramp * 0.16

    def swell(self, length: int) -> np.ndarray:
        """A reversed cymbal: a soft breath into the next section."""
        crash = self.drum("crash")
        n = min(length, len(crash))
        return np.flip(crash[:n]).copy() * 0.9


# ── The arrangement: events ───────────────────────────────────────────────


@dataclass
class _Event:
    start: int
    bus: str
    sound: np.ndarray  # mono (n,) or stereo (2, n)
    gain: float = 1.0
    pan: float = 0.0


def _voicing(root: int, quality: str, previous: tuple[int, ...] | None, low: int, high: int) -> tuple[int, ...]:
    """Four chord tones in [low, high], as close as possible to *previous*: voice leading."""
    classes = []
    for interval in _QUALITIES[quality]:
        pc = (root + interval) % 12
        if pc not in classes:
            classes.append(pc)
    if len(classes) < 4:
        classes.append(classes[0])
    classes = classes[:5]
    options = []
    for pc in classes:
        notes = [n for n in range(low, high + 1) if n % 12 == pc]
        options.append(notes or [low + pc])
    target = previous or (((low + high) // 2,) * 4)
    best: tuple[float, tuple[int, ...]] | None = None
    for combo in itertools.product(*options):
        notes = tuple(sorted(set(combo)))
        if len(notes) < min(4, len(classes)) or notes[-1] - notes[0] > 19:
            continue
        ref = sorted(target)
        cost = sum(abs(a - b) for a, b in zip(notes, ref[: len(notes)], strict=False)) + 0.3 * abs(
            sum(notes) / len(notes) - sum(ref) / len(ref)
        )
        if best is None or cost < best[0]:
            best = (cost, notes)
    return best[1] if best else tuple(sorted(options[i][0] for i in range(len(options))))


@functools.lru_cache(maxsize=512)
def _close_voicing(root: int, quality: str, low: int, high: int) -> tuple[int, ...]:
    """The chord centred in [low, high], for a part that leads no voice (keys, arpeggios, bells)."""
    return _voicing(root, quality, None, low, high)


def _pan(sound: np.ndarray, gain: float, pan: float) -> np.ndarray:
    if sound.ndim == 2:
        return sound * gain
    angle = (pan + 1.0) * math.pi / 4.0
    return np.stack((sound * gain * math.cos(angle), sound * gain * math.sin(angle)))


class _Composer:
    """The events of one score, its pads' filter curve and its kicks (for the pump)."""

    def __init__(self, style: Style, plan: _Plan, sounds: _Sounds, rng: np.random.Generator) -> None:
        self.style = style
        self.plan = plan
        self.sounds = sounds
        self.rng = rng
        self.events: list[_Event] = []
        self.kicks: list[int] = []
        self.cutoff_points: list[tuple[float, float]] = []
        self._last_voicing: tuple[int, ...] | None = None

    def at(self, seconds: float) -> int:
        return max(0, round(seconds * _SR))

    def step_time(self, bar: int, step: int) -> float:
        """Seconds of the 16th *step* of *bar*, the swing on the off sixteenths."""
        sixteenth = self.plan.bar / 16.0
        shift = self.style.swing * sixteenth if step % 2 == 1 else 0.0
        return bar * self.plan.bar + step * sixteenth + shift

    def human(self, seconds: float, spread: float = 0.004) -> float:
        return seconds + float(self.rng.normal(0.0, spread))

    def add(self, seconds: float, bus: str, sound: np.ndarray, gain: float = 1.0, pan: float = 0.0) -> None:
        self.events.append(_Event(self.at(max(0.0, seconds)), bus, sound, gain, pan))

    def section_of(self, bar: int) -> _Section:
        for section in self.plan.sections:
            if section.start <= bar < section.end:
                return section
        return self.plan.sections[-1]

    def compose(self) -> None:
        plan, style = self.plan, self.style
        self._pads()
        previous_energy = 0
        for index, section in enumerate(plan.sections):
            lift = section.energy > previous_energy and index > 0
            self._transition(section, lift, previous_energy)
            for bar in range(section.start, section.end):
                last_of_section = bar == section.end - 1
                next_section = plan.sections[index + 1] if index + 1 < len(plan.sections) else None
                fill = last_of_section and next_section is not None and next_section.energy == 3 and section.energy < 3
                self._keys(bar, section)
                self._bass(bar, section)
                self._drums(bar, section, fill)
                if style.lead and not section.room and section.energy <= 2 and bar % 2 == 0:
                    self._bell(bar, section)
            previous_energy = section.energy
        self._final()
        self._cutoffs()

    # Layers.

    def _pads(self) -> None:
        plan, style = self.plan, self.style
        previous: tuple[int, ...] | None = None
        # A held chord, its release included, stays within what a chunk carries.
        longest = max(1, int((_RING / _SR - 1.6) // plan.bar))
        bar = 0
        while bar < plan.bars:
            chord = plan.chords[bar]
            span = 1
            while bar + span < plan.bars and plan.chords[bar + span] == chord and span < min(4, longest):
                span += 1
            notes = _voicing(style.root + 12 + chord[0], chord[1], previous, 52, 76)
            previous = notes
            section = self.section_of(bar)
            length = _seconds(span * plan.bar + 0.06)
            gain = {1: 0.5, 2: 0.6, 3: 0.68}[section.energy] * (0.85 if section.room else 1.0)
            self.add(bar * plan.bar, "pad", self.sounds.pad(notes, length, _seconds(1.4)), gain)
            bar += span
        self._last_voicing = previous

    def _chord_notes(self, bar: int, low: int, high: int) -> tuple[int, ...]:
        chord = self.plan.chords[bar]
        return _close_voicing(self.style.root + 12 + chord[0], chord[1], low, high)

    def _keys(self, bar: int, section: _Section) -> None:
        style, plan = self.style, self.plan
        pattern = style.keys_pattern
        energy = section.energy
        room = section.room
        gain = (0.5 if room else 0.75) * (0.8 if energy == 1 else 1.0)
        sixteenth = plan.bar / 16.0
        if pattern == "comp":
            notes = self._chord_notes(bar, 55, 79)
            hits = (0, 6) if energy >= 2 else (0,)
            if energy == 3 and not room:
                hits = (0, 6, 10)
            for step in hits:
                length = _seconds(sixteenth * (6 if step == 0 else 4))
                start = self.step_time(bar, step)
                for i, note in enumerate(notes):
                    velocity = 0.55 + 0.12 * (step == 0) + float(self.rng.uniform(-0.05, 0.05))
                    # A hand rolls a chord: lowest note first.
                    self.add(
                        self.human(start + 0.006 * i),
                        "keys",
                        self.sounds.key(note, length, velocity),
                        gain * 0.42,
                        -0.2 + 0.13 * i,
                    )
            return
        if pattern == "offbeat":
            if energy == 1:
                return
            notes = self._chord_notes(bar, 57, 79)
            for step in (2, 6, 10, 14):
                length = _seconds(sixteenth * 1.4)
                for i, note in enumerate(notes):
                    self.add(
                        self.step_time(bar, step),
                        "keys",
                        self.sounds.key(note, length, 0.7),
                        gain * 0.36,
                        -0.3 + 0.2 * i,
                    )
            return
        if pattern == "ostinato":
            chord = plan.chords[bar]
            base = style.root + 24 + chord[0]
            tones = [base + i for i in _QUALITIES[chord[1]][:3]]
            figure = [tones[0], tones[2], tones[0] + 12, tones[2], tones[1] + 12, tones[2], tones[0] + 12, tones[2]]
            step_size = 2 if energy >= 2 else 4
            for i, step in enumerate(range(0, 16, step_size)):
                note = figure[(i * (2 if step_size == 4 else 1)) % len(figure)]
                velocity = 0.5 + (0.15 if step % 8 == 0 else 0.0)
                self.add(
                    self.human(self.step_time(bar, step), 0.006),
                    "keys",
                    self.sounds.key(note, _seconds(sixteenth * step_size * 1.6), velocity),
                    gain * 0.55,
                    0.15,
                )
            return
        if pattern == "broken":
            chord = plan.chords[bar]
            base = style.root + 12 + chord[0]
            tones = [base + i for i in _QUALITIES[chord[1]]]
            fifth = tones[2 % len(tones)]
            # Root, fifth, then the tenth and the upper tones: a pianist's open broken chord.
            figure = [tones[0], fifth, tones[1] + 12, fifth + 12, tones[-1] + 12, tones[1] + 12]
            figure = [n if n >= 50 else n + 12 for n in figure]
            step_size = 2 if energy >= 2 and not room else 4
            for i, step in enumerate(range(0, 16, step_size)):
                note = figure[i % len(figure)]
                velocity = 0.42 + (0.18 if step == 0 else 0.0) + float(self.rng.uniform(-0.04, 0.04))
                self.add(
                    self.human(self.step_time(bar, step), 0.008),
                    "keys",
                    self.sounds.key(note, _seconds(plan.bar * 0.9), velocity),
                    gain * 0.7,
                    -0.15 + 0.06 * (i % 6),
                )
            return
        # Arpeggios: up and down the chord over two octaves.
        notes = self._chord_notes(bar, 60, 76)
        ladder = list(notes) + [n + 12 for n in notes]
        ladder = ladder + ladder[-2:0:-1]
        dense = pattern == "arp16" and energy >= 2 and not room
        step_size = 1 if dense else 2
        if energy == 1 and pattern == "arp16":
            step_size = 2
        for i, step in enumerate(range(0, 16, step_size)):
            note = ladder[i % len(ladder)]
            velocity = 0.55 + (0.2 if step % 4 == 0 else 0.0)
            pan = 0.35 * math.sin(i * 0.7)
            self.add(
                self.step_time(bar, step),
                "keys",
                self.sounds.key(note, _seconds(sixteenth * 1.8), velocity),
                gain * 0.32,
                pan,
            )

    def _bass(self, bar: int, section: _Section) -> None:
        style, plan = self.style, self.plan
        chord = plan.chords[bar]
        root = style.root + chord[0]
        if root > style.root + 7:
            root -= 12
        sixteenth = plan.bar / 16.0
        energy = section.energy
        kind = style.bass
        if kind == "drone":
            if bar == section.start or (bar - section.start) % style.bars_per_chord == 0:
                span = min(style.bars_per_chord, section.end - bar)
                self.add(
                    bar * plan.bar,
                    "bass",
                    self.sounds.bass(root, _seconds(span * plan.bar)),
                    0.6 if energy > 1 else 0.45,
                )
            return
        if energy == 1 and kind != "pulse16":
            return
        if kind == "pulse16":
            cutoff = {1: 260, 2: 520, 3: 980}[energy]
            fifth = root + 7
            figure = (root, root, root + 12, root, root, fifth, root, root + 12)
            for step in range(0, 16, 1 if energy >= 2 else 2):
                note = figure[step % len(figure)]
                accent = 1.0 if step % 4 == 0 else 0.72
                self.add(
                    self.step_time(bar, step),
                    "bass",
                    self.sounds.bass(note, _seconds(sixteenth * 0.85), cutoff),
                    0.55 * accent,
                )
            return
        if kind == "offbeat":
            for step in (2, 6, 10, 14):
                self.add(
                    self.step_time(bar, step), "bass", self.sounds.bass(root, _seconds(sixteenth * 1.6), 900), 0.62
                )
            if energy == 3:
                self.add(
                    self.step_time(bar, 15), "bass", self.sounds.bass(root + 12, _seconds(sixteenth * 0.8), 1100), 0.4
                )
            return
        if kind == "pluck":
            for step, length in ((0, 5), (7, 2), (10, 5)):
                self.add(
                    self.human(self.step_time(bar, step)),
                    "bass",
                    self.sounds.bass(root, _seconds(sixteenth * length)),
                    0.7,
                )
            return
        # Sub: long roots, eighths when the section is full.
        if energy == 2 or section.room:
            for step, length in ((0, 7), (8, 6)):
                self.add(self.step_time(bar, step), "bass", self.sounds.bass(root, _seconds(sixteenth * length)), 0.7)
        else:
            for step in (0, 3, 6, 8, 11, 14):
                self.add(self.step_time(bar, step), "bass", self.sounds.bass(root, _seconds(sixteenth * 1.7)), 0.68)

    def _hit(self, bar: int, step: float, kind: str, gain: float, pan: float = 0.0, variant: int = 0) -> None:
        start = self.human(self.step_time(bar, int(step)) + (step - int(step)) * self.plan.bar / 16.0, 0.003)
        if kind in ("kick", "lofi_kick"):
            self.kicks.append(self.at(start))
        self.add(start, "drums", self.sounds.drum(kind, variant), gain * float(self.rng.uniform(0.88, 1.0)), pan)

    def _drums(self, bar: int, section: _Section, fill: bool) -> None:
        kit, energy, room = self.style.kit, section.energy, section.room
        if kit == "none":
            return
        if kit in ("electronic", "house"):
            if energy == 1:
                for step in range(2, 16, 4):
                    self._hit(bar, step, "shaker", 0.7, 0.25)
                return
            if energy == 2:
                for step in (0, 8) if kit == "electronic" else (0, 4, 8, 12):
                    self._hit(bar, step, "kick", 0.85)
                self._hit(bar, 12, "clap", 0.55 if room else 0.75, -0.05)
                for step in range(2, 16, 4):
                    self._hit(bar, step, "hat", 0.7, 0.2, step % 3)
            else:
                for step in (0, 4, 8, 12):
                    self._hit(bar, step, "kick", 1.0)
                for step in (4, 12):
                    self._hit(bar, step, "clap", 0.8, -0.05)
                for step in range(2, 16, 4):
                    self._hit(bar, step, "open_hat", 0.75, 0.25)
                for step in range(0, 16):
                    if step % 4 != 2:
                        self._hit(bar, step, "hat", 0.45, -0.2, step % 3)
            if fill:
                for i, step in enumerate((12, 13, 14, 15)):
                    self._hit(bar, step, "snare", 0.35 + 0.15 * i)
            return
        if kit == "pulse":
            for step in range(0, 16, 2 if energy == 1 else 1):
                self._hit(bar, step, "hat", 0.35 if step % 4 else 0.55, 0.3 * math.sin(step), step % 3)
            if energy >= 2:
                for step in (0, 8) if energy == 2 or room else (0, 4, 8, 12):
                    self._hit(bar, step, "kick", 0.9)
            if energy == 3 and not room:
                self._hit(bar, 12, "clap", 0.6)
            if fill:
                for i, step in enumerate((8, 10, 12, 13, 14, 15)):
                    self._hit(bar, step, "snare", 0.25 + 0.1 * i)
            return
        if kit == "cinematic":
            if energy == 1:
                return
            pattern = (0, 10) if energy == 2 or room else (0, 3, 6, 8, 11, 14)
            for step in pattern:
                self._hit(bar, step, "tom", 0.95 if step in (0, 8) else 0.7, 0.3 * math.sin(step))
            if fill:
                for i, step in enumerate((8, 10, 12, 13, 14, 15)):
                    self._hit(bar, step, "tom", 0.5 + 0.08 * i, -0.3 + 0.12 * i)
            return
        # Lo-fi: a dusty swung beat.
        for step in range(0, 16, 2):
            self._hit(bar, step, "hat", 0.42 if step % 4 else 0.55, 0.2, step % 3)
        if energy == 1:
            return
        for step in (0, 10) if energy == 2 else (0, 7, 10):
            self._hit(bar, step, "lofi_kick", 0.95)
        for step in (4, 12):
            self._hit(bar, step, "lofi_snare", 0.5 if room else 0.65, -0.08)

    def _bell(self, bar: int, section: _Section) -> None:
        notes = self._chord_notes(bar, 74, 90)
        for beat in (0, 2) if section.energy == 1 else (0,):
            note = notes[int(self.rng.integers(0, len(notes)))]
            self.add(
                self.human(bar * self.plan.bar + beat * self.plan.beat, 0.006),
                "lead",
                self.sounds.bell(note),
                0.45,
                float(self.rng.uniform(-0.4, 0.4)),
            )

    def _transition(self, section: _Section, lift: bool, previous_energy: int) -> None:
        style, plan = self.style, self.plan
        at = section.start * plan.bar
        if lift and section.energy - previous_energy >= 1 and section.start > 0:
            if style.risers and section.energy == 3:
                length = _seconds(min(2 * plan.bar, 4.5, at))
                self.add(at - length / _SR, "fx", self.sounds.riser(length), 1.0)
            elif style.kit != "none":
                length = _seconds(min(plan.bar, 2.5, at))
                self.add(at - length / _SR, "fx", self.sounds.swell(length), 0.5)
        if section.energy == 3 and previous_energy < 3 and section.start > 0:
            if style.impacts:
                self.add(at, "fx", self.sounds.drum("impact"), 0.8)
            if style.kit in ("electronic", "house", "pulse"):
                self.add(at, "drums", self.sounds.drum("crash"), 0.9, 0.2)

    def _final(self) -> None:
        """The final chord on the last bar line, ringing out to the last frame."""
        style, plan = self.style, self.plan
        at = plan.final_at
        ring = max(0.4, plan.duration - at)
        chord = plan.chords[-1]
        notes = _voicing(style.root + 12 + chord[0], chord[1], getattr(self, "_last_voicing", None), 52, 76)
        self.add(at, "pad", self.sounds.pad(notes, _seconds(ring * 0.55), _seconds(ring * 0.5)), 0.62)
        root = style.root + chord[0]
        if root > style.root + 7:
            root -= 12
        self.add(
            at,
            "bass",
            self.sounds.bass(root, _seconds(ring * 0.6))
            if style.bass != "pulse16"
            else self.sounds.bass(root, _seconds(ring * 0.4), 400),
            0.6,
        )
        key_notes = _voicing(style.root + 12 + chord[0], chord[1], None, 55, 79)
        for i, note in enumerate(key_notes):
            self.add(at + 0.012 * i, "keys", self.sounds.key(note, _seconds(ring * 0.7), 0.6), 0.35, -0.2 + 0.13 * i)
        if style.kit in ("electronic", "house", "pulse"):
            self.kicks.append(self.at(at))
            self.add(at, "drums", self.sounds.drum("kick"), 0.9)
            self.add(at, "drums", self.sounds.drum("crash"), 0.8, 0.2)
        elif style.kit == "cinematic":
            self.add(at, "drums", self.sounds.drum("tom"), 1.0)
        if style.impacts:
            self.add(at, "fx", self.sounds.drum("impact"), 0.85)

    def _cutoffs(self) -> None:
        """The pads' filter over time: closed in an intro, open in a lift, opening during a riser."""
        style, plan = self.style, self.plan
        levels = {1: 1500.0, 2: 3200.0, 3: 6500.0}
        points: list[tuple[float, float]] = []
        previous = None
        for section in plan.sections:
            target = levels[section.energy] * style.brightness * (0.8 if section.room else 1.0)
            at = section.start * plan.bar
            ramp = plan.bar * (2 if previous is not None and target > previous else 0.5)
            if previous is None:
                points.append((0.0, target))
            else:
                points.append((max(0.0, at - ramp if target > previous else at), previous))
                points.append((at + (0.0 if target > previous else ramp), target))
            previous = target
        points.append((plan.final_at, previous or 1500.0))
        points.append((plan.duration + 1.0, (previous or 1500.0) * 0.6))
        self.cutoff_points = sorted(points)


# ── The render ────────────────────────────────────────────────────────────


class _StatefulFilter:
    """A fixed filter run chunk after chunk on a stereo signal, its state carried."""

    def __init__(self, sos: np.ndarray) -> None:
        self.sos = sos
        self.zi = np.zeros((2, sos.shape[0], 2))

    def run(self, x: np.ndarray) -> np.ndarray:
        out = np.empty_like(x)
        for ch in range(2):
            out[ch], self.zi[ch] = signal.sosfilt(self.sos, x[ch], zi=self.zi[ch])
        return out


class _SweepFilter:
    """A low-pass whose cutoff moves (the pads opening), in blocks, its state carried."""

    def __init__(self) -> None:
        self.zi = np.zeros((2, 1, 2))

    def run(self, x: np.ndarray, cutoff: np.ndarray, block: int = 512) -> np.ndarray:
        out = np.empty_like(x)
        n = x.shape[1]
        for start in range(0, n, block):
            stop = min(n, start + block)
            fc = float(np.clip(cutoff[start], 120.0, 16_000.0))
            sos = _sos("low", round(fc / 20.0) * 20.0, order=2)
            for ch in range(2):
                out[ch, start:stop], self.zi[ch] = signal.sosfilt(sos, x[ch, start:stop], zi=self.zi[ch])
        return out


class _Convolver:
    """A stereo impulse response applied chunk after chunk: the tail of one chunk rings into the next."""

    def __init__(self, ir: np.ndarray) -> None:
        self.ir = ir
        self.carry = np.zeros((2, ir.shape[1] - 1), dtype=np.float32)

    def run(self, mono: np.ndarray) -> np.ndarray:
        n = len(mono)
        width = self.carry.shape[1]
        full = np.zeros((2, n + width), dtype=np.float32)
        if np.any(mono):
            for ch in range(2):
                full[ch] = signal.oaconvolve(mono, self.ir[ch])
        full[:, :width] += self.carry
        self.carry = full[:, n:].copy()
        return full[:, :n]


def _reverb_ir(seconds: float, rng: np.random.Generator) -> np.ndarray:
    """A room: decorrelated noise decaying 60 dB over *seconds*, darker as it rings, after a pre-delay."""
    n = _seconds(seconds)
    pre = _seconds(0.018)
    t = np.arange(n, dtype=np.float64) / _SR
    env = np.exp(-6.9 * t / seconds)
    ir = np.zeros((2, pre + n), dtype=np.float32)
    for ch in range(2):
        tail = rng.standard_normal(n) * env
        early = signal.sosfilt(_sos("low", 12000), tail[: n // 4])
        late = signal.sosfilt(_sos("low", 5500), tail)
        late[: n // 4] = 0.6 * early + 0.4 * late[: n // 4]
        ir[ch, pre:] = late.astype(np.float32)
    ir /= np.sqrt(np.sum(ir**2) / 2.0) + 1e-9
    return ir


def _delay_ir(beat: float) -> np.ndarray:
    """A ping-pong dotted-eighth echo, five repeats."""
    step = _seconds(beat * 0.75)
    ir = np.zeros((2, step * 6 + 1), dtype=np.float32)
    for k in range(1, 6):
        ir[k % 2, step * k] = 0.55**k
    return ir


_BUSES = ("pad", "keys", "bass", "drums", "lead", "fx")
_REVERB_SEND = {"pad": 0.55, "keys": 0.6, "bass": 0.0, "drums": 0.12, "lead": 0.9, "fx": 0.7}
_DELAY_SEND = {"keys": 0.22, "lead": 0.45}
_BUS_GAIN = {"pad": 0.85, "keys": 0.9, "bass": 0.7, "drums": 0.8, "lead": 0.7, "fx": 0.6}
#: The master's air: what lies above it is lifted a little, as a mastering shelf would.
_AIR_HZ = 4500
_AIR_GAIN = 0.4


def render_score(track_id: str, duration: float, cues: Sequence[Cue] | None, out_path: str) -> None:
    """Write the score of *track_id* for a video of *duration* seconds under *cues*: 48 kHz stereo WAV."""
    style = get_style(track_id)
    if style is None:
        raise ValueError(f"no built-in track {track_id!r}")
    duration = max(1.0, float(duration))
    seed = int(score_digest(style.id, duration, cues)[:16], 16)
    rng = np.random.default_rng(seed)
    plan = plan_sections(style, duration, cues)
    sounds = _Sounds(style, rng)
    composer = _Composer(style, plan, sounds, rng)
    composer.compose()
    events = sorted(composer.events, key=lambda e: e.start)
    kicks = np.array(sorted(composer.kicks), dtype=np.int64)
    cut_t = np.array([p[0] for p in composer.cutoff_points])
    cut_f = np.array([p[1] for p in composer.cutoff_points])

    total = _seconds(duration)
    buses = {name: np.zeros((2, _CHUNK + _RING), dtype=np.float32) for name in _BUSES}
    pad_highpass = _StatefulFilter(_sos("high", 140))
    pad_sweep = _SweepFilter()
    master_highpass = _StatefulFilter(_sos("high", 34))
    air = _StatefulFilter(_sos("high", _AIR_HZ))
    reverb = _Convolver(_reverb_ir(style.room_seconds, rng) * style.reverb)
    delay = _Convolver(_delay_ir(plan.beat) * 0.6)
    pump_length = _seconds(min(0.22, plan.beat * 0.6))
    pump_curve = 1.0 - style.pump * np.exp(-np.arange(pump_length) / (pump_length / 3.5)).astype(np.float32)
    master_gain = 0.9 * style.gain
    event_index = 0

    with wave.open(out_path, "wb") as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(_SR)
        for c0 in range(0, total, _CHUNK):
            n = min(_CHUNK, total - c0)
            while event_index < len(events) and events[event_index].start < c0 + n:
                event = events[event_index]
                event_index += 1
                offset = event.start - c0
                if offset < 0:
                    continue
                stereo = _pan(event.sound, event.gain, event.pan)
                room = buses[event.bus].shape[1] - offset
                length = min(stereo.shape[1], room)
                if length > 0:
                    buses[event.bus][:, offset : offset + length] += stereo[:, :length]
            t = (c0 + np.arange(n)) / _SR
            pads = pad_sweep.run(pad_highpass.run(buses["pad"][:, :n]), np.interp(t, cut_t, cut_f))
            if style.pump > 0 and len(kicks):
                pump = np.ones(n, dtype=np.float32)
                first = np.searchsorted(kicks, c0 - pump_length)
                last = np.searchsorted(kicks, c0 + n)
                for kick in kicks[first:last]:
                    a = max(0, kick - c0)
                    b = min(n, kick - c0 + pump_length)
                    if b > a:
                        pump[a:b] = np.minimum(pump[a:b], pump_curve[a - (kick - c0) : b - (kick - c0)])
                pads = pads * pump
                buses["bass"][:, :n] *= pump
            dry = pads * _BUS_GAIN["pad"]
            send = (pads[0] + pads[1]) * 0.5 * _REVERB_SEND["pad"]
            echo = np.zeros(n, dtype=np.float32)
            for name in _BUSES[1:]:
                part = buses[name][:, :n]
                dry = dry + part * _BUS_GAIN[name]
                send = send + (part[0] + part[1]) * 0.5 * _REVERB_SEND[name]
                if name in _DELAY_SEND:
                    echo = echo + (part[0] + part[1]) * 0.5 * _DELAY_SEND[name]
            mix = dry + reverb.run(send.astype(np.float32)) + delay.run(echo)
            if style.crackle:
                mix = mix + _crackle(n, rng)
            mix = master_highpass.run(mix)
            mix = (mix + _AIR_GAIN * air.run(mix)) * master_gain
            mix = np.tanh(mix * 1.15) / 1.15
            # The ends: no click in, silence on the last frame.
            fade_in = min(n, _seconds(0.004))
            if c0 == 0:
                mix[:, :fade_in] *= np.linspace(0.0, 1.0, fade_in, dtype=np.float32)
            end_fade = _seconds(min(1.2, plan.tail * 0.6))
            tail_start = total - end_fade
            if c0 + n > tail_start:
                a = max(0, tail_start - c0)
                k = np.arange(a, n) + c0 - tail_start
                mix[:, a:] *= np.cos(np.clip(k / end_fade, 0.0, 1.0) * math.pi / 2.0) ** 2
            dither = (rng.random((2, n)) - rng.random((2, n))) / 32768.0
            pcm = np.clip((mix + dither) * 32767.0, -32768, 32767).astype("<i2")
            out.writeframes(pcm.T.tobytes())
            for name in _BUSES:
                bus = buses[name]
                bus[:, :_RING] = bus[:, n : n + _RING]
                bus[:, _RING:] = 0.0


def _crackle(n: int, rng: np.random.Generator) -> np.ndarray:
    """Vinyl: sparse soft clicks over a faint hiss."""
    hiss = signal.lfilter([0.02], [1.0, -0.97], rng.standard_normal(n)).astype(np.float32) * 0.012
    clicks = np.zeros(n, dtype=np.float32)
    count = rng.poisson(5.0 * n / _SR)
    if count:
        at = rng.integers(0, n, count)
        clicks[at] = rng.uniform(-0.08, 0.08, count).astype(np.float32)
        clicks = _filter(clicks, "high", 1500)
    noise = hiss + clicks
    return np.stack((noise, np.roll(noise, 37)))
