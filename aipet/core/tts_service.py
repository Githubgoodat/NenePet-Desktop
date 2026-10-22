"""Shared GPT-SoVITS service orchestration."""

from __future__ import annotations

import atexit
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from aipet.core.autodl_tts import AutoDLTTSConnection
from aipet.core.config import TTSSettings, get_cache_dir
from aipet.core.credentials import CredentialError, unprotect_secret
from aipet.core.network import is_loopback_url
from aipet.core.runtime_logging import get_logger
from aipet.core.tts_assets import (
    TTSAssetState,
    locate_tts_assets,
    probe_tts_service,
    tts_service_is_reachable,
)
from aipet.platforms import PlatformRuntime, get_platform_runtime
from aipet.core.voice_profile import (
    VoiceProfileError,
    tts_base_url,
    voice_profile_path,
    write_profile_engine_config,
)


ProgressCallback = Callable[[str], None]
logger = get_logger("tts-service")


class TTSServiceError(RuntimeError):
    pass


class TTSServiceCancelled(TTSServiceError):
    """An explicit cancellation stopped an owned local startup."""


class LocalTTSServiceManager:
    """Own at most one GPT-SoVITS API process for the current AIpet process."""

    def __init__(
        self,
        platform_runtime: PlatformRuntime | None = None,
    ) -> None:
        self._platform_runtime = (
            platform_runtime or get_platform_runtime()
        )
        self._condition = threading.Condition(threading.RLock())
        self._process: subprocess.Popen[bytes] | None = None
        self._autodl_connection: AutoDLTTSConnection | None = None
        self._process_job = (
            self._platform_runtime.processes.create_child_process_guard()
        )
        self._log_file = None
        self._server_address: tuple[str, int] | None = None
        self._profile_signature: str | None = None
        self._starting = False
        self._shutting_down = False
        self._cancel_start = threading.Event()
        self._startup_started_at: float | None = None
        self._last_stage = "idle"
        self._last_health_detail = ""
        self._log_start_offset = 0
        self._ready_marker: Path | None = None
        self._last_process_pid: int | None = None
        self._last_process_exit_code: int | None = None

    def is_starting(self) -> bool:
        with self._condition:
            return self._starting

    def owns_running_process(self) -> bool:
        with self._condition:
            return self._process_is_running_locked() or (
                self._autodl_connection is not None
                and self._autodl_connection.is_active()
            )

    def ensure_running(
        self,
        settings: TTSSettings,
        *,
        state: TTSAssetState | None = None,
        progress: ProgressCallback | None = None,
        password: str = "",
        cancelled: Callable[[], bool] | None = None,
    ) -> bool:
        """Ensure the selected TTS API is reachable; return True if started."""

        with self._condition:
            if self._shutting_down:
                raise TTSServiceError("AIpet is shutting down.")
        self._raise_if_cancelled(cancelled, internal=False)
        configured_profile = voice_profile_path(settings)
        profile = None
        assets = state
        if configured_profile:
            if settings.uses_autodl():
                raise TTSServiceError("角色语音配置仅支持本地 GPT-SoVITS")
            try:
                if assets is None or assets.voice_profile is None:
                    assets = locate_tts_assets(
                        configured_engine_root=settings.engine_root,
                        configured_voice_profile=configured_profile,
                    )
                profile = assets.voice_profile
                if profile is None or profile.manifest_path != Path(configured_profile).expanduser().resolve():
                    raise VoiceProfileError("TTS 资源与所选角色语音配置不匹配")
                configured_engine = Path(settings.engine_root).expanduser().resolve()
                if profile.engine_root != configured_engine or assets.engine_root != profile.engine_root:
                    raise VoiceProfileError("角色语音配置与所选引擎目录不匹配")
            except (VoiceProfileError, OSError) as exc:
                raise TTSServiceError(str(exc)) from exc

        elif assets is not None and assets.voice_profile is not None:
            raise TTSServiceError("默认丛雨配置不能复用角色 profile 的资源状态")

        if (
            not configured_profile
            and is_loopback_url(settings.base_url)
            and urlsplit(settings.base_url).port == 9881
        ):
            raise TTSServiceError("9881 端口保留给角色语音配置；默认丛雨服务请使用其他端口")
        if settings.uses_autodl():
            return self._ensure_autodl_running(
                settings,
                progress=progress,
                password=password,
            )

        deadline = time.monotonic() + settings.timeout_seconds
        base_url = tts_base_url(settings)
        signature = profile.signature if profile else None
        health = probe_tts_service(base_url)
        self._last_health_detail = health.detail
        if health.reachable:
            if configured_profile or is_loopback_url(base_url):
                with self._condition:
                    self._check_profile_owner_locked(signature, _local_server_address(base_url))
            logger.info(
                "TTS 服务已在线 | %s | 健康检查=%s",
                base_url,
                health.detail,
            )
            return False
        # Online remote APIs remain usable; only automatic startup needs a
        # loopback address and a bundled local runtime.
        address = _local_server_address(base_url)
        logger.info(
            "TTS 健康检查未通过，将尝试启动本地服务 | %s | 原因=%s",
            base_url,
            health.detail,
        )

        busy_process = None
        with self._condition:
            if self._shutting_down:
                raise TTSServiceError("AIpet is shutting down.")
            while self._starting:
                self._raise_if_cancelled(cancelled, internal=False)
                _report(progress, "waiting_for_existing_start")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TTSServiceError(
                        "Timed out while another TTS startup was in progress."
                    )
                self._condition.wait(min(0.5, remaining))
            if tts_service_is_reachable(base_url):
                self._check_profile_owner_locked(signature, address)
                return False
            if self._process_is_running_locked():
                if self._server_address != address:
                    raise TTSServiceError(
                        "AIpet already manages a TTS service on another address. "
                        "Stop it in Settings before changing the endpoint."
                    )
                self._check_profile_owner_locked(signature, address)
                busy_process = self._process
            else:
                self._close_dead_process_locked()
                self._raise_if_cancelled(cancelled, internal=False)
                self._cancel_start.clear()
                self._starting = True
                self._startup_started_at = time.monotonic()
                self._last_stage = "locating_engine"
                self._last_process_pid = None
                self._last_process_exit_code = None

        if busy_process is not None:
            # api_v2's async /tts handler runs synchronous inference on its
            # single event loop. OpenAPI can time out while an earlier (even
            # client-cancelled) synthesis still runs. Do not restart or stop
            # that owned process, or hold the manager lock during HTTP waits.
            return self._wait_for_owned_api(
                base_url, address, signature, busy_process, deadline,
                progress=progress, cancelled=cancelled,
            )

        process: subprocess.Popen[bytes] | None = None
        try:
            _report(progress, "locating_engine")
            assets = assets or locate_tts_assets(
                configured_engine_root=settings.engine_root,
                configured_model_dir=settings.model_dir,
            )
            if assets.engine_root is None:
                raise TTSServiceError("GPT-SoVITS directory was not found.")
            if not assets.engine_ready:
                raise TTSServiceError(
                    "GPT-SoVITS is incomplete. Download or repair the engine "
                    "before starting its API."
                )
            self._raise_if_cancelled(cancelled)

            if profile is None:
                command, environment = _build_start_command(
                    assets.engine_root,
                    address,
                    self._platform_runtime,
                )
            else:
                profile_config = write_profile_engine_config(profile, get_cache_dir())
                command, environment = _build_start_command(
                    assets.engine_root,
                    address,
                    self._platform_runtime,
                    config_path=profile_config,
                )
            self._last_stage = "starting_process"
            _report(progress, "starting_process")
            self._ready_marker = (
                Path(environment["AIPET_TTS_READY_MARKER"])
                if environment.get("AIPET_TTS_READY_MARKER") else None
            )
            self._raise_if_cancelled(cancelled)
            process = self._launch(
                command,
                environment,
                assets.engine_root,
                address,
            )
            with self._condition:
                self._profile_signature = signature
            self._raise_if_cancelled(cancelled)
            self._last_stage = "waiting_for_api"
            _report(progress, "waiting_for_api")
            next_progress = time.monotonic() + 5
            while time.monotonic() < deadline:
                self._raise_if_cancelled(cancelled)
                exit_code = process.poll()
                if exit_code is not None:
                    raise TTSServiceError(self._startup_failure_detail(
                        f"GPT-SoVITS exited with code {exit_code}.", process,
                    ))
                if tts_service_is_reachable(
                    base_url,
                    timeout=0.75,
                ):
                    self._raise_if_cancelled(cancelled)
                    if process.poll() is not None:
                        raise TTSServiceError(self._startup_failure_detail(
                            "The owned TTS process exited before readiness was confirmed.", process,
                        ))
                    if self._ready_marker is not None:
                        self._ready_marker.write_text("ready\n", encoding="utf-8")
                    self._last_stage = "ready"
                    self._last_health_detail = "OpenAPI 已确认当前服务就绪"
                    _report(progress, "ready")
                    logger.info(
                        "TTS 服务启动完成 | %s | PID %s",
                        base_url,
                        getattr(process, "pid", "unknown"),
                    )
                    return True
                if time.monotonic() >= next_progress:
                    _report(progress, "waiting_for_api")
                    next_progress = time.monotonic() + 5
                self._cancel_start.wait(0.35)

            raise TTSServiceError(self._startup_failure_detail(
                "Timed out waiting for the GPT-SoVITS API to become ready.", process,
            ))
        except BaseException as exc:
            self._last_stage = "cancelled" if isinstance(exc, (TTSServiceCancelled, KeyboardInterrupt)) else "failed"
            if isinstance(exc, (TTSServiceCancelled, KeyboardInterrupt, SystemExit)):
                logger.info("TTS 服务启动已中止；正在清理本进程拥有的子进程")
            else:
                logger.exception("TTS 服务启动失败")
            if process is not None:
                self._terminate_process(process)
                self._last_process_exit_code = process.poll()
            with self._condition:
                if self._process is process:
                    self._process = None
                    self._server_address = None
                    self._profile_signature = None
                    self._close_log_locked()
            raise
        finally:
            with self._condition:
                self._starting = False
                self._condition.notify_all()

    def _wait_for_owned_api(
        self,
        base_url: str,
        address: tuple[str, int],
        signature: str | None,
        process,
        deadline: float,
        *,
        progress: ProgressCallback | None,
        cancelled: Callable[[], bool] | None,
    ) -> bool:
        """Wait for an existing owned API without assuming it is dead or idle.

        Cancelling this caller never enters startup cleanup and never kills a
        process used by other callers. stop()/shutdown() still own that policy.
        """
        logger.info("已拥有的 TTS API 暂未应答，等待恢复 | %s | PID %s",
                    base_url, getattr(process, "pid", "unknown"))
        _report(progress, "waiting_for_api")
        while True:
            with self._condition:
                self._check_waiting_owner_locked(process, signature, address, cancelled)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TTSServiceError(
                    "Timed out waiting for the owned GPT-SoVITS API to respond "
                    f"(PID={getattr(process, 'pid', 'unknown')}). "
                    "It may still be processing an earlier synthesis; the process was not stopped."
                )
            # The probe has separate connect/read timeouts. Bound both by the
            # remaining budget; ordinary cancellation is checked between them.
            health = probe_tts_service(base_url, timeout=min(0.5, remaining / 2))
            with self._condition:
                self._last_health_detail = health.detail
                # Recheck after network I/O: a stopped/replaced/dead child must
                # never be confused with another service now owning the port.
                self._check_waiting_owner_locked(process, signature, address, cancelled)
                if health.reachable:
                    logger.info("已拥有的 TTS API 已恢复应答 | %s | PID %s",
                                base_url, getattr(process, "pid", "unknown"))
                    return False
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    self._condition.wait(min(0.25, remaining))

    def _check_waiting_owner_locked(self, process, signature, address, cancelled) -> None:
        self._raise_if_cancelled(cancelled)
        if self._shutting_down:
            raise TTSServiceError("AIpet is shutting down.")
        if self._process is not process:
            raise TTSServiceError("The owned TTS process changed while waiting; retry with the current settings.")
        exit_code = process.poll()
        if exit_code is not None:
            self._last_process_exit_code = exit_code
            self._close_dead_process_locked()
            raise TTSServiceError(f"The owned GPT-SoVITS process exited with code {exit_code} while waiting.")
        if self._server_address != address:
            raise TTSServiceError("The owned TTS address changed while waiting; stop it before changing the endpoint.")
        self._check_profile_owner_locked(signature, address)

    def stop(self) -> bool:
        """Stop only services and SSH sessions launched by this AIpet process."""

        with self._condition:
            self._cancel_start.set()
            self._condition.notify_all()
            process = self._process
            autodl_connection = self._autodl_connection
            self._autodl_connection = None
            if process is None and autodl_connection is None:
                self._close_log_locked()
                return False

        if process is not None:
            self._terminate_process(process)
            self._last_process_exit_code = process.poll()
            logger.info(
                "TTS 服务已停止 | PID %s",
                getattr(process, "pid", "unknown"),
            )
        if autodl_connection is not None:
            autodl_connection.stop()
            logger.info("AutoDL TTS SSH 会话已关闭")
        with self._condition:
            if self._process is process:
                self._process = None
                self._server_address = None
                self._profile_signature = None
                self._close_log_locked()
            self._condition.notify_all()
        return True

    def _raise_if_cancelled(self, cancelled, *, internal: bool = True) -> None:
        if (internal and self._cancel_start.is_set()) or (cancelled is not None and cancelled()):
            raise TTSServiceCancelled("TTS 启动已取消；只清理当前进程拥有的服务")

    def startup_diagnostics(self) -> dict:
        """Return bounded diagnostics without probing ports or loading models."""
        with self._condition:
            process = self._process
            started = self._startup_started_at
            return {
                "stage": self._last_stage,
                "pid": getattr(process, "pid", None) if process is not None else self._last_process_pid,
                "process_exit_code": process.poll() if process is not None else self._last_process_exit_code,
                "address": self._server_address,
                "elapsed_seconds": time.monotonic() - started if started is not None else None,
                "last_health_detail": self._last_health_detail,
                "log_path": str(get_cache_dir() / "logs/gpt-sovits-service.log"),
                "log_tail": self._read_log_tail(),
            }

    def _startup_failure_detail(self, message: str, process) -> str:
        elapsed = (
            time.monotonic() - self._startup_started_at
            if self._startup_started_at is not None else 0
        )
        detail = self._read_log_tail()
        return (
            f"{message} PID={getattr(process, 'pid', 'unknown')}; "
            f"elapsed={elapsed:.1f}s; stage={self._last_stage}; "
            f"log={get_cache_dir() / 'logs/gpt-sovits-service.log'}\n"
            + (detail or "No child output was captured; startup progress is unverified.")
        )

    def _ensure_autodl_running(
        self,
        settings: TTSSettings,
        *,
        progress: ProgressCallback | None,
        password: str,
    ) -> bool:
        with self._condition:
            active_connection = self._autodl_connection
        if (
            active_connection is not None
            and active_connection.is_active()
            and tts_service_is_reachable(settings.base_url)
        ):
            logger.info("AutoDL TTS 服务已在线 | %s", settings.base_url)
            return False
        health = probe_tts_service(settings.base_url)
        if health.reachable:
            raise TTSServiceError(
                "The local TTS port is occupied by a service that was not "
                "started through the current AutoDL SSH session. Stop that "
                "service or use another local port."
            )
        logger.info(
            "AutoDL TTS 健康检查未通过，将尝试建立 SSH 会话 | %s | "
            "原因=%s",
            settings.base_url,
            health.detail,
        )

        address = _local_server_address(settings.base_url)
        deadline = time.monotonic() + settings.timeout_seconds
        with self._condition:
            if self._shutting_down:
                raise TTSServiceError("AIpet is shutting down.")
            while self._starting:
                _report(progress, "waiting_for_existing_start")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TTSServiceError(
                        "Timed out while another TTS startup was in progress."
                    )
                self._condition.wait(min(0.5, remaining))
            if tts_service_is_reachable(settings.base_url):
                active_connection = self._autodl_connection
                if (
                    active_connection is not None
                    and active_connection.is_active()
                ):
                    return False
                raise TTSServiceError(
                    "The local TTS port is occupied by another service."
                )
            if (
                self._autodl_connection is not None
                and self._autodl_connection.is_active()
            ):
                raise TTSServiceError(
                    "The AutoDL SSH session is active, but its TTS API is not "
                    "reachable. Stop it in Settings and try again."
                )
            self._autodl_connection = None
            self._starting = True

        connection = AutoDLTTSConnection()
        try:
            clear_password = password
            if not clear_password:
                try:
                    clear_password = unprotect_secret(
                        settings.autodl_password_encrypted
                    )
                except CredentialError as exc:
                    raise TTSServiceError(str(exc)) from exc
            connection.start(
                settings.autodl_ssh_command,
                clear_password,
                settings.autodl_remote_command,
                local_address=address,
                remote_address=("127.0.0.1", 9880),
                progress=progress,
            )
            with self._condition:
                self._autodl_connection = connection

            _report(progress, "waiting_for_api")
            while time.monotonic() < deadline:
                if tts_service_is_reachable(
                    settings.base_url,
                    timeout=0.75,
                ):
                    _report(progress, "ready")
                    logger.info(
                        "AutoDL TTS 启动完成 | %s",
                        settings.base_url,
                    )
                    return True
                if not connection.is_active():
                    detail = connection.output_tail()
                    suffix = f"\n{detail}" if detail else ""
                    raise TTSServiceError(
                        "The AutoDL SSH session ended before TTS became ready."
                        + suffix
                    )
                time.sleep(0.35)

            detail = connection.output_tail()
            suffix = f"\n{detail}" if detail else ""
            raise TTSServiceError(
                "Timed out waiting for the AutoDL TTS API to become ready."
                + suffix
            )
        except Exception:
            logger.exception("AutoDL TTS 启动失败")
            connection.stop()
            with self._condition:
                if self._autodl_connection is connection:
                    self._autodl_connection = None
            raise
        finally:
            with self._condition:
                self._starting = False
                self._condition.notify_all()

    def shutdown(self) -> None:
        with self._condition:
            self._shutting_down = True
        try:
            self.stop()
        finally:
            self._process_job.close()

    def autodl_reference(
        self,
        settings: TTSSettings,
        emotion: str,
    ) -> tuple[str, str]:
        with self._condition:
            connection = self._autodl_connection
        if (
            connection is None
            or not connection.is_active()
            or not settings.uses_autodl()
        ):
            raise TTSServiceError(
                "The AutoDL SSH session is not active."
            )
        try:
            return connection.read_reference_metadata(
                settings.autodl_remote_reference_root,
                emotion,
            )
        except Exception as exc:
            raise TTSServiceError(str(exc)) from exc

    def _launch(
        self,
        command: list[str],
        environment: dict[str, str],
        engine_root: Path,
        address: tuple[str, int],
    ) -> subprocess.Popen[bytes]:
        log_path = get_cache_dir() / "logs" / "gpt-sovits-service.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = log_path.open("ab", buffering=0)
        self._log_start_offset = log_path.stat().st_size
        log_file.write(
            (
                f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
                "Starting GPT-SoVITS API\n"
            ).encode("utf-8")
        )
        kwargs = (
            self._platform_runtime.processes.hidden_subprocess_options()
        )
        try:
            process = subprocess.Popen(
                command,
                cwd=str(engine_root),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                **kwargs,
            )
        except Exception:
            log_file.close()
            raise
        try:
            self._process_job.assign(process)
        except OSError as exc:
            logger.warning(
                "无法把 TTS 子进程绑定到桌宠生命周期：%s",
                exc,
            )
        with self._condition:
            self._process = process
            self._last_process_pid = getattr(process, "pid", None)
            self._log_file = log_file
            self._server_address = address
        logger.info(
            "TTS 子进程已创建 | PID %s | 原始日志=%s",
            getattr(process, "pid", "unknown"),
            log_path,
        )
        return process

    def _process_is_running_locked(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def _check_profile_owner_locked(
        self,
        signature: str | None,
        address: tuple[str, int],
    ) -> None:
        owned = self._process_is_running_locked() and self._server_address == address
        if signature is not None and (not owned or self._profile_signature != signature):
            raise TTSServiceError(
                "角色语音端口 9881 已被其他服务占用，或配置已改变。"
                "请先停止该服务；不会复用或切换未经确认的模型。"
            )
        if signature is None and owned and self._profile_signature is not None:
            raise TTSServiceError("请先停止角色语音服务，再切换到默认丛雨语音")

    def _close_dead_process_locked(self) -> None:
        if self._process is not None and self._process.poll() is not None:
            self._process = None
            self._server_address = None
            self._profile_signature = None
            self._close_log_locked()

    def _close_log_locked(self) -> None:
        if self._log_file is not None:
            try:
                self._log_file.close()
            except OSError:
                pass
            self._log_file = None

    @staticmethod
    def _terminate_process(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        except OSError:
            pass

    def _read_log_tail(self) -> str:
        path = get_cache_dir() / "logs" / "gpt-sovits-service.log"
        try:
            with path.open("rb") as stream:
                stream.seek(max(self._log_start_offset, path.stat().st_size - 12_000))
                data = stream.read(12_000)
        except OSError:
            return ""
        return data.decode("utf-8", errors="replace").strip()


def _local_server_address(base_url: str) -> tuple[str, int]:
    parsed = urlsplit(base_url)
    if parsed.scheme != "http" or not is_loopback_url(base_url):
        raise TTSServiceError(
            "Automatic startup is available only for a local HTTP TTS endpoint."
        )
    host = parsed.hostname or "127.0.0.1"
    if host.lower() == "localhost":
        host = "127.0.0.1"
    try:
        port = parsed.port or 80
    except ValueError as exc:
        raise TTSServiceError("The TTS endpoint contains an invalid port.") from exc
    return host, port


def _build_start_command(
    engine_root: Path,
    address: tuple[str, int],
    platform_runtime: PlatformRuntime | None = None,
    *,
    config_path: Path | None = None,
) -> tuple[list[str], dict[str, str]]:
    python = _locate_runtime_python(engine_root, platform_runtime)
    api_script = engine_root / "api_v2.py"
    config = config_path or engine_root / "GPT_SoVITS" / "configs" / "tts_infer.yaml"
    if not api_script.is_file() or not config.is_file():
        raise TTSServiceError("GPT-SoVITS API files are incomplete.")

    host, port = address
    command = [
        str(python),
        str(api_script),
        "-a",
        host,
        "-p",
        str(port),
        "-c",
        str(config),
    ]
    environment = os.environ.copy()
    runtime_dir = str(python.parent)
    environment["PATH"] = (
        runtime_dir
        + os.pathsep
        + environment.get("PATH", "")
    )
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONUNBUFFERED"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    if config_path is not None:
        launcher = _write_profile_launcher(config_path.parent)
        marker = config_path.parent / f"ready-{uuid.uuid4().hex}.marker"
        environment["AIPET_TTS_READY_MARKER"] = str(marker)
        command = [str(python), "-u", "-X", "faulthandler", str(launcher)] + command[1:]
    return command, environment


def _write_profile_launcher(directory: Path) -> Path:
    """Trace the Python entry and slow imports without editing the engine."""
    launcher = directory / "startup.py"
    source = '''import faulthandler, os, runpy, sys, threading, time
from pathlib import Path
faulthandler.enable()
marker = Path(os.environ["AIPET_TTS_READY_MARKER"])
faulthandler.dump_traceback_later(30, repeat=True)
def wait_for_ready():
    while not marker.is_file():
        time.sleep(0.2)
    faulthandler.cancel_dump_traceback_later()
threading.Thread(target=wait_for_ready, daemon=True).start()
api = Path(sys.argv[1]).resolve()
sys.argv = sys.argv[1:]
sys.path.insert(0, str(api.parent))
print("STARTUP Python entered:", sys.version, flush=True)
print("STARTUP importing API:", api, flush=True)
try:
    runpy.run_path(str(api), run_name="__main__")
finally:
    faulthandler.cancel_dump_traceback_later()
'''
    directory.mkdir(parents=True, exist_ok=True)
    launcher.write_text(source, encoding="utf-8")
    return launcher.resolve()


def _locate_runtime_python(
    engine_root: Path,
    platform_runtime: PlatformRuntime | None = None,
) -> Path:
    candidates = (
        (platform_runtime or get_platform_runtime())
        .processes.runtime_python_candidates(engine_root)
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.absolute()
    raise TTSServiceError(
        "GPT-SoVITS does not contain a bundled Python runtime."
    )


def _report(callback: ProgressCallback | None, stage: str) -> None:
    if callback is not None:
        callback(stage)


_SERVICE_MANAGER: LocalTTSServiceManager | None = None


def get_tts_service_manager(
    platform_runtime: PlatformRuntime | None = None,
) -> LocalTTSServiceManager:
    global _SERVICE_MANAGER
    if _SERVICE_MANAGER is None:
        _SERVICE_MANAGER = LocalTTSServiceManager(platform_runtime)
    return _SERVICE_MANAGER


def shutdown_tts_service() -> None:
    try:
        if _SERVICE_MANAGER is not None:
            _SERVICE_MANAGER.shutdown()
    finally:
        # Both backends are owned by this application and share its graceful
        # exit path. This import must never load a model into the Qt process.
        from aipet.core.emotional_vits_service import shutdown_emotional_vits_service
        shutdown_emotional_vits_service()


atexit.register(shutdown_tts_service)

