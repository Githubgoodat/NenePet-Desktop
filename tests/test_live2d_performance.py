from __future__ import annotations

import json
import math
from pathlib import Path
import struct
from tempfile import TemporaryDirectory
import unittest
import wave

from aipet.core.live2d_performance import (
    MAX_FRAME_COUNT, MOTION_NAMES, PARAMETER_RANGES, PHASES, compose_motion,
)


def points(curve: dict) -> list[tuple[float, float]]:
    segments = curve["Segments"]
    return [(segments[0], segments[1])] + [
        (segments[index + 1], segments[index + 2])
        for index in range(2, len(segments), 3)
    ]


def curves(motion: dict) -> dict[str, dict]:
    return {curve["Id"]: curve for curve in motion["Curves"]}


def at(curve: dict, time: float) -> float:
    values = points(curve)
    for (start, low), (end, high) in zip(values, values[1:]):
        if time <= end:
            return low + (high - low) * max(0.0, (time - start) / (end - start))
    return values[-1][1]


def write_wav(path: Path, sample, *, duration: float = 1.5, rate: int = 16_000,
              channels: int = 1, width: int = 2) -> None:
    data = bytearray()
    limit = (1 << (width * 8 - 1)) - 1
    for index in range(int(duration * rate)):
        amplitude = sample(index / rate)
        for channel in range(channels):
            level = amplitude[channel] if isinstance(amplitude, tuple) else amplitude
            value = round(max(-1.0, min(1.0, level)) * limit)
            if width == 1:
                data.append(value + 128)
            else:
                data.extend(value.to_bytes(width, "little", signed=True))
    with wave.open(str(path), "wb") as output:
        output.setparams((channels, width, rate, 0, "NONE", "not compressed"))
        output.writeframes(data)


def phrase(time: float) -> float:
    amplitude = 0.3 if 0.2 <= time < 0.6 else 0.1 if 0.95 <= time < 1.2 else 0.0
    return amplitude * math.sin(time * math.tau * 440)


class Live2DPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_motion3_linear_schema_counts_and_exact_end_times(self):
        motion = compose_motion(duration=7.1234567)
        meta = motion["Meta"]
        self.assertEqual(motion["Version"], 3)
        self.assertFalse(meta["Loop"])
        self.assertEqual(meta["CurveCount"], len(motion["Curves"]))
        segment_count = 0
        point_count = 0
        for curve in motion["Curves"]:
            self.assertEqual(curve["Target"], "Parameter")
            segments = curve["Segments"]
            self.assertEqual((len(segments) - 2) % 3, 0)
            self.assertTrue(all(segments[index] == 0 for index in range(2, len(segments), 3)))
            ps = points(curve)
            self.assertEqual(ps[0][0], 0.0)
            self.assertEqual(ps[-1][0], meta["Duration"])
            self.assertTrue(all(a[0] < b[0] for a, b in zip(ps, ps[1:])))
            segment_count += len(ps) - 1
            point_count += len(ps)
        self.assertEqual(meta["TotalSegmentCount"], segment_count)
        self.assertEqual(meta["TotalPointCount"], point_count)
        self.assertEqual(meta["UserDataCount"], 0)

    def test_only_confirmed_input_allowlist_never_physics_outfit_or_events(self):
        expected = {
            "ParamAngleX", "ParamAngleY", "ParamAngleZ", "ParamEyeBallX", "ParamEyeBallY",
            "ParamEyeLOpen", "ParamEyeROpen", "ParamEyeLSmile", "ParamEyeRSmile",
            "ParamBrowLY", "ParamBrowRY", "ParamBrowLAngle", "ParamBrowRAngle",
            "ParamMouthForm", "ParamMouthOpenY", "ParamCheek", "ParamBreath",
        }
        for phase in PHASES:
            motion = compose_motion(phase=phase)
            self.assertEqual(set(curves(motion)), expected)
            self.assertNotIn("UserData", motion)
            self.assertNotIn("Sound", motion)
            self.assertNotIn("Events", motion)

    def test_all_emotions_and_phases_stay_finite_in_verified_ranges(self):
        for emotion in MOTION_NAMES:
            for phase in PHASES:
                with self.subTest(emotion=emotion, phase=phase):
                    motion = compose_motion(emotion=emotion, phase=phase, attention=(90, -90), strength=3)
                    for name, curve in curves(motion).items():
                        low, high = PARAMETER_RANGES[name]
                        for time, value in points(curve):
                            self.assertTrue(math.isfinite(time) and math.isfinite(value))
                            self.assertLessEqual(low, value)
                            self.assertLessEqual(value, high)

    def test_strength_blends_expression_toward_calm_not_a_hard_switch(self):
        calm = curves(compose_motion(emotion="平静", seed=3))
        mild = curves(compose_motion(emotion="生气", strength=0.25, seed=3))
        full = curves(compose_motion(emotion="生气", strength=1.0, seed=3))
        none = curves(compose_motion(emotion="生气", strength=0.0, seed=3))
        self.assertEqual(none, calm)
        for name in ("ParamBrowLY", "ParamBrowRAngle", "ParamMouthForm", "ParamEyeLOpen"):
            self.assertAlmostEqual(at(mild[name], 0.0),
                                   at(calm[name], 0.0) * 0.75 + at(full[name], 0.0) * 0.25)

    def test_unknown_emotion_uses_calm(self):
        self.assertEqual(compose_motion(emotion="unsupported"), compose_motion(emotion="平静"))

    def test_blink_reaches_complete_closure_even_with_partial_smile(self):
        for emotion in MOTION_NAMES:
            motion = curves(compose_motion(emotion=emotion, duration=8.0))
            left = points(motion["ParamEyeLOpen"])
            right = points(motion["ParamEyeROpen"])
            self.assertEqual(left, right)
            self.assertEqual(min(value for _, value in left), 0.0)
            self.assertGreater(max(value for _, value in left), 0.1)

    def test_paused_only_blink_breath_vary_and_loop_seam_is_closed(self):
        motion = curves(compose_motion(emotion="生气", duration=8, phase="paused", attention=(0.7, -0.4)))
        variable = set()
        for name, curve in motion.items():
            values = [value for _, value in points(curve)]
            self.assertEqual(values[0], values[-1], name)
            if max(values) - min(values) > 0.0001:
                variable.add(name)
        self.assertEqual(variable, {"ParamEyeLOpen", "ParamEyeROpen", "ParamBreath"})
        self.assertEqual(at(motion["ParamMouthOpenY"], 0), 0)
        self.assertEqual(at(motion["ParamBrowLAngle"], 0), -0.4225)

    def test_attention_is_bounded_target_and_paused_has_no_search(self):
        left = curves(compose_motion(attention=(-1, -1), phase="listening"))
        right = curves(compose_motion(attention=(1, 1), phase="listening"))
        self.assertLess(at(left["ParamEyeBallX"], 0), 0)
        self.assertGreater(at(right["ParamEyeBallY"], 0), 0)
        self.assertLess(abs(at(right["ParamAngleX"], 0)), 4)
        self.assertLessEqual(max(v for _, v in points(right["ParamEyeBallX"])), 0.7)

    def test_seed_reproduces_motion_without_consuming_global_random(self):
        import random
        original = random.getstate()
        first = compose_motion(seed=101)
        self.assertEqual(random.getstate(), original)
        self.assertEqual(first, compose_motion(seed=101))
        self.assertNotEqual(first, compose_motion(seed=102))

    def test_speaking_mouth_follows_actual_rhythm_and_volume(self):
        path = self.root / "phrase.wav"
        write_wav(path, phrase)
        mouth = curves(compose_motion(phase="speaking", duration=2.0, audio_path=path))["ParamMouthOpenY"]
        self.assertEqual(at(mouth, 0.1), 0)
        self.assertGreater(at(mouth, 0.4), 0.6)
        self.assertEqual(at(mouth, 0.8), 0)
        self.assertGreater(at(mouth, 1.1), 0.3)
        self.assertGreater(at(mouth, 0.4), at(mouth, 1.1))
        self.assertEqual(at(mouth, 1.45), 0)
        self.assertEqual(at(mouth, 1.8), 0)

    def test_audio_never_repeats_and_first_last_mouth_frames_are_closed(self):
        path = self.root / "short.wav"
        write_wav(path, lambda t: 0.4 * math.sin(math.tau * 400 * t), duration=0.3)
        mouth = curves(compose_motion(phase="speaking", duration=3, audio_path=path))["ParamMouthOpenY"]
        self.assertGreater(at(mouth, 0.15), 0.6)
        self.assertTrue(all(at(mouth, t) == 0 for t in (0, 0.3, 0.6, 1.2, 2.4, 3)))
        self.assertEqual(mouth["FadeInTime"], 0)
        self.assertEqual(mouth["FadeOutTime"], 0)

    def test_silence_and_quiet_noise_stay_fully_closed_even_surprised(self):
        for name, sample in (("silent", lambda t: 0.0),
                             ("noise", lambda t: 0.002 * math.sin(math.tau * 900 * t))):
            path = self.root / f"{name}.wav"
            write_wav(path, sample)
            mouth = curves(compose_motion(emotion="惊讶", phase="speaking", audio_path=path))["ParamMouthOpenY"]
            self.assertTrue(all(value == 0 for _, value in points(mouth)))

    def test_brief_word_in_long_silence_is_not_discarded_as_noise(self):
        path = self.root / "brief.wav"
        write_wav(path, lambda t: 0.3 * math.sin(math.tau * 400 * t) if 1 <= t < 1.1 else 0,
                  duration=4.0)
        mouth = curves(compose_motion(phase="speaking", duration=4, audio_path=path))["ParamMouthOpenY"]
        self.assertGreater(at(mouth, 1.066667), 0.5)
        self.assertEqual(at(mouth, 2), 0)

    def test_stereo_opposite_phase_does_not_cancel_voice_energy(self):
        path = self.root / "stereo.wav"
        write_wav(path, lambda t: (phrase(t), -phrase(t)), channels=2, rate=44_100)
        mouth = curves(compose_motion(phase="speaking", duration=2, audio_path=path))["ParamMouthOpenY"]
        self.assertGreater(at(mouth, 0.4), 0.6)
        self.assertEqual(at(mouth, 0.8), 0)

    def test_integer_pcm_widths_and_common_sample_rates(self):
        for width, rate in ((1, 8_000), (2, 22_050), (3, 48_000), (4, 32_000)):
            with self.subTest(width=width, rate=rate):
                path = self.root / f"pcm{width}.wav"
                write_wav(path, phrase, width=width, rate=rate)
                mouth = curves(compose_motion(phase="speaking", duration=2, audio_path=path))["ParamMouthOpenY"]
                self.assertGreater(at(mouth, 0.4), 0.5)
                self.assertEqual(at(mouth, 0.8), 0)

    def test_no_audio_or_nonspeaking_phase_never_opens_mouth_or_reads_file(self):
        for phase in PHASES:
            with self.subTest(phase=phase):
                kwargs = {} if phase == "speaking" else {"audio_path": self.root / "missing.wav"}
                mouth = curves(compose_motion(emotion="惊讶", phase=phase, **kwargs))["ParamMouthOpenY"]
                self.assertTrue(all(value == 0 for _, value in points(mouth)))

    def test_audio_decode_failures_are_explicit_value_errors(self):
        invalid = self.root / "invalid.wav"
        invalid.write_bytes(b"not a WAV")
        empty = self.root / "empty.wav"
        write_wav(empty, lambda t: 0, duration=0)
        truncated = self.root / "truncated.wav"
        write_wav(truncated, phrase)
        truncated.write_bytes(truncated.read_bytes()[:500])
        for path in (self.root / "missing.wav", invalid, empty, truncated):
            with self.subTest(path=path.name):
                with self.assertRaisesRegex(ValueError, "口型无法读取 WAV"):
                    compose_motion(phase="speaking", audio_path=path)

    def test_float_wav_and_multichannel_wav_are_rejected_clearly(self):
        multichannel = self.root / "three.wav"
        with wave.open(str(multichannel), "wb") as output:
            output.setparams((3, 2, 16_000, 0, "NONE", "not compressed"))
            output.writeframes(b"\0" * 6 * 160)
        float_wav = self.root / "float.wav"
        fmt = struct.pack("<HHIIHH", 3, 1, 16_000, 64_000, 4, 32)
        body = b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", 400) + b"\0" * 400
        float_wav.write_bytes(b"RIFF" + struct.pack("<I", len(body) + 4) + b"WAVE" + body)
        for path in (multichannel, float_wav):
            with self.assertRaisesRegex(ValueError, "口型无法读取 WAV"):
                compose_motion(phase="speaking", audio_path=path)

    def test_extreme_duration_frame_and_serialized_size_bounds(self):
        motion = compose_motion(duration=120, phase="thinking")
        self.assertLessEqual(max(len(points(c)) for c in motion["Curves"]), MAX_FRAME_COUNT)
        self.assertLess(len(json.dumps(motion, separators=(",", ":")).encode()), 2_000_000)
        self.assertTrue(all(points(c)[-1][0] == 120 for c in motion["Curves"]))
        short = compose_motion(duration=0.1)
        self.assertTrue(all(points(c)[-1][0] == 0.1 for c in short["Curves"]))

    def test_invalid_duration_or_nonfinite_inputs_raise_value_error(self):
        for duration in (-1, 0, 0.099, 120.1, float("nan"), float("inf"), "bad"):
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                compose_motion(duration=duration)
        for kwargs in ({"strength": float("nan")}, {"attention": (float("inf"), 0)},
                       {"attention": (0,)}, {"phase": "unknown"}, {"seed": "bad"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                compose_motion(**kwargs)


if __name__ == "__main__":
    unittest.main()
