"""Bounded, local Cubism motion synthesis for the recovered Nene model.

Only model inputs are owned here. Clothing, body/physics outputs, sound, text,
and Viewer events are deliberately absent. Ranges were checked against
``data/cubism-recovery/blendshape-core-effects-20261019-001.json``; expression
poses come from the existing ``assets/motions`` files. The result is a single
non-looping motion: a renderer must replace it on interruption and schedule new
idle motions separately. No speech is generated, played, stored, or repeated.

This is amplitude-driven mouth opening, not Japanese phoneme recognition.
"""

from __future__ import annotations

from array import array
from bisect import bisect_right
from functools import lru_cache
import json
import math
from pathlib import Path
import random
import sys
import wave


MOTION_ROOT = Path(__file__).resolve().parents[2] / "assets" / "motions"
MOTION_NAMES = {
    "平静": "calm", "高兴": "happy", "害羞": "shy", "生气": "angry",
    "惊讶": "surprised", "着急": "worried",
}
PHASES = frozenset({"idle", "listening", "thinking", "speaking", "paused"})
FPS = 30
MIN_DURATION = 0.1
MAX_DURATION = 120.0
MAX_FRAME_COUNT = int(MAX_DURATION * FPS) + 1

# A narrow allowlist, rather than every parameter in the editable model.
PARAMETER_RANGES = {
    "ParamAngleX": (-30.0, 30.0),
    "ParamAngleY": (-30.0, 30.0),
    "ParamAngleZ": (-30.0, 30.0),
    "ParamEyeBallX": (-1.0, 1.0),
    "ParamEyeBallY": (-1.0, 1.0),
    "ParamEyeLOpen": (0.0, 1.0),
    "ParamEyeROpen": (0.0, 1.0),
    "ParamEyeLSmile": (0.0, 1.0),
    "ParamEyeRSmile": (0.0, 1.0),
    "ParamBrowLY": (-1.0, 1.0),
    "ParamBrowRY": (-1.0, 1.0),
    "ParamBrowLAngle": (-1.0, 1.0),
    "ParamBrowRAngle": (-1.0, 1.0),
    "ParamMouthForm": (-1.0, 1.0),
    "ParamMouthOpenY": (0.0, 1.0),
    "ParamCheek": (0.0, 1.0),
    "ParamBreath": (0.0, 1.0),
}
FACE_PARAMETERS = (
    "ParamEyeLOpen", "ParamEyeROpen", "ParamEyeLSmile", "ParamEyeRSmile",
    "ParamBrowLY", "ParamBrowRY", "ParamBrowLAngle", "ParamBrowRAngle",
    "ParamMouthForm", "ParamCheek",
)


def _number(value: object, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} 必须是有限数字") from exc
    if not math.isfinite(result):
        raise ValueError(f"{label} 必须是有限数字")
    return result


def _clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def _smoothstep(value: float) -> float:
    value = _clamp(value, 0.0, 1.0)
    return value * value * (3.0 - 2.0 * value)


@lru_cache(maxsize=6)
def _face_pose(emotion: str) -> tuple[tuple[str, float], ...]:
    filename = f"{MOTION_NAMES[emotion]}.motion3.json"
    try:
        document = json.loads((MOTION_ROOT / filename).read_text(encoding="utf-8-sig"))
        values = {
            curve["Id"]: _number(curve["Segments"][1], "表情参数")
            for curve in document["Curves"]
            if curve.get("Target") == "Parameter" and curve.get("Id") in FACE_PARAMETERS
        }
        if set(values) != set(FACE_PARAMETERS):
            raise ValueError("缺少眉眼或嘴形参数")
        for name, value in values.items():
            low, high = PARAMETER_RANGES[name]
            if not low <= value <= high:
                raise ValueError("表情参数越界")
    except (OSError, ValueError, TypeError, KeyError, IndexError) as exc:
        raise ValueError(f"Live2D 表情模板不可用：{filename}") from exc
    return tuple(values.items())


def _decode_pcm(data: bytes, width: int) -> list[int] | array:
    if width == 1:
        return [value - 128 for value in data]
    if width in (2, 4):
        samples = array("h" if width == 2 else "i")
        samples.frombytes(data)
        if sys.byteorder != "little":
            samples.byteswap()
        return samples
    # PCM24 is signed little endian. Python's wave does not decode IEEE float.
    return [int.from_bytes(data[index:index + 3], "little", signed=True)
            for index in range(0, len(data), 3)]


def _audio_envelope(path: Path, duration: float) -> tuple[list[float], list[float], float]:
    """Read only the needed prefix; keep memory bounded even for long WAVs.

    Stereo energy is combined before taking RMS, so opposite-phase channels
    cannot cancel each other. A smooth noise gate and attack/release filter
    suppress hiss/chatter while retaining actual pauses. No cyclic indexing.
    """
    rms: list[float] = []
    ends: list[float] = []
    try:
        with wave.open(str(path), "rb") as source:
            channels, width, rate, frames = (
                source.getnchannels(), source.getsampwidth(),
                source.getframerate(), source.getnframes(),
            )
            if source.getcomptype() != "NONE" or channels not in (1, 2):
                raise ValueError("需要未压缩单声道或双声道 PCM WAV")
            if width not in (1, 2, 3, 4) or not 8_000 <= rate <= 192_000:
                raise ValueError("需要 8/16/24/32 位整数 PCM WAV，采样率 8000..192000 Hz")
            if frames <= 0:
                raise ValueError("WAV 中没有音频帧")
            frame_bytes = width * channels
            limit = min(frames, int(math.ceil(duration * rate)))
            chunk_frames = max(1, int(rate * 0.01))
            position = 0
            scale = float(1 << (width * 8 - 1))
            while position < limit:
                amount = min(chunk_frames, limit - position)
                data = source.readframes(amount)
                if len(data) != amount * frame_bytes:
                    raise ValueError("WAV 音频数据被截断")
                samples = _decode_pcm(data, width)
                rms.append(math.sqrt(sum(value * value for value in samples) / len(samples)) / scale)
                position += amount
                ends.append(min(position / rate, duration))
            audio_end = min(frames / rate, duration)
    except (OSError, EOFError, wave.Error, TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"Live2D 口型无法读取 WAV：{exc}") from exc

    ordered = sorted(rms)
    audible = [value for value in ordered if value >= 0.008]
    # A sub-gate recording must stay closed; normalizing quiet noise would make
    # an apparently speaking mouth out of silence.
    if not audible:
        return [0.0, audio_end], [0.0, 0.0], audio_end
    # Estimate loudness from audible bins, so a brief word surrounded by long
    # silence does not vanish below the recording-wide 95th percentile.
    peak = audible[min(len(audible) - 1, int(len(audible) * 0.95))]
    low_energy = ordered[int(len(ordered) * 0.15)]
    gate = max(0.004, min(0.014, low_energy * 1.5, peak * 0.15))
    values = [0.0]
    times = [0.0]
    previous_time = 0.0
    level = 0.0
    for end, energy in zip(ends, rms):
        opening = min(0.9, 0.9 * math.sqrt(energy / peak))
        target = opening * _smoothstep((energy - gate) / gate)
        time_constant = 0.022 if target > level else 0.035
        level += (target - level) * (1.0 - math.exp(-(end - previous_time) / time_constant))
        if target == 0.0 and level < 0.012:
            level = 0.0
        times.append(end)
        values.append(level)
        previous_time = end
    # The WAV's end is a hard boundary: never hold an open mouth after it.
    values[-1] = 0.0
    return times, values, audio_end


def _sample(times: list[float], values: list[float], time: float) -> float:
    if time <= times[0]:
        return values[0]
    if time >= times[-1]:
        return values[-1]
    index = bisect_right(times, time) - 1
    fraction = (time - times[index]) / (times[index + 1] - times[index])
    return values[index] + (values[index + 1] - values[index]) * fraction


def _blink_centers(rng: random.Random, duration: float) -> list[float]:
    centers = []
    next_blink = rng.uniform(1.5, 3.2)
    while next_blink + 0.15 < duration:
        # Put complete closure on a frame; small expressions must still blink.
        centers.append(round(next_blink * FPS) / FPS)
        next_blink += rng.uniform(2.6, 5.4)
    return centers


def _blink(time: float, centers: list[float]) -> float:
    index = bisect_right(centers, time)
    for center in centers[max(0, index - 1):index + 1]:
        delta = time - center
        if -0.085 < delta <= 0.02:
            return _smoothstep((-delta - 0.015) / 0.07)
        if 0.02 < delta < 0.14:
            return _smoothstep((delta - 0.02) / 0.12)
    return 1.0


def _gaze_knots(rng: random.Random, duration: float, phase: str) -> tuple[list[float], list[float], list[float]]:
    times, xs, ys = [0.0], [0.0], [0.0]
    while times[-1] < duration:
        times.append(min(duration, times[-1] + rng.uniform(2.2, 4.3)))
        xs.append(rng.uniform(-0.065, 0.065))
        ys.append(rng.uniform(-0.04, 0.04))
    if phase == "thinking":
        # One modest look-away, returning to the requested target. This is a
        # local performance cue, not a claim about what the character inferred.
        xs = [0.0] + [0.15 + value for value in xs[1:-1]] + [0.0]
        ys = [0.0] + [-0.11 + value for value in ys[1:-1]] + [0.0]
    return times, xs, ys


def _smooth_sample(times: list[float], values: list[float], time: float) -> float:
    if time >= times[-1]:
        return values[-1]
    index = max(0, bisect_right(times, time) - 1)
    fraction = _smoothstep((time - times[index]) / (times[index + 1] - times[index]))
    return values[index] + (values[index + 1] - values[index]) * fraction


def _curve(name: str, times: list[float], values: list[float]) -> dict:
    low, high = PARAMETER_RANGES[name]
    points = [(time, round(_clamp(value, low, high), 6)) for time, value in zip(times, values)]
    # Remove constant interiors (not their edges), leaving the exact same
    # linear curve and greatly reducing static expression file size.
    compact = [points[0]]
    for index in range(1, len(points) - 1):
        if points[index - 1][1] == points[index][1] == points[index + 1][1]:
            continue
        compact.append(points[index])
    compact.append(points[-1])
    segments: list[float | int] = [compact[0][0], compact[0][1]]
    for time, value in compact[1:]:
        segments.extend([0, time, value])
    result = {"Target": "Parameter", "Id": name, "Segments": segments}
    if name == "ParamMouthOpenY":
        # Cancelling speech replaces this curve with zero immediately; the
        # brow/head curves can still use the motion's gentle global fade.
        result.update({"FadeInTime": 0.0, "FadeOutTime": 0.0})
    return result


def compose_motion(
    *, emotion: str = "平静", duration: float = 8.0, phase: str = "idle",
    audio_path: Path | None = None, attention: tuple[float, float] = (0, 0),
    seed: int = 0, strength: float = 0.65,
) -> dict:
    """Compose one standard motion3 object, without changing external state.

    ``duration`` must be 0.1..120 seconds. ``attention`` is a bounded target in
    model coordinates (X right, Y up), not inferred eye contact with a person.
    Finite strength/attention are clamped to [0,1]/[-1,1]. Unknown emotions use
    calm. Audio is read only in speaking phase, supported integer PCM WAV only;
    its failure raises ValueError so callers can retain sound and use a closed
    mouth fallback. Last keyframes equal Meta.Duration. The result never loops.
    """
    duration = _number(duration, "duration")
    if not MIN_DURATION <= duration <= MAX_DURATION:
        raise ValueError("duration 必须在 0.1..120 秒之间")
    if phase not in PHASES:
        raise ValueError(f"不支持的 Live2D phase：{phase}")
    strength = _clamp(_number(strength, "strength"), 0.0, 1.0)
    if not isinstance(attention, (tuple, list)) or len(attention) != 2:
        raise ValueError("attention 必须包含 X、Y 两个有限数字")
    target_x, target_y = [_clamp(_number(value, "attention"), -1.0, 1.0) for value in attention]
    if not isinstance(seed, int):
        raise ValueError("seed 必须是整数")
    rng = random.Random(seed)
    emotion = emotion if emotion in MOTION_NAMES else "平静"
    calm, pose = dict(_face_pose("平静")), dict(_face_pose(emotion))
    face = {name: calm[name] + strength * (pose[name] - calm[name]) for name in FACE_PARAMETERS}
    steps = int(math.ceil(duration * FPS))
    times = [round(index / FPS, 6) for index in range(steps)] + [duration]
    blinks = _blink_centers(rng, duration)
    gaze_times, gaze_xs, gaze_ys = _gaze_knots(rng, duration, phase)
    offsets = [rng.uniform(0, math.tau) for _ in range(4)]
    audio_times, audio_values, audio_end = [0.0, duration], [0.0, 0.0], 0.0
    if phase == "speaking" and audio_path is not None:
        audio_times, audio_values, audio_end = _audio_envelope(Path(audio_path), duration)
    values: dict[str, list[float]] = {name: [] for name in PARAMETER_RANGES}
    amplitudes = {"idle": 0.55, "listening": 0.32, "thinking": 0.45, "speaking": 0.65, "paused": 0.0}
    amplitude = amplitudes[phase]
    for time in times:
        blink = _blink(time, blinks)
        for name in FACE_PARAMETERS:
            values[name].append(face[name] * blink if name in ("ParamEyeLOpen", "ParamEyeROpen") else face[name])
        mouth = _sample(audio_times, audio_values, time) if time < audio_end else 0.0
        values["ParamMouthOpenY"].append(mouth)
        if phase == "paused":
            gaze_x, gaze_y = target_x * 0.55, target_y * 0.45
            head_x = head_y = head_z = 0.0
        else:
            gaze_x = target_x * 0.55 + _smooth_sample(gaze_times, gaze_xs, time)
            gaze_y = target_y * 0.45 + _smooth_sample(gaze_times, gaze_ys, time)
            head_x = target_x * 2.0 + amplitude * math.sin(time * 0.66 + offsets[0])
            head_y = target_y * 1.4 + amplitude * 0.65 * math.sin(time * 0.92 + offsets[1])
            head_z = amplitude * 0.7 * math.sin(time * 0.49 + offsets[2])
            if phase == "listening":
                head_z += 0.55
            if phase == "speaking":
                # Restrained articulation emphasis tied to actual audio energy.
                head_y -= mouth * 0.65
        values["ParamEyeBallX"].append(gaze_x)
        values["ParamEyeBallY"].append(gaze_y)
        values["ParamAngleX"].append(head_x)
        values["ParamAngleY"].append(head_y)
        values["ParamAngleZ"].append(head_z)
        if phase == "paused":
            # The renderer can safely loop only a silent paused motion. Close
            # the breath cycle at the exact duration to avoid a loop seam.
            cycles = max(1, round(duration / 4.7))
            breath = (0.5 if duration < 2.4 else
                      0.5 + 0.18 * math.sin((time / duration * cycles) * math.tau + offsets[3]))
        else:
            breath = 0.5 + 0.18 * math.sin(time * math.tau / 4.7 + offsets[3])
        values["ParamBreath"].append(breath)
    curves = [_curve(name, times, points) for name, points in values.items()]
    segment_count = sum((len(curve["Segments"]) - 2) // 3 for curve in curves)
    return {
        "Version": 3,
        "Meta": {
            "Duration": duration, "Fps": float(FPS), "Loop": False,
            "AreBeziersRestricted": True, "CurveCount": len(curves),
            "TotalSegmentCount": segment_count,
            "TotalPointCount": segment_count + len(curves),
            "UserDataCount": 0, "TotalUserDataSize": 0,
            "FadeInTime": 0.15, "FadeOutTime": 0.15,
        },
        "Curves": curves,
    }


def motion_window(motion: dict, start_seconds: float, duration: float = 2.0) -> dict:
    """Rebase a generated linear motion onto the audio processing clock.

    Short windows bound ViewerEX drift without pretending to read its clock.
    Only this module's linear parameter curves are accepted; no sound/events.
    """
    start = _number(start_seconds, "start_seconds")
    length = _number(duration, "duration")
    total = _number(motion["Meta"]["Duration"], "motion duration")
    if start < 0 or length < MIN_DURATION or not MIN_DURATION <= total <= MAX_DURATION:
        raise ValueError("Invalid motion window")
    ended = start >= total
    start = total if ended else min(start, max(0.0, total - MIN_DURATION))
    end = min(total, start + min(length, MAX_DURATION))
    window_duration = min(length, MAX_DURATION) if ended else end - start
    curves = []
    for curve in motion["Curves"]:
        if curve.get("Target") != "Parameter" or curve.get("Id") not in PARAMETER_RANGES:
            raise ValueError("Unsupported curve in motion window")
        segments = curve["Segments"]
        if len(segments) < 5 or (len(segments) - 2) % 3 or any(segments[i] != 0 for i in range(2, len(segments), 3)):
            raise ValueError("Only generated linear motion curves are supported")
        times = [segments[0], *segments[3::3]]
        values = [segments[1], *segments[4::3]]
        if ended:
            # Holding the endpoint must never replay the preceding audio or
            # open the mouth again when an EOF callback arrives slightly late.
            window_times = [0.0, window_duration]
            window_values = [_sample(times, values, total)] * 2
        else:
            window_times = [0.0, *[t - start for t in times if start < t < end], window_duration]
            window_values = [_sample(times, values, t + start) for t in window_times]
        curves.append(_curve(curve["Id"], window_times, window_values))
    count = sum((len(c["Segments"]) - 2) // 3 for c in curves)
    return {"Version": 3, "Meta": {**motion["Meta"], "Duration": window_duration,
        "Loop": False, "CurveCount": len(curves), "TotalSegmentCount": count,
        "TotalPointCount": count + len(curves), "UserDataCount": 0,
        "TotalUserDataSize": 0, "FadeInTime": 0.03, "FadeOutTime": 0.03}, "Curves": curves}
