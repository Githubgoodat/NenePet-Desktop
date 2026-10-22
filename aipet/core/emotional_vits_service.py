"""Own an optional Emotional VITS child, isolated from GPT-SoVITS services."""

from __future__ import annotations

import atexit
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import threading
import time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from aipet.core.config import get_cache_dir
from aipet.platforms import PlatformRuntime, get_platform_runtime


class EmotionalVITSServiceError(RuntimeError):
    pass


class EmotionalVITSServiceCancelled(EmotionalVITSServiceError):
    """Cancellation affects only the caller or its owned startup."""


@dataclass(frozen=True)
class EmotionalVITSEndpoint:
    url: str
    token: str = field(repr=False)
    signature: str
    pid: int


@dataclass
class _OwnedChild:
    process: subprocess.Popen
    directory: Path
    ready_file: Path
    log_file: object
    token: str
    signature: str
    endpoint: EmotionalVITSEndpoint | None = None


def load_emotional_vits_profile(path):
    # Lazy import keeps importing this lifecycle module free of model/profile I/O.
    from aipet.core.emotional_vits_profile import load_emotional_vits_profile as load
    return load(path)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the bearer token, including to another loopback port.
        return None


def _request_health(endpoint: EmotionalVITSEndpoint, timeout: float) -> dict | None:
    request = Request(
        endpoint.url.rsplit("/", 1)[0] + "/health",
        headers={"Authorization": "Bearer " + endpoint.token}, method="GET",
    )
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            if response.status != 200:
                return None
            data = response.read(16_385)
    except (HTTPError, URLError, OSError, TimeoutError):
        return None
    if len(data) > 16_384:
        raise EmotionalVITSServiceError("情绪语音健康响应超出大小限制")
    try:
        value = json.loads(data)
    except (ValueError, UnicodeError) as exc:
        raise EmotionalVITSServiceError("情绪语音健康响应不是有效 JSON") from exc
    if not isinstance(value, dict):
        raise EmotionalVITSServiceError("情绪语音健康响应格式不正确")
    return value


def _identity(value: dict, signature: str, pid: int, port: int | None = None) -> int:
    actual_port = value.get("port")
    valid = (
        type(value.get("protocol")) is int and value["protocol"] == 1
        and value.get("kind") == "nene-emotional-vits"
        and value.get("signature") == signature
        and type(value.get("pid")) is int and value["pid"] == pid
        and type(actual_port) is int and 1 <= actual_port <= 65535
        and actual_port not in {9880, 9881}
        and (port is None or actual_port == port)
    )
    if not valid:
        raise EmotionalVITSServiceError("情绪语音服务的协议、签名、端口或自有 PID 不匹配")
    return actual_port


class EmotionalVITSServiceManager:
    """Serialize startup and stop; never discover, adopt or kill foreign servers."""

    def __init__(
        self, platform_runtime: PlatformRuntime | None = None, *,
        cache_dir: Path | None = None, helper_path: Path | None = None,
    ) -> None:
        self._platform_runtime = platform_runtime or get_platform_runtime()
        self._process_job = self._platform_runtime.processes.create_child_process_guard()
        self._cache_dir = Path(cache_dir) if cache_dir is not None else None
        self._helper_path = helper_path or Path(__file__).resolve().parents[2] / "tools/serve_nene_emotional_vits.py"
        self._condition = threading.Condition(threading.RLock())
        self._owned: _OwnedChild | None = None
        self._starting = False
        self._stopping = False
        self._shutting_down = False
        self._guard_closed = False
        self._startup_cancel: threading.Event | None = None
        self._startup_thread: int | None = None
        self._last_child: _OwnedChild | None = None
        self._last_port: int | None = None
        self._last_signature: str | None = None
        self._last_log_path: Path | None = None
        self._startup_started_at: float | None = None
        self._last_stage = "idle"

    def owns_running_process(self) -> bool:
        with self._condition:
            return self._owned is not None and self._owned.process.poll() is None

    def current_endpoint(self) -> EmotionalVITSEndpoint | None:
        """Return cached verified identity only; this method does no network I/O."""
        with self._condition:
            if self._stopping or self._shutting_down or self._owned is None:
                return None
            if self._owned.process.poll() is not None:
                return None
            return self._owned.endpoint

    def startup_diagnostics(self) -> dict:
        """Read cached ownership and our bounded log; never probe or expose tokens."""
        with self._condition:
            child = self._owned or self._last_child
            running = self._owned is not None and self._owned.process.poll() is None
            started = self._startup_started_at
            result = {
                "pid": child.process.pid if child is not None else None,
                "port": self._last_port,
                "signature": child.signature if child is not None else self._last_signature,
                "running": running, "is_starting": self._starting,
                "stage": self._last_stage,
                "log_path": str(self._last_log_path) if self._last_log_path is not None else None,
                "elapsed_seconds": time.monotonic() - started if started is not None else None,
                "process_exit_code": child.process.poll() if child is not None else None,
            }
        result["log_tail"] = self._log_tail(child) if child is not None else ""
        return result

    @staticmethod
    def _cancelled(callback, event: threading.Event | None = None) -> None:
        if (event is not None and event.is_set()) or (callback is not None and callback()):
            raise EmotionalVITSServiceCancelled("情绪语音启动已取消；仅清理本次自有子进程")

    def ensure_running(self, settings, cancelled: Callable[[], bool] | None = None) -> EmotionalVITSEndpoint:
        try:
            timeout = float(settings.timeout_seconds)
        except (TypeError, ValueError, AttributeError) as exc:
            raise EmotionalVITSServiceError("情绪语音启动超时设置无效") from exc
        if not math.isfinite(timeout) or timeout <= 0:
            raise EmotionalVITSServiceError("情绪语音启动超时必须为正数")
        deadline = time.monotonic() + timeout
        self._cancelled(cancelled)
        with self._condition:
            while self._starting or self._stopping:
                if self._shutting_down:
                    raise EmotionalVITSServiceError("情绪语音管理器已关闭")
                self._cancelled(cancelled)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise EmotionalVITSServiceError("等待其他情绪语音启动或停止操作超时")
                self._condition.wait(min(0.1, remaining))
            if self._shutting_down:
                raise EmotionalVITSServiceError("情绪语音管理器已关闭")
            self._cancelled(cancelled)
            self._starting = True
            self._last_stage = "loading_profile"
            self._startup_thread = threading.get_ident()
            event = self._startup_cancel = threading.Event()
        launched: _OwnedChild | None = None
        try:
            profile = load_emotional_vits_profile(settings.emotional_vits_profile)
            self._cancelled(cancelled, event)
            if time.monotonic() >= deadline:
                raise EmotionalVITSServiceError("读取情绪语音配置时启动超时")
            with self._condition:
                owned = self._owned
            if owned is not None:
                if owned.process.poll() is not None:
                    self._dispose(owned)
                else:
                    if owned.signature != profile.signature:
                        raise EmotionalVITSServiceError("配置已更改，请先停止情绪语音服务后重启")
                    endpoint = owned.endpoint
                    if endpoint is None:
                        raise EmotionalVITSServiceError("自有情绪语音进程尚未就绪，请先停止后重启")
                    self._last_stage = "checking_health"
                    value = _request_health(endpoint, min(0.75, max(0.001, deadline - time.monotonic())))
                    self._cancelled(cancelled, event)
                    if value is None:
                        raise EmotionalVITSServiceError("自有情绪语音服务运行中但健康检查失败，请先停止后重启")
                    _identity(value, endpoint.signature, endpoint.pid, urlsplit(endpoint.url).port)
                    if owned.process.poll() is not None:
                        raise EmotionalVITSServiceError("自有情绪语音进程在健康检查时退出")
                    if time.monotonic() >= deadline:
                        raise EmotionalVITSServiceError("确认情绪语音服务健康时超时")
                    self._last_stage = "ready"
                    return endpoint

            launched = self._launch(profile, event, cancelled)
            self._last_stage = "waiting_for_model"
            while time.monotonic() < deadline:
                self._cancelled(cancelled, event)
                exit_code = launched.process.poll()
                if exit_code is not None:
                    raise EmotionalVITSServiceError(f"情绪语音子进程提前退出（代码 {exit_code}）")
                ready = self._read_ready(launched.ready_file)
                if ready is not None:
                    port = _identity(ready, launched.signature, launched.process.pid)
                    self._last_port = port
                    endpoint = EmotionalVITSEndpoint(
                        f"http://127.0.0.1:{port}/tts", launched.token, launched.signature, launched.process.pid,
                    )
                    value = _request_health(endpoint, min(0.75, max(0.001, deadline - time.monotonic())))
                    if value is not None:
                        _identity(value, launched.signature, launched.process.pid, port)
                        with self._condition:
                            self._cancelled(cancelled, event)
                            if launched.process.poll() is not None:
                                raise EmotionalVITSServiceError("情绪语音子进程在确认就绪前退出")
                            if time.monotonic() >= deadline:
                                raise EmotionalVITSServiceError("确认情绪语音服务就绪时超时")
                            launched.endpoint = endpoint
                            self._last_stage = "ready"
                            return endpoint
                event.wait(min(0.1, max(0, deadline - time.monotonic())))
            raise EmotionalVITSServiceError("等待情绪语音模型和动态端口就绪超时")
        except BaseException as exc:
            self._last_stage = "cancelled" if isinstance(exc, EmotionalVITSServiceCancelled) else "failed"
            detail = self._log_tail(launched) if launched is not None else ""
            if launched is not None:
                self._dispose(launched)
            if isinstance(exc, (EmotionalVITSServiceCancelled, KeyboardInterrupt, SystemExit)):
                raise
            message = str(exc) + ("\n" + detail if detail else "")
            raise EmotionalVITSServiceError(message) from exc
        finally:
            with self._condition:
                self._starting = False
                self._startup_thread = None
                self._startup_cancel = None
                self._condition.notify_all()

    def _launch(self, profile, event, cancelled) -> _OwnedChild:
        runtime = Path(profile.runtime_executable).resolve()
        helper = Path(self._helper_path).resolve()
        experiment = Path(profile.experiment_root).resolve()
        if not runtime.is_file() or not helper.is_file() or not experiment.is_dir():
            raise EmotionalVITSServiceError("情绪语音 runtime、启动工具或实验目录缺失")
        if not isinstance(profile.signature, str) or not profile.signature:
            raise EmotionalVITSServiceError("情绪语音配置缺少签名")
        directory = None
        log_file = None
        process = None
        child = None
        try:
            self._cancelled(cancelled, event)
            cache = (self._cache_dir if self._cache_dir is not None else get_cache_dir() / "emotional-vits").resolve()
            cache.mkdir(parents=True, exist_ok=True)
            directory = Path(tempfile.mkdtemp(prefix="owned-", dir=cache))
            ready = directory / "ready.json"
            log_file = (directory / "service.log").open("xb", buffering=0)
            with self._condition:
                self._last_child = None
                self._last_port = None
                self._last_signature = profile.signature
                self._last_log_path = directory / "service.log"
                self._startup_started_at = time.monotonic()
                self._last_stage = "starting_process"
            token = secrets.token_urlsafe(32)
            command = [str(runtime), "-u", "-X", "faulthandler", str(helper),
                       "--profile", str(Path(profile.manifest_path).resolve()),
                       "--ready-file", str(ready), "--token", token]
            environment = os.environ.copy()
            environment.update(PYTHONUTF8="1", PYTHONUNBUFFERED="1", PYTHONDONTWRITEBYTECODE="1", CUDA_VISIBLE_DEVICES="")
            environment["PATH"] = str(runtime.parent) + os.pathsep + environment.get("PATH", "")
            for key in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
                environment[key] = str(getattr(profile, "threads", 4))
            self._cancelled(cancelled, event)
            process = subprocess.Popen(
                command, cwd=str(experiment), env=environment, stdin=subprocess.DEVNULL,
                stdout=log_file, stderr=subprocess.STDOUT,
                **self._platform_runtime.processes.hidden_subprocess_options(),
            )
            child = _OwnedChild(process, directory, ready, log_file, token, profile.signature)
            with self._condition:
                self._owned = child
                self._last_child = child
            self._process_job.assign(process)
            self._cancelled(cancelled, event)
            return child
        except BaseException:
            if child is not None:
                self._dispose(child)
            else:
                if process is not None:
                    self._terminate_process(process)
                if log_file is not None:
                    log_file.close()
                if directory is not None:
                    self._remove_owned_files(directory)
            raise

    @staticmethod
    def _read_ready(path: Path) -> dict | None:
        try:
            with path.open("rb") as stream:
                data = stream.read(16_385)
        except FileNotFoundError:
            return None
        if len(data) > 16_384:
            raise EmotionalVITSServiceError("情绪语音 ready 文件超出大小限制")
        try:
            value = json.loads(data)
        except (ValueError, UnicodeError):
            return None  # An interrupted/partial write is never accepted as ready.
        if not isinstance(value, dict):
            raise EmotionalVITSServiceError("情绪语音 ready 文件格式不正确")
        return value

    def stop(self) -> bool:
        """Blocking: cancel startup and stop only our Popen; allow later restart."""
        with self._condition:
            while self._stopping:
                self._condition.wait(0.1)
            self._stopping = True
            self._last_stage = "stopping"
            had_startup = self._starting
            if self._startup_cancel is not None:
                self._startup_cancel.set()
            if self._startup_thread == threading.get_ident():
                self._stopping = False
                self._condition.notify_all()
                return had_startup
            while self._starting:
                self._condition.wait(0.1)
            owned = self._owned
        try:
            if owned is not None:
                self._dispose(owned)
            return had_startup or owned is not None
        finally:
            with self._condition:
                self._stopping = False
                self._last_stage = "shutdown" if self._shutting_down else "stopped"
                self._condition.notify_all()

    def shutdown(self) -> None:
        """Permanently close this manager, including its own lifetime guard."""
        with self._condition:
            self._shutting_down = True
        try:
            self.stop()
        finally:
            with self._condition:
                close_guard = not self._guard_closed
                self._guard_closed = True
            if close_guard:
                self._process_job.close()

    def _dispose(self, child: _OwnedChild) -> None:
        self._terminate_process(child.process)
        child.log_file.close()
        self._remove_owned_files(child.directory)
        with self._condition:
            if self._owned is child:
                self._owned = None

    @staticmethod
    def _terminate_process(process) -> None:
        if process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            if process.poll() is None:
                try:
                    process.kill()
                except OSError as exc:
                    if process.poll() is None:
                        raise EmotionalVITSServiceError("无法终止自有情绪语音子进程") from exc
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired as exc:
                    # Keep ownership and its guard instead of forgetting a live child.
                    raise EmotionalVITSServiceError("自有情绪语音子进程未能退出") from exc
        if process.poll() is None:
            raise EmotionalVITSServiceError("自有情绪语音子进程仍在运行")

    @staticmethod
    def _remove_owned_files(directory: Path) -> None:
        # Never recurse: an unexpected file is preserved, even in our run directory.
        # Preserve service.log for startup failure and acceptance evidence.
        for name in ("ready.json", "ready.json.tmp"):
            try:
                (directory / name).unlink(missing_ok=True)
            except OSError:
                pass
        try:
            directory.rmdir()
        except OSError:
            pass

    @staticmethod
    def _log_tail(child: _OwnedChild) -> str:
        try:
            with (child.directory / "service.log").open("rb") as stream:
                stream.seek(max(0, os.fstat(stream.fileno()).st_size - 4_000))
                return stream.read(4_000).decode("utf-8", errors="replace").replace(child.token, "<redacted>").strip()
        except OSError:
            return ""


_SERVICE_MANAGER: EmotionalVITSServiceManager | None = None
_SINGLETON_LOCK = threading.Lock()


def get_emotional_vits_service_manager() -> EmotionalVITSServiceManager:
    global _SERVICE_MANAGER
    with _SINGLETON_LOCK:
        if _SERVICE_MANAGER is None:
            _SERVICE_MANAGER = EmotionalVITSServiceManager()
        return _SERVICE_MANAGER


def shutdown_emotional_vits_service() -> None:
    with _SINGLETON_LOCK:
        manager = _SERVICE_MANAGER
    if manager is not None:
        manager.shutdown()


atexit.register(shutdown_emotional_vits_service)
