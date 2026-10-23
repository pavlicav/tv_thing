#!/usr/bin/env python3
"""The Sports Guide's music: smooth jazz for channel 60, as a cable listings channel had
under its pages in the nineties. Synthesized here (numpy and scipy), never sampled.

    python3 sports_guide_music.py [--out static/sports-guide-music.m4a] [--wav FILE]

Three tunes, about five and a half minutes, then the loop starts over: the channel plays
it from wherever the wall clock says it is (server.py's build_sports_guide_command and
static/sports-guide.html both seek to time.time() modulo its length), so it sounds like
it was always on. Writes the AAC file and, beside it, ``<name>.json`` with its length.

  * "The Sports Guide Theme", F major, 92 BPM: a soprano sax over a Rhodes-style FM
    electric piano, fretless bass, a soft drum machine and strings. The vibraphone plays
    VizelVision's open logo (C E G A) in the intro and the closed logo in the coda.
  * "Halftime Report", E-flat, 84 BPM: the vibraphone leads, the piano and a pad under it.
  * "Late Scores", D major, 104 BPM: a bossa, a synth flute over nylon guitar.

Everything is seeded, so the file is the same each time it is made. Levelled to -20 LUFS
with a little of the period's broadcast character: band-limited, a breath of tape hiss.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import zlib
from pathlib import Path

import numpy as np
from scipy import signal
from scipy.io import wavfile

SR = 48000
HERE = Path(__file__).resolve().parent
OUT = HERE / "static" / "sports-guide-music.m4a"
LOUDNESS = -20.0
rng = np.random.default_rng(60)

NOTE = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
QUALITY = {  # intervals above the root
    "maj7": (0, 4, 7, 11), "maj9": (0, 4, 7, 11, 14), "6/9": (0, 4, 7, 9, 14),
    "m7": (0, 3, 7, 10), "m9": (0, 3, 7, 10, 14), "m11": (0, 3, 7, 10, 14, 17),
    "7": (0, 4, 7, 10), "9": (0, 4, 7, 10, 14), "13": (0, 4, 7, 10, 14, 21),
    "7sus": (0, 5, 7, 10), "13sus": (0, 5, 7, 10, 14, 21), "7b9": (0, 4, 7, 10, 13),
    "m7b5": (0, 3, 6, 10),
}


def hz(midi):
    return 440.0 * 2 ** ((np.asarray(midi, float) - 69) / 12)


def chord(name):
    """'Bbmaj9' -> (root pitch class, intervals)."""
    root = NOTE[name[0]]
    rest = name[1:]
    if rest[:1] in ("b", "#"):
        root += -1 if rest[0] == "b" else 1
        rest = rest[1:]
    return root % 12, QUALITY[rest]


# ---------------------------------------------------------------------------
# Instruments: each returns a mono note (numpy array) starting at its onset
# ---------------------------------------------------------------------------

def times(n):
    return np.arange(n) / SR


def release(x, gate, length=0.12):
    """Fade a note out from ``gate`` seconds over ``length``."""
    t = times(len(x))
    return x * np.clip(1 - (t - gate) / length, 0, 1)


def epiano(midi, gate, vel=0.8):
    """An FM electric piano, the DX7's tine: a 1:1 pair whose brightness falls away,
    a bell partial at the strike, a slower decay for the low notes."""
    n = int((gate + 0.6) * SR)
    t = times(n)
    f = float(hz(midi))
    index = vel * (1.6 * np.exp(-t / 0.28) + 0.35)
    x = np.sin(2 * np.pi * f * t + index * np.sin(2 * np.pi * f * t))
    x += 0.22 * vel * np.sin(2 * np.pi * f * 14 * t) * np.exp(-t / 0.018)
    x += 0.12 * np.sin(2 * np.pi * f * 2.001 * t) * np.exp(-t / 0.6)
    decay = 1.4 + 2.2 * (f < 260)
    return release(x * np.exp(-t / decay) * vel, gate, 0.25) * 0.5


def fretless(midi, gate, vel=0.8, from_midi=None):
    """A fretless bass: a soft round tone that swells a little after the attack (the
    "mwah"), sliding up or down from the note before when asked."""
    n = int((gate + 0.15) * SR)
    t = times(n)
    f = hz(midi) * np.ones(n)
    if from_midi is not None:
        glide = np.clip(t / 0.06, 0, 1)
        f = hz(from_midi) * (hz(midi) / hz(from_midi)) ** glide
    phase = 2 * np.pi * np.cumsum(f) / SR
    mwah = 0.25 + 0.35 * np.clip(t / 0.15, 0, 1) * np.exp(-t / 0.6)
    x = np.sin(phase) + mwah * np.sin(2 * phase) + 0.5 * mwah * np.sin(3 * phase)
    env = np.clip(t / 0.012, 0, 1) * np.exp(-t / 2.5)
    return release(x * env * vel, gate, 0.08) * 0.55


def vibes(midi, gate, vel=0.8):
    """A vibraphone: the bar's tone and its bright fourth partial, the motor's
    tremolo, the mallet's click; damped when the pedal comes up."""
    n = int((gate + 1.6) * SR)
    t = times(n)
    f = float(hz(midi))
    x = np.sin(2 * np.pi * f * t) * np.exp(-t / 1.8)
    x += 0.16 * np.sin(2 * np.pi * f * 3.93 * t) * np.exp(-t / 0.25)
    x *= 1 + 0.22 * np.sin(2 * np.pi * 5.2 * t)
    x += 0.15 * signal.lfilter([1], [1, -0.6], rng.standard_normal(n)) * np.exp(-t / 0.004)
    return release(x * vel, gate + 0.4, 0.5) * 0.5


def guitar(midi, gate, vel=0.7):
    """A nylon string: a plucked comb (Karplus-Strong, as a filter), warm and short."""
    n = int((gate + 0.4) * SR)
    period = SR / float(hz(midi))
    d = int(period)
    burst = np.zeros(n)
    burst[:d] = signal.lfilter([0.5], [1, -0.5], rng.uniform(-1, 1, d))
    a = np.zeros(d + 2)
    a[0], a[d], a[d + 1] = 1.0, -0.4985, -0.4985
    x = signal.lfilter([1.0], a, burst)
    return release(x * vel, gate, 0.1) * 0.6


class Line:
    """A monophonic lead played legato: one continuous phase with glides between
    tied notes, vibrato that blooms on the long ones, breath between phrases."""

    def __init__(self, voice):
        self.voice = voice

    def render(self, notes, length):
        """``notes``: [(start s, midi or None, duration s)]. Returns length seconds."""
        n = int(length * SR)
        f = np.zeros(n)
        amp = np.zeros(n)
        vib_depth = np.zeros(n)
        t_note = np.zeros(n)
        previous_end, previous = -1.0, None
        for start, midi, dur in notes:
            if midi is None:
                continue
            a, b = int(start * SR), min(n, int((start + dur) * SR))
            if a >= n:
                break
            tied = previous is not None and abs(start - previous_end) < 0.02
            seg = np.arange(b - a) / SR
            target = float(hz(midi))
            if tied:
                glide = np.clip(seg / 0.045, 0, 1)
                f[a:b] = float(hz(previous)) * (target / float(hz(previous))) ** glide
            else:
                f[a:b] = target
            attack = 0.05 if self.voice == "flute" else 0.03
            env = np.clip(seg / (0.012 if tied else attack), 0, 1) * (0.88 if tied else 1.0)
            env *= 1 + 0.12 * np.clip(seg / 0.4, 0, 1)                           # a little swell
            env *= np.clip((dur - seg) / 0.07, 0, 1) if not self._tied_next(notes, start, dur) else 1
            amp[a:b] = env
            vib_depth[a:b] = np.clip((seg - 0.22) / 0.35, 0, 1) * (0.28 if self.voice == "sax" else 0.18)
            t_note[a:b] = seg
            previous_end, previous = start + dur, midi
        t = times(n)
        f = np.where(f > 0, f, 220.0)
        f = f * 2 ** (vib_depth * np.sin(2 * np.pi * 5.4 * t) / 12)
        phase = 2 * np.pi * np.cumsum(f) / SR
        if self.voice == "sax":
            x = np.zeros(n)
            for k in range(1, 26):
                gain = (1.0 / k) * (f * k < 9000)
                x += gain * np.sin(k * phase)
            # the body: two resonances, the reed's bark, a soft top
            for fc, q, g in ((1150, 1.4, 1.8), (2650, 2.0, 1.1)):
                b_, a_ = signal.iirpeak(fc, q, fs=SR)
                x = x + g * signal.lfilter(b_, a_, x)
            x = signal.sosfilt(signal.butter(2, 5200, "low", fs=SR, output="sos"), x)
            breath = signal.sosfilt(signal.butter(2, [1500, 5000], "band", fs=SR, output="sos"),
                                    rng.standard_normal(n)) * 0.05
            x = np.tanh(1.4 * x * amp) * 0.45 + breath * amp
        else:   # flute: nearly a sine, a breathy chiff at the start of each note
            x = np.sin(phase) + 0.22 * np.sin(2 * phase) + 0.06 * np.sin(3 * phase)
            chiff = np.exp(-t_note / 0.04) * (t_note > 0)
            breath = signal.sosfilt(signal.butter(2, [1800, 7000], "band", fs=SR, output="sos"),
                                    rng.standard_normal(n))
            x = x * amp * 0.5 + breath * amp * (0.035 + 0.12 * chiff)
        return x

    @staticmethod
    def _tied_next(notes, start, dur):
        for s, m, _ in notes:
            if m is not None and abs(s - (start + dur)) < 0.02:
                return True
        return False


def pad(midis, gate, vel=0.5):
    """Strings: detuned saws, filtered dark, a slow bow."""
    n = int((gate + 0.8) * SR)
    t = times(n)
    x = np.zeros(n)
    for m in midis:
        for cents in (-9, 0, 8):
            f = float(hz(m)) * 2 ** (cents / 1200)
            x += signal.sawtooth(2 * np.pi * f * t + rng.uniform(0, 6))
    x = signal.sosfilt(signal.butter(2, 1700, "low", fs=SR, output="sos"), x)
    env = np.clip(t / 0.7, 0, 1)
    return release(x * env * vel / (3 * len(midis)), gate, 0.7) * 0.6


def kick(vel=0.8):
    n = int(0.3 * SR)
    t = times(n)
    f = 48 + 70 * np.exp(-t / 0.03)
    return np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t / 0.16) * vel


def rim(vel=0.6):
    n = int(0.08 * SR)
    t = times(n)
    tone = np.sin(2 * np.pi * 1650 * t) * np.exp(-t / 0.012) + 0.6 * np.sin(2 * np.pi * 820 * t) * np.exp(-t / 0.02)
    return tone * vel * 0.6


def hat(vel=0.5, open_=False):
    n = int((0.35 if open_ else 0.08) * SR)
    t = times(n)
    x = signal.sosfilt(signal.butter(2, 7500, "high", fs=SR, output="sos"), rng.standard_normal(n))
    return x * np.exp(-t / (0.12 if open_ else 0.028)) * vel * 0.4


def shaker(vel=0.4):
    n = int(0.09 * SR)
    t = times(n)
    x = signal.sosfilt(signal.butter(2, [4500, 9000], "band", fs=SR, output="sos"), rng.standard_normal(n))
    return x * np.clip(t / 0.03, 0, 1) * np.exp(-t / 0.035) * vel * 0.35


def brush(vel=0.5):
    n = int(0.3 * SR)
    t = times(n)
    x = signal.sosfilt(signal.butter(2, [1800, 6000], "band", fs=SR, output="sos"), rng.standard_normal(n))
    return x * np.clip(t / 0.01, 0, 1) * np.exp(-t / 0.09) * vel * 0.5


def ride(vel=0.4):
    n = int(1.2 * SR)
    t = times(n)
    x = sum(np.sin(2 * np.pi * f * t + rng.uniform(0, 6)) for f in (3120, 4430, 5180, 6890)) * 0.2
    x += signal.sosfilt(signal.butter(2, 6000, "high", fs=SR, output="sos"), rng.standard_normal(n)) * 0.3
    return x * np.exp(-t / 0.45) * vel * 0.25


# ---------------------------------------------------------------------------
# The mix
# ---------------------------------------------------------------------------

class Desk:
    """Stereo buses: dry, and a send to one shared room."""

    def __init__(self, length):
        self.n = int(length * SR)
        self.dry = np.zeros((2, self.n))
        self.wet = np.zeros((2, self.n))

    def add(self, x, at, gain=1.0, pan=0.0, verb=0.15):
        a = int(round(at * SR))
        if a >= self.n or len(x) == 0:
            return
        x = x[:self.n - a] * gain
        angle = (np.clip(pan, -1, 1) + 1) * np.pi / 4
        for ch, g in ((0, np.cos(angle)), (1, np.sin(angle))):
            self.dry[ch, a:a + len(x)] += x * g * np.sqrt(2)
            if verb:
                self.wet[ch, a:a + len(x)] += x * g * np.sqrt(2) * verb

    def mix(self):
        n_ir = int(2.2 * SR)
        t = times(n_ir)
        room = []
        for _ in range(2):
            ir = rng.standard_normal(n_ir) * np.exp(-6.9 * t / 1.9)
            ir = signal.sosfilt(signal.butter(1, 5500, "low", fs=SR, output="sos"), ir)
            ir[:int(0.018 * SR)] = 0
            room.append(ir / np.sqrt(np.sum(ir ** 2)))
        wet = np.array([signal.fftconvolve(self.wet[c], room[c])[:self.n] for c in range(2)])
        return self.dry + wet * 0.9


def chorus(x, depth=0.0025, rate=0.7):
    """A stereo chorus for the piano and guitar: two modulated delays."""
    n = len(x)
    t = times(n)
    out = []
    for phase in (0.0, np.pi / 2):
        delay = (0.012 + depth * (1 + np.sin(2 * np.pi * rate * t + phase))) * SR
        idx = np.arange(n) - delay
        lo = np.clip(np.floor(idx).astype(int), 0, n - 1)
        hi = np.clip(lo + 1, 0, n - 1)
        frac = idx - np.floor(idx)
        out.append(0.6 * x + 0.4 * (x[lo] * (1 - frac) + x[hi] * frac))
    return np.array(out)


# ---------------------------------------------------------------------------
# The tunes
# ---------------------------------------------------------------------------

def voicings(names):
    """Rootless voicings for the piano, each moving as little as it can from the last."""
    out, centre = [], 63.0
    for name in names:
        root, intervals = chord(name)
        tones = [i for i in intervals if i not in (0,)]
        if len(tones) > 4:
            tones = [i for i in tones if i != 7] or tones
        tones = tones[:4]
        notes = []
        for i in tones:
            pc = (root + i) % 12
            options = [pc + 12 * k for k in range(3, 7) if 52 <= pc + 12 * k <= 74]
            notes.append(min(options, key=lambda m: abs(m - centre)))
        notes = sorted(set(notes))
        centre = 0.7 * centre + 0.3 * float(np.mean(notes))
        out.append(notes)
    return out


def bass_root(name, low=36):
    root, intervals = chord(name)
    return low + (root - low) % 12


THEME = {
    "title": "The Sports Guide Theme", "bpm": 92, "style": "smooth", "lead": "sax",
    "A": ["Fmaj9", "Am7", "Bbmaj9", "C13sus", "Fmaj9", "Dm9", "Gm9", "C13"],
    "B": ["Bbmaj9", "Am7", "Gm9", "Fmaj9", "Bbmaj9", ("Am7", "D7b9"), "Gm9", "C13sus"],
    "melody": {
        "A1": [[None, 1, 69, .5, 72, .5, 76, 1.5, 74, .5], [72, 1.5, 69, .5, 67, 1, 64, 1],
               [65, .5, 67, .5, 69, .5, 72, .5, 74, 2], [72, 1, 70, .5, 69, .5, 67, 2],
               [None, 1, 69, .5, 72, .5, 76, 1.5, 79, .5], [77, 1.5, 76, .5, 74, 1, 72, 1],
               [70, .5, 69, .5, 70, .5, 74, .5, 77, 1, 76, 1], [74, .5, 72, .5, 69, 1, None, 2]],
        "A2_end": [74, .5, 76, .5, 77, 3],
        "B": [[74, 1, 77, 1, 81, 1.5, 79, .5], [76, 1, 79, 1, 72, 2], [70, .5, 74, .5, 77, .5, 81, .5, 79, 2],
              [76, 1, 72, 1, 69, 2], [74, 1, 77, 1, 81, 1.5, 82, .5], [81, 1, 79, 1, 78, 1, 75, 1],
              [74, 1.5, 72, .5, 70, 1, 69, 1], [67, 1, 69, 1, 70, 1, 72, 1]],
        "A3_end": [74, .5, 72, .5, 69, 3],
    },
    "intro": ["Fmaj9", "Bbmaj9", "Fmaj9", "Bbmaj9"], "outro": ["Fmaj9", "Bbmaj9", "Fmaj9", "Fmaj9"],
    "logo_in": (72, 76, 79, 81), "logo_out": (74, 78, 81, 86),
}
HALFTIME = {
    "title": "Halftime Report", "bpm": 84, "style": "smooth", "lead": "vibes",
    "A": ["Ebmaj9", "Cm9", "Abmaj9", "Bb13sus", "Gm7", "Cm9", "Fm9", "Bb13"],
    "B": ["Abmaj9", "Gm7", "Fm9", "Ebmaj9", "Abmaj9", ("Gm7", "C7b9"), "Fm9", "Bb13sus"],
    "melody": {
        "A1": [[67, .5, 70, .5, 74, 1, 77, 1, 74, 1], [75, 1.5, 74, .5, 72, 1, 67, 1],
               [72, .5, 75, .5, 79, 1.5, 77, .5, 75, 1], [77, 2, 75, 1, None, 1],
               [70, .5, 74, .5, 77, 1.5, 74, .5, 70, 1], [74, 1, 75, 1, 79, 2],
               [80, 1, 79, .5, 77, .5, 75, 1, 72, 1], [74, 1, 72, .5, 70, .5, 67, 2]],
        "A2_end": [70, 1, 72, 1, 75, 2],
        "B": [[72, 1, 75, 1, 79, 2], [77, 1, 74, 1, 70, 2], [68, .5, 72, .5, 75, .5, 79, .5, 77, 2],
              [79, 1, 77, 1, 74, 2], [72, 1, 75, 1, 79, 1, 82, 1], [79, 1, 77, 1, 76, 1, 73, 1],
              [72, 1.5, 68, .5, 65, 1, 67, 1], [68, 1, 70, 1, 72, 1, 74, 1]],
        "A3_end": [74, .5, 72, .5, 70, 1, 67, 2],
    },
    "intro": ["Abmaj9", "Ebmaj9", "Abmaj9", "Bb13sus"], "outro": ["Ebmaj9", "Abmaj9", "Ebmaj9", "Ebmaj9"],
}
LATE_SCORES = {
    "title": "Late Scores", "bpm": 104, "style": "bossa", "lead": "flute",
    "A": ["Dmaj9", "Bm9", "Em9", "A13", "F#m7", "B7b9", "Em9", ("A7sus", "A7")],
    "B": ["Gmaj9", "F#m7", "Em9", "Dmaj9", "Gmaj9", ("F#m7", "B7b9"), "Em9", "A13sus"],
    "melody": {
        "A1": [[78, 1.5, 76, .5, 78, 1, 81, 1], [85, 1.5, 83, .5, 81, 2],
               [79, .5, 78, .5, 76, 1, 74, .5, 76, .5, 78, 1], [76, 2, None, 1, 73, .5, 74, .5],
               [76, 1.5, 73, .5, 69, 2], [75, 1, 72, 1, 69, 1, 66, 1],
               [67, .5, 71, .5, 74, .5, 78, .5, 76, 2], [74, 1.5, 73, .5, None, 2]],
        "A2_end": [74, 1, 76, 1, 74, 2],
        "B": [[83, 1.5, 81, .5, 78, 1, 74, 1], [76, 1.5, 73, .5, 69, 2], [71, .5, 74, .5, 78, .5, 81, .5, 79, 2],
              [78, 1, 76, 1, 73, 1, 69, 1], [71, 1, 74, 1, 78, 1, 81, 1], [81, 1, 78, 1, 75, 1, 72, 1],
              [71, 1.5, 74, .5, 76, 1, 78, 1], [76, 2, 74, 1, 76, 1]],
        "A3_end": [74, 1, 73, 1, 74, 2],
    },
    "intro": ["Dmaj9", "Em9", "Dmaj9", "A13sus"], "outro": ["Dmaj9", "Gmaj9", "Dmaj9", "Dmaj9"],
}
TUNES = (THEME, HALFTIME, LATE_SCORES)


def bars_of(tune):
    """The tune's bars: [(chord or (chord, chord), section)], intro, A A B A, coda."""
    a, b = tune["A"], tune["B"]
    return ([(c, "intro") for c in tune["intro"]] + [(c, "A1") for c in a] + [(c, "A2") for c in a]
            + [(c, "B") for c in b] + [(c, "A3") for c in a] + [(c, "outro") for c in tune["outro"]])


def melody_notes(tune, bar_s, beat_s):
    """The lead's notes as (start s, midi, duration s), section by section."""
    out = []
    m = tune["melody"]

    def lay(bar_index, cells):
        t = bar_index * bar_s
        for k in range(0, len(cells), 2):
            midi, beats = cells[k], cells[k + 1]
            out.append((t, midi, beats * beat_s * (0.96 if midi else 1)))
            t += beats * beat_s

    first = len(tune["intro"])
    for section, offset in (("A1", 0), ("A2", 8), ("B", 16), ("A3", 24)):
        bars = m["B"] if section == "B" else m["A1"]
        for k, cells in enumerate(bars):
            if k == 7 and section in ("A2", "A3"):
                cells = m[f"{section}_end"]
            lay(first + offset + k, cells)
    return out


def render_tune(tune):
    beat = 60.0 / tune["bpm"]
    bar = 4 * beat
    bars = bars_of(tune)
    length = len(bars) * bar + 4.0
    desk = Desk(length)
    groove = np.random.default_rng(zlib.crc32(tune["title"].encode()))
    names = [c for chords in bars for c in (chords[0] if isinstance(chords[0], tuple) else (chords[0],))]
    voiced = iter(voicings(names))
    previous_bass = None
    piano = np.zeros(desk.n)
    nylon = np.zeros(desk.n)
    for index, (chords, section) in enumerate(bars):
        t0 = index * bar
        halves = chords if isinstance(chords, tuple) else (chords,)
        span = bar / len(halves)
        coda = section == "outro" and index == len(bars) - 1
        for h, name in enumerate(halves):
            at = t0 + h * span
            notes = next(voiced)
            vel = 0.55 if section in ("intro", "outro") else 0.7
            # the piano: a chord on the beat, a push before the next
            if tune["style"] == "smooth":
                hits = [(0.0, span * 0.55)] if coda else [(0.0, span * 0.42), (span * 0.875, span * 0.12 + 0.05)]
                if len(halves) == 1 and not coda and groove.random() < 0.5:
                    hits = [(0.0, beat * 1.4), (beat * 2.5, beat * 1.2)]
                for when, gate in hits:
                    for k, m in enumerate(notes):
                        x = epiano(m, gate if not coda else 3.0, vel * (0.85 + 0.15 * groove.random()))
                        a = int((at + when + k * 0.006) * SR)
                        piano[a:a + len(x)] += x[:max(0, desk.n - a)]
                if section != "intro" or index >= 2:
                    desk.add(pad(notes, span if not coda else 3.5, 0.5), at, 0.5 if tune["lead"] == "sax" else 0.65,
                             0.0, verb=0.35)
            else:   # bossa: the guitar's pattern, the piano held under it softly
                for when in ((0.0, 1.5, 2.5, 3.0) if len(halves) == 1 else (0.0, 1.0))[: (1 if coda else None)]:
                    for k, m in enumerate(notes):
                        x = guitar(m, beat * (0.9 if not coda else 4), 0.55 + 0.1 * groove.random())
                        a = int((at + when * beat + k * 0.012) * SR)
                        nylon[a:a + len(x)] += x[:max(0, desk.n - a)]
                for k, m in enumerate(notes):
                    x = epiano(m, span * 0.9, 0.35)
                    a = int((at + k * 0.004) * SR)
                    piano[a:a + len(x)] += x[:max(0, desk.n - a)]
            # the bass
            root = bass_root(name)
            fifth = root + 7 if root + 7 <= 50 else root - 5
            if coda:
                desk.add(fretless(root, 3.5, 0.8, previous_bass), at, 1.0, 0.0, verb=0.05)
            elif tune["style"] == "smooth":
                pattern = [(0.0, root, 1.4), (1.5, root + 12 if groove.random() < 0.3 else fifth, 0.45),
                           (2.0, fifth, 0.9)] if len(halves) == 1 else [(0.0, root, 1.8)]
                for when, m, gate in pattern:
                    # now and then the fretless slides into a note from the one before
                    slide = previous_bass if previous_bass is not None and abs(m - previous_bass) <= 5 \
                        and groove.random() < 0.3 else None
                    desk.add(fretless(m, gate * beat, 0.75, slide), at + when * beat, 1.0, 0.0, verb=0.05)
                    previous_bass = m
                if len(halves) == 1:   # walk into the next bar
                    desk.add(fretless(root - 1 if groove.random() < 0.5 else root + 2, beat * 0.45, 0.6), at + 3.5 * beat,
                             0.9, 0.0, verb=0.05)
            else:
                pattern = [(0.0, root, 1.4), (1.5, fifth, 0.45), (2.0, fifth, 1.4), (3.5, root, 0.45)] \
                    if len(halves) == 1 else [(0.0, root, 1.4), (1.5, fifth, 0.45)]
                for when, m, gate in pattern:
                    desk.add(fretless(m, gate * beat, 0.7), at + when * beat, 1.0, 0.0, verb=0.05)
            previous_bass = root
        # the drums: soft, a machine played gently
        if section in ("intro",) and index < 2 or coda:
            if coda:
                desk.add(ride(0.5), t0, 0.7, 0.3, verb=0.3)
            continue
        jitter = lambda: groove.normal(0, 0.004)  # noqa: E731
        for b in range(4):
            tb = t0 + b * beat
            if tune["style"] == "smooth":
                if b in (0, 2):
                    desk.add(kick(0.7 if b == 0 else 0.5), tb + jitter(), 0.9, 0.0, verb=0.02)
                if b in (1, 3):
                    desk.add(rim(0.5 + 0.1 * groove.random()), tb + jitter(), 0.8, -0.15, verb=0.2)
                    if section == "B":
                        desk.add(brush(0.5), tb + jitter(), 0.6, 0.1, verb=0.25)
                for e in (0, 0.5):
                    desk.add(hat(0.45 if e else 0.6, open_=(b == 3 and e and groove.random() < 0.3)),
                             tb + e * beat + jitter(), 0.55, 0.35, verb=0.1)
                for s in (0.25, 0.75):
                    desk.add(shaker(0.35), tb + s * beat + jitter(), 0.6, -0.4, verb=0.1)
            else:
                clave = {(0, 0), (1, 0.5), (3, 0)} if index % 2 == 0 else {(1, 0), (2, 0.5)}
                for e in (0, 0.5):
                    if (b, e) in clave:
                        desk.add(rim(0.55), tb + e * beat + jitter(), 0.85, -0.2, verb=0.2)
                    for s in (0, 0.25):
                        desk.add(shaker(0.4 if s else 0.5), tb + (e + s) * beat + jitter(), 0.6, 0.35, verb=0.1)
                if b in (0, 2):
                    desk.add(kick(0.55), tb + jitter(), 0.85, 0.0, verb=0.02)
                if b in (1, 3):
                    desk.add(kick(0.35), tb + 0.5 * beat + jitter(), 0.7, 0.0, verb=0.02)
            if section == "B" and b == 0 and tune["style"] == "smooth":
                desk.add(ride(0.35), tb, 0.5, 0.3, verb=0.2)
    wide = chorus(piano)
    desk.dry += wide * 0.55
    desk.wet += wide * 0.55 * 0.25
    if nylon.any():
        wide = chorus(nylon, depth=0.0015, rate=0.5)
        desk.dry += wide * np.array([[0.75], [0.45]])
        desk.wet += wide * 0.15
    # the lead
    notes = melody_notes(tune, bar, beat)
    if tune["lead"] == "vibes":
        lead = np.zeros(desk.n)
        for start, m, dur in notes:
            if m is not None:
                x = vibes(m, dur, 0.8)
                a = int(start * SR)
                lead[a:a + len(x)] += x[:max(0, desk.n - a)]
        desk.add(lead, 0.0, 1.05, -0.1, verb=0.3)
    else:
        lead = Line(tune["lead"]).render(notes, length)
        desk.add(lead, 0.0, 0.95 if tune["lead"] == "sax" else 1.0, 0.1, verb=0.28)
    # the station's logo on the vibes: open in the intro, closed at the end
    if tune.get("logo_in"):
        for k, (offset, m) in enumerate(zip((0, 0.5, 1.0, 2.0), tune["logo_in"])):
            desk.add(vibes(m, beat * (0.5 if k < 3 else 2.5), 0.8), 2 * bar + offset * beat, 0.9, -0.3, verb=0.4)
    if tune.get("logo_out"):
        start = (len(bars) - 2) * bar
        for k, (offset, m) in enumerate(zip((0, 0.5, 1.0, 2.0), tune["logo_out"])):
            desk.add(vibes(m - 2, beat * (0.5 if k < 3 else 3.5), 0.8), start + offset * beat, 0.9, 0.3, verb=0.45)
    mix = desk.mix()
    tail = times(mix.shape[1])
    mix *= np.clip((length - tail) / 2.5, 0, 1)
    return mix


def master(tunes):
    """The tunes back to back with a breath between, as a listings channel's tape ran;
    band-limited, a little tape hiss and wow, levelled softly."""
    gap = np.zeros((2, int(0.8 * SR)))
    pieces = []
    for mix in tunes:
        pieces += [mix, gap]
    x = np.concatenate(pieces, axis=1)
    n = x.shape[1]
    t = times(n)
    # wow: a slow, slight pitch drift (a modulated read position)
    pos = np.arange(n) + 0.0009 * SR * np.sin(2 * np.pi * 0.55 * t) / (2 * np.pi * 0.55)
    lo = np.clip(np.floor(pos).astype(int), 0, n - 1)
    hi = np.clip(lo + 1, 0, n - 1)
    frac = pos - np.floor(pos)
    x = x[:, lo] * (1 - frac) + x[:, hi] * frac
    sos = signal.butter(4, [45, 12500], "band", fs=SR, output="sos")
    x = signal.sosfilt(sos, x, axis=1)
    x += signal.sosfilt(signal.butter(2, 3000, "high", fs=SR, output="sos"), rng.standard_normal((2, n))) * 0.0009
    # a gentle bus compressor: slow RMS, 2:1 above the threshold
    rms = np.sqrt(signal.sosfilt(signal.butter(1, 3, "low", fs=SR, output="sos"), np.mean(x ** 2, axis=0)) + 1e-12)
    level = 20 * np.log10(rms + 1e-9)
    threshold = np.percentile(level, 80)
    gain = 10 ** (-np.maximum(0, level - threshold) * 0.5 / 20)
    x = x * gain
    return x / (np.abs(x).max() + 1e-9) * 0.89


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--wav", type=Path, help="also keep the unlevelled WAV here")
    args = parser.parse_args(argv)
    mixes = []
    for tune in TUNES:
        mixes.append(render_tune(tune))
        print(f"  {tune['title']}: {mixes[-1].shape[1] / SR:.1f} s", flush=True)
    x = master(mixes)
    with tempfile.TemporaryDirectory() as folder:
        wav = Path(args.wav) if args.wav else Path(folder) / "guide.wav"
        wavfile.write(wav, SR, (x.T * 32767).astype(np.int16))
        partial = args.out.with_name("." + args.out.name)
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(wav),
                        "-af", f"loudnorm=I={LOUDNESS}:TP=-2:LRA=11", "-ar", "48000",
                        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", "-f", "mp4", str(partial)],
                       check=True)
    seconds = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                    str(partial)], capture_output=True, text=True, check=True).stdout)
    partial.replace(args.out)
    args.out.with_suffix(".json").write_text(json.dumps(
        {"seconds": round(seconds, 3), "tunes": [t["title"] for t in TUNES], "loudness": LOUDNESS}, indent=2) + "\n")
    print(f"  wrote {args.out} ({seconds:.1f} s, {args.out.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
