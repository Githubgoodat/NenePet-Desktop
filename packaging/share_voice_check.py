"""Explicit, isolated frozen-EXE -> external-engine CPU speech acceptance.

This is never a normal-startup step. Only a public packaged reference and a
fixed synthetic sentence are used; no user configuration/history is loaded.
The only HTTP destination is 127.0.0.1:19883. A busy port causes refusal.
"""
from __future__ import annotations

import array
import hashlib
import io
import json
import math
import os
from pathlib import Path
import shutil
import socket
import sys
import time
from uuid import uuid4
import wave

from share_support import ExternalSubprocessProxy


ADDRESS = ("127.0.0.1", 19883)
SENTENCE = "今日は少し休んでから、一緒に続きを考えましょう。"
MAX_WAV_BYTES = 64 * 1024 * 1024


def _publish(report_path: Path, report: dict) -> None:
    temporary = report_path.with_name(report_path.name + "." + uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(report_path)


def _check_port_free() -> None:
    # Bind only the dedicated acceptance port; never inspect the production
    # service on 9881 or adopt/terminate a process already listening here.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            probe.bind(ADDRESS)
        except OSError as exc:
            raise RuntimeError("验收端口 19883 已被占用；未启动或停止任何现有服务") from exc


def _isolated_environment(work: Path) -> None:
    for name, relative in {
        "HF_HOME": "hf", "HUGGINGFACE_HUB_CACHE": "hf/hub",
        "TRANSFORMERS_CACHE": "hf/transformers", "HF_MODULES_CACHE": "hf/modules",
        "HF_DATASETS_CACHE": "hf/datasets", "TORCH_HOME": "torch",
        "TORCH_EXTENSIONS_DIR": "torch/extensions", "NUMBA_CACHE_DIR": "numba",
        "MPLCONFIGDIR": "matplotlib", "XDG_CACHE_HOME": "xdg",
        "TEMP": "temporary", "TMP": "temporary", "TMPDIR": "temporary",
        "PYTHONPYCACHEPREFIX": "pycache",
    }.items():
        directory = work / relative
        directory.mkdir(parents=True, exist_ok=True)
        os.environ[name] = str(directory)
    os.environ.update({
        "AIPET_DATA_DIR": str(work / "unused-state"),
        "AIPET_CONFIG_PATH": str(work / "unused-state" / "never-loaded.json"),
        "AIPET_CACHE_DIR": str(work / "cache"),
        "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1", "WANDB_MODE": "disabled",
        "PYTHONDONTWRITEBYTECODE": "1", "CUDA_VISIBLE_DEVICES": "",
        "TOKENIZERS_PARALLELISM": "false", "OMP_NUM_THREADS": "4",
        "MKL_NUM_THREADS": "4", "OPENBLAS_NUM_THREADS": "4", "NUMBA_NUM_THREADS": "4",
    })


def wav_metrics(raw: bytes) -> dict:
    if not raw or len(raw) > MAX_WAV_BYTES:
        raise ValueError("合成 WAV 为空或超出验收大小限制")
    with wave.open(io.BytesIO(raw), "rb") as audio:
        channels, width, rate, frames = (audio.getnchannels(), audio.getsampwidth(),
                                        audio.getframerate(), audio.getnframes())
        if audio.getcomptype() != "NONE" or width not in (1, 2, 3, 4) or channels not in (1, 2) or not 8000 <= rate <= 192000:
            raise ValueError("合成结果不是受支持的 PCM WAV")
        pcm = audio.readframes(frames)
    duration = frames / rate
    if not .2 <= duration <= 60 or len(pcm) != frames * channels * width:
        raise ValueError("合成结果时长或 PCM 长度不正确")
    if width == 1:
        samples = [value - 128 for value in pcm]
    elif width in (2, 4):
        samples = array.array("h" if width == 2 else "i")
        samples.frombytes(pcm)
        if sys.byteorder != "little":
            samples.byteswap()
    else:
        samples = [int.from_bytes(pcm[offset:offset + 3], "little", signed=True) for offset in range(0, len(pcm), 3)]
    full_scale = 2 ** (width * 8 - 1)
    peak = max(abs(value) for value in samples) / full_scale
    rms = math.sqrt(sum(value * value for value in samples) / len(samples)) / full_scale
    if peak <= .001 or rms <= .0001:
        raise ValueError("合成 WAV 为静音或幅度过低")
    return {"duration_seconds": duration, "sample_rate": rate, "channels": channels,
            "sample_width": width, "frames": frames, "peak": peak, "rms": rms,
            "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def run_voice_check(base: Path, resources: Path, engine_root: Path, report_path: Path) -> dict:
    engine_root, report_path = engine_root.expanduser().resolve(), report_path.expanduser().resolve()
    # Never overwrite an earlier acceptance result or place diagnostic writes
    # inside the engine, immutable resources, or the normal portable profile.
    protected = (engine_root, resources.resolve(), (base / "data" / "profile").resolve())
    if report_path.exists() or any(report_path.is_relative_to(root) for root in protected):
        raise ValueError("请指定未存在且位于引擎、资源及正式 profile 目录之外的验收报告路径")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    work = report_path.parent / ("voice-check-" + uuid4().hex)
    work.mkdir(exist_ok=False)
    report = {"schema_version": 1, "success": False, "stage": "preflight",
              "scope": "frozen_parent_to_external_cpu_engine", "device": "cpu", "is_half": False,
              "port": ADDRESS[1], "work_dir": str(work), "engine_root": str(engine_root),
              "profile_read": False, "production_profile_written": False,
              "viewer_started": False, "dialogue_api_used": False, "audio_played": False,
              "network_scope": "parent_loopback_19883_only", "hf_offline_requested": True,
              "owned_child_stopped": None,
              "frozen_parent": bool(getattr(sys, "frozen", False)),
              "not_verified": ["CUDA 合成", "好友显卡及驱动", "人物显示", "音色和情绪听感"]}
    with report_path.open("x", encoding="utf-8") as initial:
        initial.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    original_environment = dict(os.environ)
    manager = None
    process = None
    session = None
    service = None
    original_subprocess = None
    before_bytecode = None
    started = time.monotonic()
    try:
        _check_port_free()
        if not (engine_root / "api_v2.py").is_file():
            raise ValueError("指定目录不包含 api_v2.py")
        before_bytecode = {str(path.relative_to(engine_root)) for path in engine_root.rglob("__pycache__")}
        _isolated_environment(work)
        from aipet.core.voice_profile import load_voice_profile, write_profile_engine_config
        from aipet.platforms import get_platform_runtime
        import aipet.core.tts_service as service
        import requests
        original_subprocess = service.subprocess
        service.subprocess = ExternalSubprocessProxy(original_subprocess)
        reference_dir = work / "reference"
        reference_dir.mkdir()
        for extension in ("wav", "txt"):
            source = resources / "voice-sources" / ("c2-2-nene-01-clause." + extension)
            shutil.copy2(source, reference_dir / source.name)
        manifest = work / "nene-cpu-check.json"
        manifest.write_text(json.dumps({
            "schema_version": 1, "version": "v2Pro", "device": "cpu", "is_half": False,
            "gpt_weights": str(engine_root / "GPT_SoVITS/pretrained_models/s1v3.ckpt"),
            "sovits_weights": str(engine_root / "GPT_SoVITS/pretrained_models/v2Pro/s2Gv2Pro.pth"),
            "text_lang": "ja", "prompt_lang": "ja", "references": {},
            "baseline_reference": {"audio": str(reference_dir / "c2-2-nene-01-clause.wav"),
                                   "transcript": str(reference_dir / "c2-2-nene-01-clause.txt")},
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        profile = load_voice_profile(str(manifest), engine_root)
        config = write_profile_engine_config(profile, work / "cache")
        runtime = get_platform_runtime()
        command, environment = service._build_start_command(engine_root, ADDRESS, runtime, config_path=config)
        manager = service.LocalTTSServiceManager(runtime)
        process = manager._launch(command, environment, engine_root, ADDRESS)
        report.update(stage="waiting_for_owned_api", owned_child_pid=process.pid,
                      command=command, standard_service_launch=True, dll_isolation_enabled=True)
        _publish(report_path, report)
        session = requests.Session()
        session.trust_env = False
        url = "http://127.0.0.1:19883"
        deadline, next_report = time.monotonic() + 600, time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("本次启动的验收引擎已退出，返回码 " + str(process.returncode))
            try:
                response = session.get(url + "/openapi.json", timeout=(.4, .75), allow_redirects=False)
                if response.status_code == 200:
                    schema = response.json()
                    post = schema.get("paths", {}).get("/tts", {}).get("post", {})
                    if isinstance(post, dict) and post.get("requestBody") and process.poll() is None:
                        break
            except (requests.RequestException, ValueError):
                pass
            if time.monotonic() >= next_report:
                report["elapsed_seconds"] = round(time.monotonic() - started, 2)
                _publish(report_path, report)
                next_report = time.monotonic() + 10
            time.sleep(.35)
        else:
            raise TimeoutError("等待独立 CPU 引擎就绪超时")
        if process.poll() is not None:
            raise RuntimeError("发送验收文本前，本次拥有的引擎已退出")
        # Cancel only this launcher's traceback timer. Never touch a production
        # marker, API endpoint or service ownership record.
        if environment.get("AIPET_TTS_READY_MARKER"):
            Path(environment["AIPET_TTS_READY_MARKER"]).write_text("ready\n", encoding="utf-8")
        report["stage"] = "synthesizing_public_sample"
        _publish(report_path, report)
        reference = profile.baseline_reference
        payload = {"text": SENTENCE, "text_lang": "ja", "ref_audio_path": str(reference.audio),
                   "prompt_text": reference.transcript, "prompt_lang": "ja", "aux_ref_audio_paths": [],
                   "top_k": 15, "top_p": 1, "temperature": 1, "text_split_method": "cut1",
                   "batch_size": 1, "batch_threshold": .75, "split_bucket": True,
                   "speed_factor": 1, "streaming_mode": False, "seed": 20261022,
                   "parallel_infer": True, "repetition_penalty": 1.35,
                   "sample_steps": 32, "super_sampling": False, "media_type": "wav"}
        chunks, byte_count = [], 0
        with session.post(url + "/tts", json=payload, timeout=(5, 600), allow_redirects=False, stream=True) as response:
            if response.status_code != 200:
                raise RuntimeError("独立验收 /tts 返回 HTTP " + str(response.status_code))
            for chunk in response.iter_content(chunk_size=65536):
                byte_count += len(chunk)
                if byte_count > MAX_WAV_BYTES:
                    raise ValueError("合成结果超过验收大小限制")
                chunks.append(chunk)
        if process.poll() is not None:
            raise RuntimeError("收到结果时，本次拥有的引擎已退出")
        raw = b"".join(chunks)
        report["wav"] = wav_metrics(raw)
        wav_path = work / "public-sample-cpu.wav"
        wav_path.write_bytes(raw)
        report.update(success=True, stage="passed", wav_path=str(wav_path))
    except Exception as exc:
        report.update(success=False, stage="failed", error_type=type(exc).__name__, error=str(exc)[:1000])
    finally:
        cleanup_errors = []
        if session is not None:
            try:
                session.close()
            except Exception as exc:
                cleanup_errors.append("session:" + type(exc).__name__)
        if manager is not None:
            # Only this manager's direct Popen and its own Job are closed.
            try:
                manager.shutdown()
            except Exception as exc:
                cleanup_errors.append("owned_manager:" + type(exc).__name__ + ":" + str(exc)[:500])
        if process is not None:
            try:
                if process.poll() is None:
                    # A failure in the manager's own cleanup must not abandon
                    # this direct child. Never locate/kill a process by port.
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except service.subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                report["owned_child_stopped"] = process.poll() is not None
                if not report["owned_child_stopped"]:
                    cleanup_errors.append("owned_child_still_running")
            except Exception as exc:
                cleanup_errors.append("owned_child_status:" + type(exc).__name__)
        if service is not None and original_subprocess is not None:
            service.subprocess = original_subprocess
        if before_bytecode is not None and engine_root.is_dir():
            try:
                after = {str(path.relative_to(engine_root)) for path in engine_root.rglob("__pycache__")}
                report["engine_new_bytecode_directories"] = sorted(after - before_bytecode)
                if report["engine_new_bytecode_directories"]:
                    cleanup_errors.append("engine_new_bytecode_directories")
            except Exception as exc:
                cleanup_errors.append("engine_bytecode_inspection:" + type(exc).__name__)
        log = work / "cache/logs/gpt-sovits-service.log"
        report["log_path"] = str(log)
        try:
            if not report["success"] and log.is_file():
                with log.open("rb") as stream:
                    stream.seek(max(0, log.stat().st_size - 8000))
                    report["log_tail"] = stream.read(8000).decode("utf-8", errors="replace")
        except OSError as exc:
            cleanup_errors.append("log_tail:" + type(exc).__name__)
        if cleanup_errors:
            report.update(success=False, stage="failed", cleanup_errors=cleanup_errors)
        report["elapsed_seconds"] = round(time.monotonic() - started, 2)
        os.environ.clear()
        os.environ.update(original_environment)
        _publish(report_path, report)
    return report
