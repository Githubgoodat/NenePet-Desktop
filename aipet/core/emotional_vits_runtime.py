"""Isolated CPU model and authenticated loopback protocol (Python 3.9).

Importing this module is lightweight. Only EmotionalVITSRuntime construction
imports Torch and the independently pinned inference sources.
"""
from __future__ import annotations

from collections import deque
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import secrets
import sys
import threading
from types import MappingProxyType
import wave

from aipet.core.emotional_vits_profile import (
    EmotionalVITSProfileError, PROFILE_KIND, load_emotional_vits_profile,
    verify_reference_vector,
)


COMMIT = "f94ba49845af6456e5708ec3889bcdceeb1b8ec5"
MAX_REQUEST_BYTES = 65536
MAX_TEXT_CHARACTERS = 500
MAX_SEQUENCE_LENGTH = 2000
MAX_LATENT_FRAMES = 1800
TARGET_NORMALIZED_LENGTH = 160
# Independent pins: editing source-manifest.json cannot authorize new code.
EXPECTED_ARTIFACTS = {
    "models.py": (19509, "7b0fd5e67ad62313a5368adcfae30df77788aa308d9dbce8644a15f4be7af1b6"),
    "modules.py": (13166, "f6514eaf7fab47d866ee0f9da94ca6ab6e45ebaf246151408da20dd14f242a75"),
    "attentions.py": (11780, "dc65085d0e5d67da9f7603c98efad1e27eaf42c572f632a58080a5cc0f577f92"),
    "commons.py": (4778, "f223dc1df791d2fb2953cfebd7014a7bc2a6ed614c05de6c17c7b44718a42cdd"),
    "transforms.py": (8490, "9d95e518610c35581c4f1c3b4e8eeca91b7c3d6899e7f0ebb86a0387394d3046"),
    "utils.py": (7885, "efb40a5b125e1ec80e6514adcd5b4715d52167ed06fe1594800e273cdd7b0009"),
    "text/__init__.py": (1664, "6c8cdc1974e32e14f66bd27fe8711b9ffac6a67dfd36844f57831e9ab67d0a29"),
    "text/cleaners.py": (7287, "70e90a322932e3a4bc0a41c725c3edeb0e7f624f670c0781cf9e4584b61cd88a"),
    "text/japanese.py": (5015, "3478b2f6ff6556896eb983a75cd38151f4e9b1105fc1a6735f89737b2e6c0e04"),
    "text/mandarin.py": (7831, "da78fdbaf8ca6eee7f4f0bc57d249cb0ca52f426290328334f8d3f8f4d0e8c6d"),
    "text/symbols.py": (2098, "6141c5e611b963221beb5a47b86dc40119b0946f2e0d49570e0409f7cdc0e3c3"),
    "configs/vtubers.json": (1283, "191e86109c616a7cbf08ecfea6c77f743656aa3adaf951153da034faa034be3e"),
    "LICENSE": (1069, "3d8165162cef96f686f02146ac2e4ae80db5797296a99c658befa424ee64727b"),
    "app.py": (4362, "b561274fe816f6bfe30ec50558be3a9be999586313f291093bc9db4377d6926e"),
    "requirements.txt": (121, "963fec8176c0eadc6b437091677e9916b81132f446dcb1f4dfa0430d454a430c"),
    "README.md": (229, "83f0a7984d3d3ae9022e1f0fdedf4d87cc2ac4388e17b5121cf7ccef0a3a3afa"),
    "nene_final.pth": (479164585, "883ceef11fac9786c842c29038c802f23f5f2030cc5102664eb0940fc1025315"),
    "all_emotions.npy": (22356096, "48e81667f1fc4ce2b2eaed80fadd0871e1ddfc8933767915954c39ac854d5724"),
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_experiment(profile) -> None:
    """Validate every pinned artifact before importing any downloaded source."""
    root = profile.experiment_root
    manifest = json.loads((root / "source-manifest.json").read_text(encoding="utf-8-sig"))
    if manifest.get("commit") != COMMIT or manifest.get("status") != "ready_for_source_review":
        raise ValueError("Unverified Emotional VITS source manifest")
    entries = manifest.get("files")
    if not isinstance(entries, list) or len(entries) != len(EXPECTED_ARTIFACTS):
        raise ValueError("Expected the complete pinned 18-artifact source set")
    seen = set()
    for entry in entries:
        name = entry.get("upstream_path")
        if name not in EXPECTED_ARTIFACTS or name in seen:
            raise ValueError("Unexpected or duplicate pinned artifact")
        seen.add(name)
        size, expected = EXPECTED_ARTIFACTS[name]
        if (entry.get("commit") != COMMIT or entry.get("status") != "verified"
                or entry.get("local_file") != "upstream/" + name
                or entry.get("bytes") != size or entry.get("sha256") != expected):
            raise ValueError("Source manifest differs from independent pins: " + name)
        path = (root / "upstream" / name).resolve()
        if not path.is_relative_to(root.resolve()) or path.stat().st_size != size or _sha256(path) != expected:
            raise ValueError("Pinned Emotional VITS artifact changed: " + name)
    if not (root / "vendor/unidecode/__init__.py").is_file():
        raise FileNotFoundError("Existing isolated Unidecode dependency is required; no downloads")


def prepare_dictionary(pyopenjtalk, executable: Path) -> None:
    """Require a complete local dictionary before its potentially downloading lazy init."""
    value = getattr(pyopenjtalk, "OPEN_JTALK_DICT_DIR", None)
    if value is None:
        raise FileNotFoundError("Existing local Open JTalk dictionary is required")
    dictionary = Path(os.fsdecode(value)).resolve()
    required = ("char.bin", "matrix.bin", "sys.dic", "unk.dic", "left-id.def",
                "right-id.def", "pos-id.def", "rewrite.def")
    if any(not (dictionary / name).is_file() or (dictionary / name).stat().st_size == 0 for name in required):
        raise FileNotFoundError("Local Open JTalk dictionary is incomplete; download is disabled")
    if os.name == "nt" and not str(dictionary).isascii():
        engine = executable.resolve().parent.parent
        if not dictionary.is_relative_to(engine):
            raise ValueError("Non-ASCII dictionary must be inside the selected runtime engine")
        relative = dictionary.relative_to(engine).as_posix()
        if not relative.isascii():
            raise ValueError("Open JTalk requires an ASCII relative dictionary path")
        # This cwd belongs only to the dedicated helper, never the Qt process.
        os.chdir(engine)
        pyopenjtalk.OPEN_JTALK_DICT_DIR = relative.encode("ascii")


def validate_request(payload, profile):
    if not isinstance(payload, dict) or set(payload) - {"text", "emotion", "seed"}:
        raise ValueError("Expected JSON fields text, emotion, and optional seed")
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARACTERS:
        raise ValueError("Text must contain 1–500 characters; long input is refused, never truncated")
    emotion = payload.get("emotion")
    profile.require_emotion(emotion)
    seed = payload.get("seed")
    if seed is not None and (type(seed) is not int or not 0 <= seed <= 0xFFFFFFFF):
        raise ValueError("Seed must be null or an integer from 0 to 4294967295")
    return text, emotion, seed


def load_emotion_vectors(profile, bank, np):
    """Cache all configured routes, without altering the pinned bank/files."""
    vectors = {}
    for emotion in profile.configured_emotions():
        if emotion in profile.emotion_vectors:
            raw = verify_reference_vector(profile.emotion_vectors[emotion])
            vector = np.load(io.BytesIO(raw), allow_pickle=False)
        else:
            vector = bank[profile.emotion_ids[emotion]]
        if (not isinstance(vector, np.ndarray) or vector.shape != (1024,)
                or vector.dtype != np.dtype("float32") or not np.isfinite(vector).all()):
            raise ValueError("Invalid float32(1024,) emotion vector: " + emotion)
        cached = vector.copy()
        cached.setflags(write=False)
        vectors[emotion] = cached
    return MappingProxyType(vectors)


def _load_source(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def strict_checkpoint(torch, net, path: Path) -> None:
    checkpoint = torch.load(path, map_location="cpu", weights_only=True)
    if not isinstance(checkpoint, dict) or not isinstance(checkpoint.get("model"), dict):
        raise ValueError("Checkpoint must contain a model state dictionary")
    state = checkpoint["model"]
    expected = net.state_dict()
    if expected.keys() != state.keys():
        raise ValueError("Checkpoint keys differ; random parameter fallback is forbidden")
    for key, tensor in expected.items():
        loaded = state[key]
        if not isinstance(loaded, torch.Tensor) or tensor.shape != loaded.shape:
            raise ValueError("Checkpoint tensor shape differs: " + key)
        if not torch.isfinite(loaded).all().item():
            raise ValueError("Checkpoint contains nonfinite values: " + key)
    net.load_state_dict(state, strict=True)


def validate_signal(signal, np):
    if signal.ndim != 1 or signal.size == 0 or not np.isfinite(signal).all() or not np.any(signal):
        raise ValueError("Empty, nonfinite or digitally silent synthesis")
    pcm = (np.clip(signal, -1., 1.) * 32767).astype("<i2")
    if not np.any(pcm):
        raise ValueError("Synthesis becomes digital silence after PCM16 quantization")
    return pcm


def encode_wav(signal, sample_rate: int, np) -> bytes:
    pcm = validate_signal(signal, np)
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setparams((1, 2, sample_rate, 0, "NONE", "not compressed"))
        stream.writeframes(pcm.tobytes())
    return output.getvalue()


class UtteranceTooLong(ValueError):
    """A hard sequence/duration cap requires another natural boundary."""


def split_normalized(text: str, target: int = TARGET_NORMALIZED_LENGTH) -> list[str]:
    """Partition only at existing punctuation/accent-phrase spaces, losslessly.

    The pinned Japanese frontend inserts spaces at accent phrase boundaries.
    Never split a phoneme, its accent arrows, or an unbroken word to hit the
    soft target. An indivisible long unit is checked against the hard caps.
    """
    if target < 1:
        raise ValueError("Normalized segment target must be positive")
    if text and not any(symbol.isalpha() for symbol in text):
        raise ValueError("Normalized text contains no phonemes")
    boundaries = []
    for match in re.finditer(r"[.!?,]+ *| +", text):
        end = match.end()
        if end < len(text):
            marks = match.group()
            priority = 0 if any(mark in marks for mark in ".!?") else (1 if "," in marks else 2)
            boundaries.append((end, priority))
    parts, start = [], 0
    while len(text) - start > target:
        nearby = [(end, priority) for end, priority in boundaries if start < end <= start + target]
        if nearby:
            best_priority = min(priority for _, priority in nearby)
            end = max(end for end, priority in nearby if priority == best_priority)
        else:
            end = next((end for end, _ in boundaries if end > start), len(text))
        parts.append(text[start:end])
        start = end
    if start < len(text):
        parts.append(text[start:])
    merged, leading_marks = [], ""
    for part in parts:
        if not any(symbol.isalpha() for symbol in part):
            if merged:
                merged[-1] += part
            else:
                leading_marks += part
        else:
            merged.append(leading_marks + part)
            leading_marks = ""
    parts = merged
    assert "".join(parts) == text
    return parts


def guarded_infer(net, x, x_lengths, *, torch, commons, sid=None, emo=None,
                  noise_scale=1, length_scale=1, noise_scale_w=1.):
    """Pinned models.py infer, with duration checks before latent allocation.

    Keep the tensor operations and random calls in the upstream order. The
    decoder receives the complete checked utterance; it never truncates it.
    """
    x, m_p, logs_p, x_mask = net.enc_p(x, x_lengths, emo)
    g = net.emb_g(sid).unsqueeze(-1) if net.n_speakers > 0 else None
    if net.use_sdp:
        logw = net.dp(x, x_mask, g=g, reverse=True, noise_scale=noise_scale_w)
    else:
        logw = net.dp(x, x_mask, g=g)
    if not torch.isfinite(logw).all().item():
        raise ValueError("Predicted log durations contain nonfinite values")
    w = torch.exp(logw) * x_mask * length_scale
    if not torch.isfinite(w).all().item():
        raise ValueError("Predicted durations contain nonfinite values")
    w_ceil = torch.ceil(w)
    raw_lengths = torch.sum(w_ceil, [1, 2])
    if not torch.isfinite(raw_lengths).all().item():
        raise ValueError("Predicted duration sum contains nonfinite values")
    if raw_lengths.max().item() > MAX_LATENT_FRAMES:
        raise UtteranceTooLong("Predicted utterance exceeds the frame cap; refusing truncated audio")
    y_lengths = torch.clamp_min(raw_lengths, 1).long()
    y_mask = torch.unsqueeze(commons.sequence_mask(y_lengths, None), 1).to(x_mask.dtype)
    attn_mask = torch.unsqueeze(x_mask, 2) * torch.unsqueeze(y_mask, -1)
    attn = commons.generate_path(w_ceil, attn_mask)
    m_p = torch.matmul(attn.squeeze(1), m_p.transpose(1, 2)).transpose(1, 2)
    logs_p = torch.matmul(attn.squeeze(1), logs_p.transpose(1, 2)).transpose(1, 2)
    z_p = m_p + torch.randn_like(m_p) * torch.exp(logs_p) * noise_scale
    z = net.flow(z_p, y_mask, g=g, reverse=True)
    o = net.dec((z * y_mask)[:, :, :MAX_LATENT_FRAMES], g=g)
    return o, attn, y_mask, (z, z_p, m_p, logs_p)


class EmotionalVITSRuntime:
    def __init__(self, profile):
        verify_experiment(profile)
        if Path(sys.executable).resolve() != profile.runtime_executable:
            raise ValueError("Helper must run in the profile's selected isolated Python runtime")
        self.profile = profile
        self.lock = threading.Lock()
        upstream = profile.experiment_root / "upstream"
        sys.path[:0] = [str(upstream), str(profile.experiment_root / "vendor")]
        import numpy as np
        self.emotions = np.load(upstream / "all_emotions.npy", allow_pickle=False, mmap_mode="r")
        if self.emotions.shape != (5458, 1024) or self.emotions.dtype != np.dtype("float32"):
            raise ValueError("Unexpected emotion bank shape or dtype")
        # Validate tiny independent files before importing/loading the model.
        self.emotion_vectors = load_emotion_vectors(profile, self.emotions, np)
        import torch
        import pyopenjtalk
        prepare_dictionary(pyopenjtalk, profile.runtime_executable)
        torch.set_num_threads(profile.threads)
        torch.set_num_interop_threads(1)
        from models import SynthesizerTrn
        import commons
        symbols = _load_source("nene_runtime_symbols", upstream / "text/symbols.py").symbols
        self.japanese = _load_source("nene_runtime_japanese", upstream / "text/japanese.py")
        self.config = json.loads((upstream / "configs/vtubers.json").read_text(encoding="utf-8"))
        data = self.config["data"]
        if data["text_cleaners"] != ["japanese_cleaners"] or data["sampling_rate"] != 22050:
            raise ValueError("Unexpected pinned Japanese normalization configuration")
        self.net = SynthesizerTrn(len(symbols), data["filter_length"] // 2 + 1,
            self.config["train"]["segment_size"] // data["hop_length"],
            n_speakers=data["n_speakers"], **self.config["model"])
        strict_checkpoint(torch, self.net, upstream / "nene_final.pth")
        self.net.eval().to("cpu")
        self.symbol_ids = {symbol: index for index, symbol in enumerate(symbols)}
        self.torch, self.np, self.commons = torch, np, commons

    def synthesize(self, payload) -> bytes:
        text, emotion, seed = validate_request(payload, self.profile)
        with self.lock:
            normalized = self.japanese.japanese_to_romaji_with_accent(text)
            if not normalized:
                raise ValueError("Japanese normalization produced no text")
            if re.match("[A-Za-z]", normalized[-1]):
                normalized += "."
            if set(normalized) - self.symbol_ids.keys():
                raise ValueError("Japanese normalization produced unmapped symbols")
            torch, np = self.torch, self.np
            torch.manual_seed(secrets.randbits(32) if seed is None else seed)
            sid = torch.LongTensor([self.profile.speaker_id]).to("cpu")
            if emotion not in self.emotion_vectors:
                raise ValueError("Validated emotion vector is missing from the helper cache: " + emotion)
            vector = self.emotion_vectors[emotion].copy()
            if not np.isfinite(vector).all():
                raise ValueError("Selected emotion vector contains nonfinite values")
            emo = torch.from_numpy(vector).unsqueeze(0).to("cpu")
            pending = deque(split_normalized(normalized))
            signals = []
            while pending:
                part = pending.popleft()
                try:
                    sequence = [self.symbol_ids[symbol] for symbol in part]
                    if self.config["data"]["add_blank"]:
                        sequence = self.commons.intersperse(sequence, 0)
                    if len(sequence) > MAX_SEQUENCE_LENGTH:
                        raise UtteranceTooLong("Normalized input is too long; refusing truncation")
                    x = torch.LongTensor(sequence).unsqueeze(0).to("cpu")
                    lengths = torch.LongTensor([len(sequence)]).to("cpu")
                    with torch.inference_mode():
                        prediction = guarded_infer(self.net, x, lengths, torch=torch, commons=self.commons,
                            sid=sid, emo=emo, noise_scale=0.667, noise_scale_w=0.8, length_scale=1.)
                except UtteranceTooLong as exc:
                    smaller = split_normalized(part, max(1, len(part) // 2))
                    if len(smaller) < 2:
                        raise ValueError(str(exc) + "; no further natural boundary is available") from exc
                    pending.extendleft(reversed(smaller))
                    continue
                segment_signal = prediction[0][0, 0].detach().float().cpu().numpy()
                validate_signal(segment_signal, np)
                signals.append(segment_signal)
            # Complete all segments before encoding, so a later failure cannot
            # turn a partial reply into an apparently successful WAV response.
            signal = signals[0] if len(signals) == 1 else np.concatenate(signals)
            return encode_wav(signal, self.config["data"]["sampling_rate"], np)


def make_handler(runtime, token: str, health: dict):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, *args):
            pass  # Never log authorization headers, request text, or the CLI token.

        def _respond(self, status, body, content_type="application/json"):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            self.wfile.write(body)

        def _json(self, status, payload):
            self._respond(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

        def _authorized(self):
            actual = self.headers.get("Authorization", "")
            if not hmac.compare_digest(actual.encode("utf-8"), ("Bearer " + token).encode("ascii")):
                self._json(401, {"error": "Authorization required"})
                return False
            return True

        def do_GET(self):
            if not self._authorized():
                return
            if self.path != "/health":
                self._json(404, {"error": "Unknown route"})
                return
            self._json(200, health)

        def do_POST(self):
            if not self._authorized():
                return
            if self.path != "/tts":
                self._json(404, {"error": "Unknown route"})
                return
            lengths = self.headers.get_all("Content-Length", [])
            if self.headers.get("Transfer-Encoding") or len(lengths) != 1:
                self._json(400, {"error": "One Content-Length is required; chunking is unsupported"})
                return
            try:
                length = int(lengths[0])
            except ValueError:
                self._json(400, {"error": "Invalid Content-Length"})
                return
            if not 0 < length <= MAX_REQUEST_BYTES:
                self._json(413, {"error": "JSON request must contain 1–65536 bytes"})
                return
            if self.headers.get_content_type() != "application/json":
                self._json(415, {"error": "Content-Type application/json is required"})
                return
            try:
                body = self.rfile.read(length)
                if len(body) != length:
                    raise ValueError("Incomplete JSON body")
                payload = json.loads(body.decode("utf-8"))
                validate_request(payload, runtime.profile)
                audio = runtime.synthesize(payload)
            except (ValueError, UnicodeError, EmotionalVITSProfileError) as exc:
                self._json(400, {"error": str(exc)})
                return
            except Exception:
                self._json(500, {"error": "Emotional VITS inference failed"})
                return
            self._respond(200, audio, "audio/wav")
    return Handler


def serve(profile_path: Path, ready_file: Path, token: str, *, runtime_factory=EmotionalVITSRuntime):
    """Serve only this owned helper; the parent terminates its owned process."""
    if not profile_path.is_absolute() or not ready_file.is_absolute():
        raise ValueError("Profile and ready-file paths must be absolute")
    if not isinstance(token, str) or not 16 <= len(token) <= 512 or not token.isascii() or any(c.isspace() or ord(c) < 33 for c in token):
        raise ValueError("A private ASCII bearer token with at least 16 characters is required")
    if ready_file.exists():
        raise FileExistsError("Refusing to overwrite an existing ready file")
    profile = load_emotional_vits_profile(profile_path)
    runtime = runtime_factory(profile)
    health = {"protocol": 1, "kind": PROFILE_KIND, "signature": profile.signature,
              "port": 0, "pid": os.getpid()}
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(runtime, token, health))
    server.daemon_threads = True
    health["port"] = server.server_address[1]
    temporary = ready_file.with_suffix(ready_file.suffix + ".tmp")
    own_temporary = False
    own_ready = False
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            own_temporary = True
            json.dump(health, stream)
            stream.write("\n")
        # Same-directory hard link publishes complete JSON atomically and
        # refuses an existing destination on both Windows and POSIX.
        os.link(temporary, ready_file)
        own_ready = True
        temporary.unlink()
        own_temporary = False
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
        # A path reused by another process must never be removed by this one.
        try:
            if own_ready and json.loads(ready_file.read_text(encoding="utf-8")) == health:
                ready_file.unlink()
        except (OSError, ValueError):
            pass
        try:
            if own_temporary:
                temporary.unlink()
        except FileNotFoundError:
            pass
