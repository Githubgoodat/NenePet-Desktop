"""Shared Qt settings dialog."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Event

from pydantic import ValidationError
from PyQt5.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtMultimedia import QSound
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from aipet.core.download_manager import (
    TTS_JOB_ID,
    DownloadManager,
    DownloadSnapshot,
    whisper_job_id,
)
from aipet.core.audio_devices import (
    decode_audio_input_device,
    refresh_audio_input_devices,
)
from aipet.core.backends import create_backend, create_vision_backend
from aipet.core.cache import clear_runtime_cache
from aipet.core.config import (
    APISettings,
    AppSettings,
    CharacterSettings,
    DisplaySettings,
    IdleSettings,
    InteractionSettings,
    OllamaSettings,
    STTSettings,
    TTSSettings,
    VisionSettings,
    get_user_data_dir,
    load_personality,
    tts_paths_for_storage_root,
)
from aipet.core.runtime_logging import get_logger
from aipet.core.stt_languages import COMMON_WHISPER_LANGUAGES
from aipet.core.tts_assets import (
    TTSAssetState,
    configure_local_tts_weights,
    locate_tts_assets,
    tts_service_is_reachable,
)
from aipet.core.tts_service import (
    TTSServiceError,
    get_tts_service_manager,
)
from aipet.core.voice_profile import EMOTIONS, PROFILE_BASE_URL, tts_base_url
from aipet.core.whisper_models import (
    WHISPER_MODELS,
    find_local_model,
    looks_like_local_model_path,
    model_repository,
)
from aipet.platforms import (
    CredentialError,
    PlatformRuntime,
    get_platform_runtime,
)


TRANSLATIONS: dict[str, dict[str, str]] = {
    "en": {
        "title_setup": "AIpet Initial Setup",
        "title_settings": "AIpet Settings",
        "language": "Language / 语言",
        "tab_models": "Language models",
        "tab_extensions": "Extensions",
        "tab_character": "Character",
        "tab_automation": "Automation",
        "tab_display": "Display",
        "tab_other": "Other",
        "backend_group": "Backend mode",
        "mode": "Mode",
        "mode_ollama": "Ollama (local service)",
        "mode_api": "Cloud API",
        "ollama_group": "Ollama",
        "server_url": "Server URL",
        "chat_model": "Chat model",
        "vision_model": "Vision model",
        "context_window": "Context window",
        "request_timeout": "Request timeout",
        "keep_alive": "Keep alive",
        "api_group": "OpenAI-compatible cloud API",
        "provider": "Provider",
        "provider_aliyun": "Alibaba Cloud Model Studio",
        "provider_openai": "OpenAI",
        "deepseek_api_key": "API key / DEEPSEEK_API_KEY",
        "aliyun_api_key": "API key / DASHSCOPE_API_KEY",
        "openai_api_key": "API key / OPENAI_API_KEY",
        "base_url": "Base URL",
        "deepseek_thinking": "Enable DeepSeek thinking mode (slower)",
        "deepseek_thinking_max_tokens": (
            "Thinking mode output limit (tokens, including reasoning and dialogue)"
        ),
        "deepseek_note": (
            "DeepSeek V4 is used for chat. Choose the independent vision "
            "backend under Extensions."
        ),
        "load_models": "Test connection & load models",
        "model_list_help": (
            "Uses Ollama /api/tags or the provider's /models endpoint. "
            "Model fields stay editable for custom or unlisted IDs."
        ),
        "load_vision_models": "Test connection & load vision models",
        "vision_model_list_help": (
            "Loads the complete model list from the vision provider and "
            "keeps the field editable for custom model IDs."
        ),
        "identity_group": "Identity",
        "user_name": "User name",
        "portrait_set": "Default portrait set",
        "portrait_a": "Portrait A",
        "portrait_b": "Portrait B",
        "outfit": "Default outfit",
        "outfit_sleepwear": "Sleepwear",
        "outfit_casual": "Casual outfit",
        "outfit_uniform": "School uniform",
        "outfit_kimono": "Purple kimono",
        "prompt_group": "Personality prompt",
        "prompt_help": (
            "This is the personality layer. AIpet adds the structured JSON "
            "contract automatically, so do not add output-format rules here."
        ),
        "prompt_placeholder": (
            "Describe the character, speaking style, relationship, and boundaries…"
        ),
        "import_prompt": "Import prompt from text file…",
        "vision_group": "Screen vision",
        "vision_provider": "Vision backend",
        "vision_provider_ollama": "Ollama (local service)",
        "vision_provider_aliyun": "Alibaba Cloud Model Studio",
        "vision_provider_openai": "OpenAI",
        "vision_enabled": (
            "Check the selected screen and react only to significant changes"
        ),
        "interval": "Interval",
        "vision_supported": (
            "The first screenshot establishes a baseline. Near-identical "
            "screens stay local; automatic reactions share a 3-minute cooldown. "
            "The vision backend is independent from the chat backend."
        ),
        "tts_group": "Text-to-speech",
        "whisper_group": "Speech input (Whisper)",
        "tts_enabled": "Enable text-to-speech",
        "tts_backend": "TTS backend",
        "tts_backend_local": "Local computer",
        "tts_backend_autodl": "AutoDL cloud",
        "tts_backend_emotional_vits": "Nene Emotional VITS (experimental)",
        "tts_emotional_vits_profile": "Nene Emotional VITS profile (JSON)",
        "tts_emotional_vits_checking": "Checking Nene Emotional VITS configuration paths…",
        "tts_emotional_vits_failed": "Nene Emotional VITS configuration is not ready: {message}",
        "tts_emotional_vits_state": (
            "Nene Emotional VITS (experimental): configuration paths checked; "
            "pinned files are verified when starting the service. "
            "Candidate mappings: {configured}; unmapped: {missing}. "
            "Profile status: {status}. The six emotions have not been approved by listening. {service}"
        ),
        "tts_emotional_vits_offline": "The managed CPU service is not running.",
        "tts_emotional_vits_online": "The managed service is ready for this configuration; no audio was auditioned by this check.",
        "tts_emotional_vits_changed": "A service with another configuration is running. Stop it before starting this configuration.",
        "tts_emotional_vits_starting": "The managed service is starting or stopping.",
        "tts_emotional_vits_missing_path": "Choose a Nene Emotional VITS JSON configuration.",
        "tts_emotional_vits_bank_route": "bank ID",
        "tts_emotional_vits_vector_route": "reference vector",
        "tts_emotional_vits_audition": "Audition and choose Nene emotions…",
        "tts_emotional_vits_audition_saved": "Experimental profile saved: {path}. To use it, select the Nene Emotional VITS backend and click OK. Your current voice has not been switched.",
        "tts_endpoint": "TTS endpoint",
        "tts_voice_profile": "Character voice profile (JSON)",
        "tts_profile_state": "Configured emotions: {configured}; missing: {missing}. {service}",
        "tts_profile_baseline_state": (
            "{character}'s daily voice is available; emotions are not yet calibrated. "
            "Separate emotion references: {configured}; missing: {missing}. {service}"
        ),
        "tts_profile_failed": "Character voice profile is not ready: {message}",
        "tts_profile_incomplete_engine": "The GPT-SoVITS base assets are incomplete.",
        "tts_profile_external": "Port 9881 is occupied by an unverified service; stop it before starting this profile.",
        "external_character_note": "Control the Live2D model, outfits, size and position through Live2DViewerEX.",
        "tts_timeout": "TTS timeout",
        "tts_storage_root": "TTS resource storage location",
        "tts_storage_preview": (
            "Automatic directories:\n"
            "Engine: {engine}\n"
            "Voice model: {model}"
        ),
        "tts_advanced_paths": "Advanced",
        "tts_engine_root": "Exact GPT-SoVITS installation directory",
        "tts_bootstrap": "Install macOS GPT-SoVITS base environment",
        "tts_model_dir": "Exact voice model directory (includes references)",
        "tts_autodl_ssh_command": "AutoDL SSH login command",
        "tts_autodl_password": "AutoDL SSH password",
        "tts_autodl_remote_command": "Remote TTS start command",
        "tts_autodl_reference_root": "Remote reference voice directory",
        "tts_autodl_password_placeholder": (
            "Leave blank to keep the password saved by the operating system"
        ),
        "browse": "Browse…",
        "tts_disabled": (
            "Enable TTS to check the engine, voice model, references, and service."
        ),
        "tts_invalid": "Enter a valid TTS endpoint before checking it.",
        "tts_autodl_missing": (
            "Enter the AutoDL SSH login command, password, remote start "
            "command, and remote reference voice directory."
        ),
        "tts_password_store_failed": (
            "The operating system could not save the AutoDL password "
            "securely: {message}"
        ),
        "tts_checking": "Checking GPT-SoVITS components…",
        "tts_ready_online": "Voice model is ready and the TTS service is online.",
        "tts_ready_offline": (
            "Voice model is ready, but the TTS service is not running."
        ),
        "tts_service_start": "Start TTS service",
        "tts_service_stop": "Stop TTS service",
        "tts_service_online": "TTS service online",
        "tts_service_locating": "Locating the GPT-SoVITS runtime…",
        "tts_service_starting": "Starting the GPT-SoVITS process…",
        "tts_service_connecting_ssh": "Connecting to AutoDL over SSH…",
        "tts_service_starting_remote": "Running the AutoDL TTS start command…",
        "tts_service_starting_tunnel": "Opening the local SSH tunnel…",
        "tts_service_waiting": "Waiting for the TTS API to become ready…",
        "tts_service_waiting_existing": (
            "Another request is starting the TTS service; waiting…"
        ),
        "tts_service_loading_weights": "Loading the character voice weights…",
        "tts_service_stopping": "Stopping the TTS service…",
        "tts_service_started": "The TTS service is ready.",
        "tts_service_stopped": "The TTS service has stopped.",
        "tts_service_failed": "TTS service operation failed: {message}",
        "tts_model_missing": "The Murasame voice model is missing.",
        "tts_engine_missing": (
            "The voice model is ready, but the GPT-SoVITS directory was not found."
        ),
        "tts_engine_incomplete": (
            "The voice model is ready, but GPT-SoVITS base assets are incomplete."
        ),
        "tts_bootstrap_consent_title": "Install macOS GPT-SoVITS",
        "tts_bootstrap_consent_body": (
            "Download the pinned GPT-SoVITS source, an isolated Python runtime, "
            "Python dependencies, and required base assets for Apple Silicon? "
            "The upstream base assets are about 5 GB. "
            "Character voice weights and reference voices are not included and "
            "remain separate on-demand downloads."
        ),
        "tts_bootstrap_installing": (
            "Installing the macOS GPT-SoVITS base environment: {detail}"
        ),
        "tts_bootstrap_installed": (
            "The macOS GPT-SoVITS base environment is ready: {path}"
        ),
        "tts_bootstrap_failed": (
            "macOS GPT-SoVITS installation failed: {message}"
        ),
        "tts_references_missing": "One or more reference voice files are missing.",
        "tts_external_online": "The external TTS endpoint is reachable.",
        "tts_external_offline": "The external TTS endpoint is not reachable.",
        "tts_download_consent_title": "Download Murasame voice model",
        "tts_download_consent_body": (
            "Download the GPT and SoVITS character weights and six reference "
            "voices (about 231 MB) from ModelScope? The assets are intended "
            "for non-commercial, educational use and reference proprietary "
            "character material."
        ),
        "tts_download_consent_body_with_engine": (
            "GPT-SoVITS was not detected. Download the complete engine package "
            "selected for your NVIDIA GPU together with the character weights "
            "and reference voices? The download is about 8–9 GB and requires "
            "additional free space while extracting. The assets are intended "
            "for non-commercial, educational use."
        ),
        "tts_download": "Download voice model",
        "tts_preparing": "Preparing the TTS download: {detail}",
        "tts_downloading": "Downloading the Murasame voice model: {detail}",
        "tts_checking_files": "Checking downloaded files: {detail}",
        "tts_extracting": "Extracting GPT-SoVITS: {detail}",
        "tts_installing": "Installing GPT-SoVITS: {detail}",
        "tts_cleaning": "Cleaning temporary files: {detail}",
        "tts_downloaded": "Voice model download completed: {path}",
        "tts_download_failed": "Voice model download failed: {message}",
        "download_files": "files",
        "download_steps": "steps",
        "stt_enabled": "Hold {shortcut} for speech input",
        "whisper_model": "Whisper model or repository ID",
        "whisper_model_dir": "Whisper model download directory",
        "stt_language": "Recognition language",
        "stt_language_ui": "Follow interface language",
        "stt_language_auto": "Detect automatically",
        "stt_language_help": (
            "Choose a common language or enter a two- or three-letter Whisper "
            "language code, such as sv for Swedish. English-only models whose "
            "names end in .en cannot recognize other languages."
        ),
        "stt_input_device": "Recording device",
        "stt_input_default": "System default ({device})",
        "stt_input_default_unknown": "System default input",
        "stt_input_unavailable": "Unavailable: {device}",
        "stt_device": "STT device",
        "stt_device_auto": "Auto",
        "stt_device_cuda": (
            "CUDA (requires the AIpet-with-cuda build; unavailable otherwise)"
        ),
        "stt_device_cpu": "CPU",
        "whisper_download": "Download model",
        "whisper_disabled": (
            "Enable speech input to check the selected download directory."
        ),
        "whisper_checking": "Checking the selected model directory…",
        "whisper_installed": "Available locally: {path}",
        "whisper_missing": (
            "The selected model is not available in the download directory. "
            "Choose a directory, then download the model."
        ),
        "whisper_path_missing": (
            "The entered local model directory is incomplete or does not exist."
        ),
        "whisper_downloading": (
            "Downloading {model}: {detail}"
        ),
        "whisper_preparing": "Preparing {model}: {detail}",
        "whisper_verifying": "Checking {model}: {detail}",
        "whisper_downloaded": "Download complete. Available locally: {path}",
        "whisper_download_failed": "Download failed: {message}",
        "download_path_required_title": "Download directory required",
        "tts_storage_path_required": (
            "Select the TTS resource storage location before downloading."
        ),
        "tts_model_path_required": (
            "Select the voice model download directory before downloading."
        ),
        "tts_engine_path_required": (
            "Select the GPT-SoVITS download/install directory before "
            "downloading the engine."
        ),
        "whisper_path_required": (
            "Select the Whisper model download directory before downloading."
        ),
        "download_path_invalid": "The selected directory cannot be used: {message}",
        "tts_download_path_unsafe": (
            "The selected TTS directories are unsafe: {message}"
        ),
        "automation_group": "Idle behavior and memory",
        "response_target": "Ordinary conversation target (seconds)",
        "important_response_target": "Important conversation target (seconds)",
        "response_unlimited": "Unlimited",
        "response_custom": "Custom…",
        "response_custom_placeholder": "Enter 0–600 seconds; 0 means unlimited",
        "response_target_help": (
            "A soft target covering reply generation and speech preparation, "
            "not a guaranteed deadline. A ready reply starts immediately; "
            "after the target, show progress and keep processing until complete "
            "or cancelled. This does not change API or speech-service timeouts. "
            "Choose a preset or type 0–600 seconds; 0 means unlimited."
        ),
        "do_not_disturb_help": (
            "Pause replies, speech, screen observation, and new memories. "
            "Keep silent character animation and the resume controls available. "
            "Discard unfinished replies; nothing is replayed after resuming."
        ),
        "settings_help": "Explain these settings",
        "automation_help_title": "Idle behavior and memory",
        "automation_help_body": (
            "Thinking reminder: after this much inactivity, the character may "
            "gently check whether you are still there. It triggers at most "
            "once per idle period.\n\n"
            "Away reminder: after the longer inactivity threshold, the "
            "character treats you as away. When you return, she may welcome "
            "you back. This value must be greater than the thinking reminder."
            "\n\n"
            "Conversation memory: the maximum number of recent messages kept "
            "and supplied as conversation context. Nene also retains completed "
            "conversations locally for relevant recall; lowering this number "
            "does not erase them. Use Clear conversation history to remove "
            "them and the associated ongoing feelings, plans, and relationship state.\n\n"
            "Response time: a soft target for reply and speech preparation. "
            "Ready replies start immediately; after the target, processing "
            "continues with a status message. It is not a guaranteed deadline.\n\n"
            "Do not disturb: pauses replies, speech, screen observation, "
            "and new memory updates. Silent character animation remains. "
            "Unfinished replies are discarded and never replayed on resume."
        ),
        "do_not_disturb": "Do not disturb",
        "clear_history": "Clear conversation history…",
        "clear_history_confirm_title": "Clear conversation history?",
        "clear_history_confirm_body": (
            "This immediately removes saved conversations, remembered names "
            "and plans, screen-event memory, and ongoing feelings and relationship state. "
            "The action cannot be undone by cancelling this settings window."
        ),
        "history_cleared": "Conversation history has been cleared.",
        "history_unavailable_first_run": (
            "Conversation history is not available during initial setup."
        ),
        "data_group": "Data management",
        "conversation_history_data": "Conversation history",
        "runtime_cache_data": "Temporary screenshots, speech, and recordings",
        "clear_cache": "Clear cache…",
        "clear_cache_confirm_title": "Clear cached files?",
        "clear_cache_confirm_body": (
            "This removes temporary screenshots, generated speech, and "
            "recordings. Settings, conversation history, models, and logs "
            "are not affected."
        ),
        "cache_cleared": "Cleared {files} cached files, freeing {size}.",
        "cache_empty": "The cache is already empty.",
        "cache_clear_partial": (
            "Cleared {files} cached files ({size}), but {failed} files or "
            "folders are still in use or could not be removed."
        ),
        "display_group": "Screen and portrait",
        "display_help_title": "Screen and portrait",
        "display_help_body": (
            "Screen index: the zero-based position in the current screen "
            "list. If the index is unavailable, the primary screen is used."
            "\n\n"
            "Portrait height ratio: the character's target height as a share "
            "of the selected screen's available height.\n\n"
            "Live diagnostic console: opens a live log window for diagnosing "
            "model, TTS, recording, and other runtime problems."
        ),
        "screen_index": "Screen index",
        "portrait_ratio": "Portrait height ratio",
        "show_log_console": "Open live diagnostic console",
        "thinking_reminder": "Thinking reminder",
        "away_reminder": "Away reminder",
        "conversation_memory": "Conversation memory",
        "password_placeholder": (
            "Leave blank when using the environment variable"
        ),
        "seconds": " seconds",
        "minutes": " minutes",
        "messages": " messages",
        "save": "Save",
        "cancel": "Cancel",
        "connecting": "Connecting to the model-list endpoint…",
        "models_empty": (
            "Connection succeeded, but the service returned no models. "
            "You can still type a model ID manually."
        ),
        "models_found": (
            "Connection succeeded. Loaded {count} model(s); "
            "manual input remains enabled."
        ),
        "connection_failed": (
            "Model-list request failed: {message}\n"
            "The current model ID was kept and can be edited manually."
        ),
        "import_title": "Import personality prompt",
        "text_filter": "Text files (*.txt *.md);;All files (*)",
        "import_failed": "Import failed",
        "invalid_settings": "Invalid settings",
        "test_running_title": "Connection test is running",
        "test_running_body": (
            "Please wait for the current connection test to finish."
        ),
        "missing_personality": "Missing personality",
        "missing_personality_body": "Please provide a personality prompt.",
        "missing_key": "Missing API key",
        "missing_key_body": (
            "Enter an API key or set the provider environment variable."
        ),
        "save_failed": "Save failed",
        "download_preparing": "Preparing download…",
    },
    "zh-CN": {
        "title_setup": "AIpet 初始设置",
        "title_settings": "AIpet 设置",
        "language": "界面语言 / Language",
        "tab_models": "语言模型",
        "tab_extensions": "拓展功能",
        "tab_character": "角色",
        "tab_automation": "自动行为",
        "tab_display": "显示",
        "tab_other": "其他",
        "backend_group": "后端模式",
        "mode": "模式",
        "mode_ollama": "Ollama（本地服务）",
        "mode_api": "云端 API",
        "ollama_group": "Ollama",
        "server_url": "服务地址",
        "chat_model": "对话模型",
        "vision_model": "视觉模型",
        "context_window": "上下文长度",
        "request_timeout": "请求超时",
        "keep_alive": "模型驻留时间",
        "api_group": "OpenAI 兼容云端 API",
        "provider": "服务商",
        "provider_aliyun": "阿里云百炼",
        "provider_openai": "OpenAI",
        "deepseek_api_key": "API Key / DEEPSEEK_API_KEY",
        "aliyun_api_key": "API Key / DASHSCOPE_API_KEY",
        "openai_api_key": "API Key / OPENAI_API_KEY",
        "base_url": "基础地址",
        "deepseek_thinking": "启用 DeepSeek 思考模式（响应更慢）",
        "deepseek_thinking_max_tokens": "思考模式总输出上限（token，含推理和最终台词）",
        "deepseek_note": (
            "DeepSeek V4 用于对话；视觉后端请在“拓展功能”中独立选择。"
        ),
        "load_models": "测试连接并加载模型",
        "model_list_help": (
            "Ollama 使用 /api/tags，云端服务使用 /models。"
            "下拉框始终可以手动输入自定义或未列出的模型 ID。"
        ),
        "load_vision_models": "测试连接并加载视觉模型",
        "vision_model_list_help": (
            "从当前视觉服务地址读取完整模型列表；"
            "仍可手动输入自定义模型 ID。"
        ),
        "identity_group": "身份",
        "user_name": "用户名称",
        "portrait_set": "默认立绘组",
        "portrait_a": "立绘 A",
        "portrait_b": "立绘 B",
        "outfit": "默认服装",
        "outfit_sleepwear": "睡衣",
        "outfit_casual": "粉白便衣",
        "outfit_uniform": "校服",
        "outfit_kimono": "紫色和服",
        "prompt_group": "人格提示词",
        "prompt_help": (
            "这里仅描述人格。AIpet 会自动添加结构化 JSON 规则，"
            "请不要在这里重复编写输出格式。"
        ),
        "prompt_placeholder": "描述角色身份、说话风格、关系和行为边界……",
        "import_prompt": "从文本文件导入提示词…",
        "vision_group": "屏幕视觉",
        "vision_provider": "视觉后端",
        "vision_provider_ollama": "Ollama（本地服务）",
        "vision_provider_aliyun": "阿里云百炼",
        "vision_provider_openai": "OpenAI",
        "vision_enabled": "检查所选屏幕，仅在明显变化时主动回应",
        "interval": "间隔",
        "vision_supported": (
            "首次截图只建立基线；近似相同的画面不会送入视觉模型，"
            "所有自动主动回应共用 3 分钟冷却。"
            "视觉后端与语言模型后端相互独立。"
        ),
        "tts_group": "语音合成",
        "whisper_group": "语音输入（Whisper）",
        "tts_enabled": "启用语音合成",
        "tts_backend": "语音后端",
        "tts_backend_local": "本机",
        "tts_backend_autodl": "AutoDL 云端",
        "tts_backend_emotional_vits": "宁宁 Emotional VITS（实验）",
        "tts_emotional_vits_profile": "宁宁 Emotional VITS 配置（JSON）",
        "tts_emotional_vits_checking": "正在检查宁宁 Emotional VITS 配置路径……",
        "tts_emotional_vits_failed": "宁宁 Emotional VITS 配置尚未就绪：{message}",
        "tts_emotional_vits_state": (
            "宁宁 Emotional VITS（实验）：配置路径检查通过，启动服务时仍会校验固定文件。"
            "候选映射：{configured}；未映射：{missing}。配置状态：{status}。"
            "六种情绪尚未试听批准。{service}"
        ),
        "tts_emotional_vits_offline": "受管 CPU 服务未运行。",
        "tts_emotional_vits_online": "受管服务已按此配置就绪；本次检查未试听音频。",
        "tts_emotional_vits_changed": "另一份配置的服务正在运行，请先停止再启动此配置。",
        "tts_emotional_vits_starting": "受管服务正在启动或停止。",
        "tts_emotional_vits_missing_path": "请选择宁宁 Emotional VITS JSON 配置。",
        "tts_emotional_vits_bank_route": "bank ID",
        "tts_emotional_vits_vector_route": "参考向量",
        "tts_emotional_vits_audition": "试听与选择宁宁情绪…",
        "tts_emotional_vits_audition_saved": "已另存实验配置：{path}。如需使用，请选择宁宁 Emotional VITS 后端后点“确定”。当前声线未切换。",
        "tts_endpoint": "TTS 地址",
        "tts_voice_profile": "角色音声配置（JSON）",
        "tts_profile_state": "已配置情绪：{configured}；尚缺：{missing}。{service}",
        "tts_profile_baseline_state": "{character}日常音色已可用；情绪尚未校准。独立情绪参考：{configured}；尚缺：{missing}。{service}",
        "tts_profile_failed": "角色情绪语音配置未就绪：{message}",
        "tts_profile_incomplete_engine": "GPT-SoVITS 通用基座资源不完整。",
        "tts_profile_external": "9881 端口已有未经确认的服务，请先停止它再启动此配置。",
        "external_character_note": "Live2D 模型、衣装、大小和位置请通过 Live2DViewerEX 控制。",
        "tts_timeout": "TTS 超时",
        "tts_storage_root": "TTS 资源存储位置",
        "tts_storage_preview": (
            "自动目录：\n"
            "引擎：{engine}\n"
            "语音模型：{model}"
        ),
        "tts_advanced_paths": "高级设置",
        "tts_engine_root": "GPT-SoVITS 精确安装目录",
        "tts_bootstrap": "一键安装 macOS GPT-SoVITS 基础环境",
        "tts_model_dir": "角色语音模型精确目录（含参考音频）",
        "tts_autodl_ssh_command": "AutoDL SSH 登录命令",
        "tts_autodl_password": "AutoDL SSH 密码",
        "tts_autodl_remote_command": "远程 TTS 启动命令",
        "tts_autodl_reference_root": "服务器参考音频目录",
        "tts_autodl_password_placeholder": (
            "留空则继续使用操作系统安全保存的密码"
        ),
        "browse": "浏览…",
        "tts_disabled": "启用 TTS 后，将检查引擎、角色模型、参考音频和服务。",
        "tts_invalid": "请先填写有效的 TTS 地址。",
        "tts_autodl_missing": (
            "请填写 AutoDL SSH 登录命令、密码、远程启动命令和服务器参考音频目录。"
        ),
        "tts_password_store_failed": (
            "操作系统无法安全保存 AutoDL 密码：{message}"
        ),
        "tts_checking": "正在检查 GPT-SoVITS 组件……",
        "tts_ready_online": "角色语音模型完整，TTS 服务在线。",
        "tts_ready_offline": "角色语音模型完整，但 TTS 服务尚未运行。",
        "tts_service_start": "启动 TTS 服务",
        "tts_service_stop": "停止 TTS 服务",
        "tts_service_online": "TTS 服务在线",
        "tts_service_locating": "正在定位 GPT-SoVITS 运行环境……",
        "tts_service_starting": "正在启动 GPT-SoVITS 进程……",
        "tts_service_connecting_ssh": "正在通过 SSH 连接 AutoDL……",
        "tts_service_starting_remote": "正在执行 AutoDL TTS 启动命令……",
        "tts_service_starting_tunnel": "正在建立本机 SSH 隧道……",
        "tts_service_waiting": "正在等待 TTS API 就绪……",
        "tts_service_waiting_existing": "已有请求正在启动 TTS 服务，正在等待……",
        "tts_service_loading_weights": "正在加载角色语音权重……",
        "tts_service_stopping": "正在停止 TTS 服务……",
        "tts_service_started": "TTS 服务已就绪。",
        "tts_service_stopped": "TTS 服务已停止。",
        "tts_service_failed": "TTS 服务操作失败：{message}",
        "tts_model_missing": "本地缺少丛雨角色语音模型。",
        "tts_engine_missing": "角色语音模型完整，但没有找到 GPT-SoVITS 目录。",
        "tts_engine_incomplete": (
            "角色语音模型完整，但 GPT-SoVITS 基础资源不完整。"
        ),
        "tts_bootstrap_consent_title": "安装 macOS GPT-SoVITS",
        "tts_bootstrap_consent_body": (
            "是否下载固定版本的 GPT-SoVITS 源码、隔离的 Python 运行环境、"
            "Python 依赖和 Apple Silicon 所需的基础资源？上游基础资源约 5 GB。"
            "角色语音权重和"
            "参考音频不包含在此步骤中，仍按需单独下载。"
        ),
        "tts_bootstrap_installing": (
            "正在安装 macOS GPT-SoVITS 基础环境：{detail}"
        ),
        "tts_bootstrap_installed": (
            "macOS GPT-SoVITS 基础环境已就绪：{path}"
        ),
        "tts_bootstrap_failed": (
            "macOS GPT-SoVITS 安装失败：{message}"
        ),
        "tts_references_missing": "一项或多项参考语音文件缺失。",
        "tts_external_online": "外部 TTS 接口可以连接。",
        "tts_external_offline": "外部 TTS 接口当前无法连接。",
        "tts_download_consent_title": "下载丛雨语音模型",
        "tts_download_consent_body": (
            "是否从 ModelScope 下载 GPT、SoVITS 角色权重及六组参考音频"
            "（约 231 MB）？这些资源仅用于非商业和学习用途，"
            "并涉及角色专有素材。"
        ),
        "tts_download_consent_body_with_engine": (
            "未检测到 GPT-SoVITS。是否根据 NVIDIA 显卡型号自动选择并下载"
            "完整引擎，同时下载角色权重和参考音频？下载量约 8～9 GB，"
            "解压时还需要额外可用空间。这些资源仅用于非商业和学习用途。"
        ),
        "tts_download": "下载角色语音模型",
        "tts_preparing": "正在准备 TTS 下载：{detail}",
        "tts_downloading": "正在下载丛雨语音模型：{detail}",
        "tts_checking_files": "正在校验已下载文件：{detail}",
        "tts_extracting": "正在解压 GPT-SoVITS：{detail}",
        "tts_installing": "正在安装 GPT-SoVITS：{detail}",
        "tts_cleaning": "正在清理临时文件：{detail}",
        "tts_downloaded": "角色语音模型下载完成：{path}",
        "tts_download_failed": "角色语音模型下载失败：{message}",
        "download_files": "个文件",
        "download_steps": "个步骤",
        "stt_enabled": "长按 {shortcut} 进行语音输入",
        "whisper_model": "Whisper 模型或仓库 ID",
        "whisper_model_dir": "Whisper 模型下载目录",
        "stt_language": "识别语言",
        "stt_language_ui": "跟随界面语言",
        "stt_language_auto": "自动检测",
        "stt_language_help": (
            "可选择常用语言，也可输入两到三位 Whisper 语言代码，"
            "例如瑞典语 sv。名称以 .en 结尾的纯英文模型无法识别其他语言。"
        ),
        "stt_input_device": "录音设备",
        "stt_input_default": "系统默认（{device}）",
        "stt_input_default_unknown": "系统默认输入设备",
        "stt_input_unavailable": "当前不可用：{device}",
        "stt_device": "语音识别设备",
        "stt_device_auto": "自动",
        "stt_device_cuda": (
            "CUDA（请使用 AIpet-with-cuda 版本，否则无法使用）"
        ),
        "stt_device_cpu": "CPU",
        "whisper_download": "下载模型",
        "whisper_disabled": "启用语音输入后，将检查填写的模型下载目录。",
        "whisper_checking": "正在检查填写的模型目录……",
        "whisper_installed": "本地已安装：{path}",
        "whisper_missing": (
            "填写的下载目录中没有可用的所选模型。"
            "请选择目录，然后下载模型。"
        ),
        "whisper_path_missing": "填写的本地模型目录不存在或模型文件不完整。",
        "whisper_downloading": "正在下载 {model}：{detail}",
        "whisper_preparing": "正在准备 {model}：{detail}",
        "whisper_verifying": "正在校验 {model}：{detail}",
        "whisper_downloaded": "下载完成，本地路径：{path}",
        "whisper_download_failed": "下载失败：{message}",
        "download_path_required_title": "需要填写下载目录",
        "tts_storage_path_required": "请先选择 TTS 资源存储位置。",
        "tts_model_path_required": "请先选择角色语音模型下载目录。",
        "tts_engine_path_required": "请先选择 GPT-SoVITS 下载及安装目录。",
        "whisper_path_required": "请先选择 Whisper 模型下载目录。",
        "download_path_invalid": "无法使用所选目录：{message}",
        "tts_download_path_unsafe": "所选 TTS 目录不安全：{message}",
        "automation_group": "空闲行为与记忆",
        "response_target": "普通对话期望响应时间（秒）",
        "important_response_target": "重要对话期望响应时间（秒）",
        "response_unlimited": "不限",
        "response_custom": "自定义…",
        "response_custom_placeholder": "输入 0–600 秒，0 表示不限",
        "response_target_help": (
            "这是包含回复生成与语音准备的软目标，不保证到点完成。"
            "提前准备好就立即回复；超过目标会提示仍在处理，继续等待或由你取消。"
            "它不会改变 API 或语音服务的超时限制。"
            "可选择预设或输入 0–600 秒，0 表示不限。"
        ),
        "do_not_disturb_help": (
            "暂停回复、语音、屏幕观察和新记忆更新；保留静默人物动画及恢复入口。"
            "未完成回复会作废，解除后不补播。"
        ),
        "settings_help": "解释这些设置",
        "automation_help_title": "空闲行为与记忆说明",
        "automation_help_body": (
            "思考提醒：持续无操作达到该时间后，角色可能会温和地询问你是否还在。"
            "每段空闲期间最多触发一次。\n\n"
            "离开提醒：持续无操作达到更长的时间后，角色会认为你暂时离开；"
            "检测到你回来后，可能会欢迎你。该时间必须大于思考提醒。\n\n"
            "对话记忆：近期上下文的消息数量上限。宁宁还会在本地保存已完成的"
            "真实对话，按话题检索；调小此数不会删除它们。清除历史对话还会清除相关的"
            "连续心情、约定和关系状态。\n\n"
            "响应时间：包含回复生成与语音准备的软目标，不保证到点完成。"
            "提前完成就立即回复；超时会提示仍在处理，并继续等待。\n\n"
            "勿扰模式：暂停回复、语音、屏幕观察和新记忆更新，保留静默人物动画。"
            "未完成回复会作废，解除后不补播。"
        ),
        "do_not_disturb": "勿扰模式",
        "clear_history": "清除历史对话…",
        "clear_history_confirm_title": "确定清除历史对话？",
        "clear_history_confirm_body": (
            "这会立即清除全部历史对话、记住的称呼和约定、屏幕事件记忆，以及连续心情和关系状态。"
            "即使之后取消设置，也无法撤销该操作。"
        ),
        "history_cleared": "历史对话已清除。",
        "history_unavailable_first_run": "首次设置时没有可清除的历史对话。",
        "data_group": "数据管理",
        "conversation_history_data": "历史对话",
        "runtime_cache_data": "临时截图、合成语音与录音",
        "clear_cache": "清除缓存…",
        "clear_cache_confirm_title": "确定清除缓存文件？",
        "clear_cache_confirm_body": (
            "这会删除临时截图、合成语音和录音。"
            "设置、历史对话、模型与日志不会受到影响。"
        ),
        "cache_cleared": "已清除 {files} 个缓存文件，释放 {size}。",
        "cache_empty": "缓存已经是空的。",
        "cache_clear_partial": (
            "已清除 {files} 个缓存文件（{size}），但仍有 {failed} 个"
            "正在使用或无法删除的文件或文件夹。"
        ),
        "display_group": "屏幕与立绘",
        "display_help_title": "屏幕与立绘说明",
        "display_help_body": (
            "屏幕编号：当前屏幕列表中从 0 开始的序号。"
            "序号不可用时会回退到主屏幕。\n\n"
            "立绘高度比例：角色立绘相对于所选屏幕可用高度的目标比例。\n\n"
            "实时日志命令行：打开实时日志窗口，用于排查模型、TTS、录音及"
            "其他运行问题。"
        ),
        "screen_index": "屏幕编号",
        "portrait_ratio": "立绘高度比例",
        "show_log_console": "打开实时日志命令行",
        "thinking_reminder": "思考提醒",
        "away_reminder": "离开提醒",
        "conversation_memory": "对话记忆",
        "password_placeholder": "使用环境变量时留空",
        "seconds": " 秒",
        "minutes": " 分钟",
        "messages": " 条消息",
        "save": "保存",
        "cancel": "取消",
        "connecting": "正在连接模型列表接口……",
        "models_empty": (
            "连接成功，但服务没有返回模型。仍然可以手动输入模型 ID。"
        ),
        "models_found": "连接成功，已加载 {count} 个模型；仍可手动输入。",
        "connection_failed": (
            "模型列表请求失败：{message}\n"
            "当前模型 ID 已保留，可以继续手动编辑。"
        ),
        "import_title": "导入人格提示词",
        "text_filter": "文本文件 (*.txt *.md);;所有文件 (*)",
        "import_failed": "导入失败",
        "invalid_settings": "设置无效",
        "test_running_title": "连接测试正在运行",
        "test_running_body": "请等待当前连接测试结束。",
        "missing_personality": "缺少人格提示词",
        "missing_personality_body": "请填写人格提示词。",
        "missing_key": "缺少 API Key",
        "missing_key_body": "请输入 API Key，或设置对应的环境变量。",
        "save_failed": "保存失败",
        "download_preparing": "正在准备下载……",
    },
}


logger = get_logger("settings")


def load_emotional_vits_profile(path: str):
    # Keep the optional backend out of the legacy settings import path.
    from aipet.core.emotional_vits_profile import load_emotional_vits_profile as load

    return load(path)


def get_emotional_vits_service_manager():
    from aipet.core.emotional_vits_service import get_emotional_vits_service_manager as get

    return get()


@dataclass(frozen=True)
class EmotionalVITSCheckState:
    profile: object
    service_signature: str | None


AUDITION_TEXTS = {
    "zh-CN": {
        "title": "宁宁情绪试听与选择（实验）",
        "intro": "同一句台词，不同语气参考。请先试听，再为六种情绪分别选择参考；所有选项均为实验候选。",
        "overlap": "请先等桌宠当前对白结束再试听，避免声音重叠；此窗口不会暂停宠物对话。",
        "checking": "正在后台校验本地样音和向量，不加载模型或启动服务……",
        "ready": "本地样音已校验。点击播放试听；播放请求不代表听感或情绪已获批准。",
        "play": "播放所选样音", "stop": "停止", "close": "取消",
        "save": "另存实验配置并填回设置…",
        "unconfigured": "待选择",
        "candidate": "候选／待听", "author": "作者参考",
        "source": "对白参考 {number}", "unknown": "来源提示未确认", "details": "来源说明",
        "legacy": "注意：旧录音的回放窗口完整性未记录，未知状态已保留。",
        "bank": "作者提供的比较预设，尚未确认对应哪一种情绪。",
        "selected": "已选择 {count}/6；未选择的情绪暂无语音。另存后保持实验状态，不代表六情绪验收。",
        "playing": "已请求播放：{name}。请确认实际听到声音；本窗口不会自动标记为已批准。",
        "ended": "播放请求已结束；请按听感选择，情绪状态仍为实验。",
        "stopped": "试听已停止。",
        "failed": "操作失败：{error}", "exporting": "正在另存独立实验配置……",
        "directory": "选择新实验配置目录的保存位置（不会覆盖现有配置）",
    },
    "en": {
        "title": "Nene emotion auditions and selection (experimental)",
        "intro": "The same line, with different speaking-style references. Listen first, then choose a reference for each emotion. All choices remain experimental candidates.",
        "overlap": "Wait for the pet's current speech to finish before auditioning to avoid overlapping audio. This window does not pause the pet.",
        "checking": "Validating local WAVs and vectors in the background; no model or service is started…",
        "ready": "Local previews verified. Playback requests do not establish listening or emotion approval.",
        "play": "Play selected preview", "stop": "Stop", "close": "Cancel",
        "save": "Save new experimental profile and fill settings…",
        "unconfigured": "Choose a reference",
        "candidate": "candidate / awaiting listening", "author": "Author reference",
        "source": "Dialogue reference {number}", "unknown": "unconfirmed source hint", "details": "Source details",
        "legacy": "Warning: the legacy recording's replay-window check was not recorded; uncertainty is preserved.",
        "bank": "The author's comparison preset has no confirmed emotion assignment.",
        "selected": "Selected {count}/6. Unselected emotions have no voice reference. Saving remains experimental, not six-emotion acceptance.",
        "playing": "Playback requested: {name}. Confirm that you actually hear it; approval is not marked automatically.",
        "ended": "Playback request ended. Choose by listening; emotion status remains experimental.",
        "stopped": "Preview stopped.", "failed": "Operation failed: {error}",
        "exporting": "Saving an independent experimental profile…",
        "directory": "Choose the parent directory for a new experimental profile (existing files stay intact)",
    },
}


class EmotionalVITSAuditionWorker(QThread):
    loaded = pyqtSignal(object)
    exported = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, *, catalog=None, selections=None, destination=None, parent=None):
        super().__init__(parent)
        self.catalog = catalog
        self.selections = dict(selections or {})
        self.destination = destination
        self._cancelled = Event()

    def cancel(self) -> None:
        self._cancelled.set()

    def run(self) -> None:
        from aipet.core.emotional_vits_audition import (
            AuditionCancelled, export_audition_profile, load_audition_catalog,
        )
        try:
            if self._cancelled.is_set():
                return
            if self.destination is None:
                catalog = load_audition_catalog(cancelled=self._cancelled.is_set)
                if not self._cancelled.is_set():
                    self.loaded.emit(catalog)
            else:
                path = export_audition_profile(self.catalog, self.selections, self.destination,
                                               cancelled=self._cancelled.is_set)
                if not self._cancelled.is_set():
                    self.exported.emit(str(path))
        except AuditionCancelled:
            return
        except Exception as exc:
            if not self._cancelled.is_set():
                self.failed.emit(str(exc))


class EmotionalVITSAuditionDialog(QDialog):
    """Audition existing local WAVs and explicitly save separate experiment routes."""
    profile_saved = pyqtSignal(str)

    def __init__(self, *, language="zh-CN", parent=None):
        super().__init__(parent)
        self._language = "en" if language == "en" else "zh-CN"
        self._catalog = None
        self._worker: EmotionalVITSAuditionWorker | None = None
        self._pending_reject = False
        self._exported_path: str | None = None
        self._sound: QSound | None = None
        self.setWindowTitle(self._text("title"))
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        self.resize(880, 660)
        layout = QVBoxLayout(self)
        for key in ("intro", "overlap"):
            label = QLabel(self._text(key))
            label.setWordWrap(True)
            layout.addWidget(label)
        self.comparison_text = QLabel()
        self.comparison_text.setWordWrap(True)
        self.comparison_text.setTextFormat(Qt.PlainText)
        layout.addWidget(self.comparison_text)
        columns = QHBoxLayout()
        previews = QVBoxLayout()
        self.candidates = QListWidget()
        self.candidates.currentRowChanged.connect(self._on_candidate_changed)
        previews.addWidget(self.candidates)
        buttons = QHBoxLayout()
        self.play_button = QPushButton(self._text("play"))
        self.play_button.clicked.connect(self._play_preview)
        self.stop_button = QPushButton(self._text("stop"))
        self.stop_button.clicked.connect(self._stop_preview)
        buttons.addWidget(self.play_button)
        buttons.addWidget(self.stop_button)
        previews.addLayout(buttons)
        columns.addLayout(previews, 3)
        mappings = QFormLayout()
        self.emotion_choices: dict[str, QComboBox] = {}
        english_labels = {"平静": "Calm", "高兴": "Happy", "害羞": "Shy", "生气": "Angry",
                          "惊讶": "Surprised", "着急": "Worried / anxious"}
        for emotion in ("平静", "高兴", "害羞", "生气", "惊讶", "着急"):
            combo = QComboBox()
            combo.addItem(self._text("unconfigured"), None)
            combo.currentIndexChanged.connect(self._update_selection_state)
            self.emotion_choices[emotion] = combo
            mappings.addRow(english_labels[emotion] if self._language == "en" else emotion, combo)
        columns.addLayout(mappings, 3)
        layout.addLayout(columns, 1)
        self.details = QLabel()
        self.details.setTextFormat(Qt.PlainText)
        self.details.setWordWrap(True)
        self.details.hide()
        self.details_toggle = QToolButton()
        self.details_toggle.setText(self._text("details"))
        self.details_toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.details_toggle.setCheckable(True)
        self.details_toggle.setArrowType(Qt.RightArrow)
        self.details_toggle.toggled.connect(self.details.setVisible)
        self.details_toggle.toggled.connect(lambda checked: self.details_toggle.setArrowType(
            Qt.DownArrow if checked else Qt.RightArrow))
        layout.addWidget(self.details_toggle)
        layout.addWidget(self.details)
        self.selection_status = QLabel()
        self.selection_status.setWordWrap(True)
        layout.addWidget(self.selection_status)
        self.status = QLabel(self._text("checking"))
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        footer = QHBoxLayout()
        self.save_button = QPushButton(self._text("save"))
        self.save_button.clicked.connect(self._choose_destination)
        self.cancel_button = QPushButton(self._text("close"))
        self.cancel_button.clicked.connect(self.reject)
        footer.addWidget(self.save_button)
        footer.addStretch(1)
        footer.addWidget(self.cancel_button)
        layout.addLayout(footer)
        for button in (self.play_button, self.stop_button, self.save_button, self.cancel_button):
            button.setAutoDefault(False)
        self._preview_timer = QTimer(self)
        self._preview_timer.setInterval(100)
        self._preview_timer.timeout.connect(self._poll_preview)
        self._update_selection_state()
        self._start_worker(EmotionalVITSAuditionWorker(parent=self))

    def _text(self, key: str, **values) -> str:
        return AUDITION_TEXTS[self._language][key].format(**values)

    def is_busy(self) -> bool:
        return self._worker is not None and self._worker.isRunning()

    def _start_worker(self, worker: EmotionalVITSAuditionWorker) -> None:
        self._worker = worker
        worker.loaded.connect(self._on_catalog_loaded)
        worker.exported.connect(self._on_exported)
        worker.failed.connect(self._on_failed)
        worker.finished.connect(lambda: self._finish_worker(worker))
        self._update_selection_state()
        worker.start()

    def _on_catalog_loaded(self, catalog) -> None:
        if self._pending_reject:
            return
        self._catalog = catalog
        self.comparison_text.setText(catalog.text)
        self.candidates.clear()
        for candidate in catalog.candidates:
            self.candidates.addItem(f"{self._candidate_name(candidate)} · {candidate.duration_seconds:.2f}s · {self._text('candidate')}")
        for combo in self.emotion_choices.values():
            combo.blockSignals(True)
            combo.clear()
            combo.addItem(self._text("unconfigured"), None)
            for candidate in catalog.candidates:
                combo.addItem(self._candidate_name(candidate), candidate.identifier)
            combo.setCurrentIndex(0)
            combo.blockSignals(False)
        self.candidates.setCurrentRow(0)
        self.status.setText(self._text("ready"))
        self._update_selection_state()

    def _candidate_name(self, candidate) -> str:
        if candidate.bank_id is not None:
            return self._text("author")
        number = next(index for index, item in enumerate(self._catalog.candidates)
                      if item.identifier == candidate.identifier)
        return self._text("source", number=number)

    def _on_exported(self, path: str) -> None:
        if not self._pending_reject:
            self._exported_path = path

    def _on_failed(self, message: str) -> None:
        if not self._pending_reject:
            self.status.setText(self._text("failed", error=message))

    def _finish_worker(self, worker) -> None:
        if self._worker is not worker:
            return
        self._worker = None
        worker.deleteLater()
        if self._pending_reject:
            super().reject()
        elif self._exported_path is not None:
            self._stop_preview()
            self.profile_saved.emit(self._exported_path)
            super().accept()
        else:
            self._update_selection_state()

    def selections(self) -> dict[str, str]:
        return {label: combo.currentData() for label, combo in self.emotion_choices.items()
                if combo.currentData() is not None}

    def _update_selection_state(self) -> None:
        idle = self._worker is None and not self._pending_reject
        ready = self._catalog is not None and idle
        count = len(self.selections())
        self.selection_status.setText(self._text("selected", count=count))
        self.candidates.setEnabled(ready)
        for combo in self.emotion_choices.values():
            combo.setEnabled(ready)
        self.play_button.setEnabled(ready and self.candidates.currentRow() >= 0)
        self.stop_button.setEnabled(self._sound is not None)
        self.save_button.setEnabled(ready and count > 0)

    def _on_candidate_changed(self, row: int) -> None:
        self._stop_preview()
        if self._catalog is None or not 0 <= row < len(self._catalog.candidates):
            return
        candidate = self._catalog.candidates[row]
        if candidate.bank_id is not None:
            text = self._text("bank")
        else:
            hint = candidate.source_audit.get("source_emotion_candidate_unverified", "—")
            text = self._text("unknown") + ": " + str(hint)
            if candidate.source_audit.get("replay_window_truncated_possible") is None:
                text += "\n" + self._text("legacy")
        self.details.setText(candidate.identifier + "\n" + text)
        self._update_selection_state()

    def _play_preview(self) -> None:
        if self._catalog is None or self._worker is not None or self._pending_reject:
            return
        index = self.candidates.currentRow()
        if not 0 <= index < len(self._catalog.candidates):
            return
        self._stop_preview()
        candidate = self._catalog.candidates[index]
        self._sound = QSound(str(candidate.audio), self)
        self._sound.play()
        self.status.setText(self._text("playing", name=self._candidate_name(candidate)))
        self._preview_timer.start()
        self._update_selection_state()

    def _poll_preview(self) -> None:
        if self._sound is not None and self._sound.isFinished():
            self._stop_preview()
            self.status.setText(self._text("ended"))

    def _stop_preview(self) -> None:
        self._preview_timer.stop()
        if self._sound is not None:
            self._sound.stop()
            self._sound.deleteLater()
            self._sound = None
            self.status.setText(self._text("stopped"))
        self.stop_button.setEnabled(False)

    def _choose_destination(self) -> None:
        if self._worker is not None or not self.selections():
            return
        path = QFileDialog.getExistingDirectory(self, self._text("directory"),
                                               str(Path(__file__).resolve().parents[2] / "voices"))
        if path:
            self._export_profile(path)

    def _export_profile(self, destination) -> None:
        if self._catalog is None or self._worker is not None or not self.selections():
            return
        self._stop_preview()
        self.status.setText(self._text("exporting"))
        self._start_worker(EmotionalVITSAuditionWorker(catalog=self._catalog, selections=self.selections(),
                                                      destination=destination, parent=self))

    def reject(self) -> None:
        self._stop_preview()
        if self._worker is not None:
            self._pending_reject = True
            self._worker.cancel()
            self.setEnabled(False)
            self.hide()
            return  # Keep the QThread owner alive until its finished signal.
        super().reject()

    def accept(self) -> None:
        self._stop_preview()
        if self._worker is None:
            super().accept()

    def hideEvent(self, event) -> None:
        self._stop_preview()
        super().hideEvent(event)

    def closeEvent(self, event) -> None:
        self._stop_preview()
        if self._worker is not None:
            self.reject()
            event.ignore()
            return
        super().closeEvent(event)


class ModelListWorker(QThread):
    models_ready = pyqtSignal(list)
    error = pyqtSignal(str)

    def __init__(
        self,
        settings: AppSettings,
        *,
        vision: bool = False,
        parent=None,
    ):
        super().__init__(parent)
        self.settings = settings
        self.vision = vision

    def run(self) -> None:
        try:
            backend = (
                create_vision_backend(self.settings)
                if self.vision
                else create_backend(self.settings)
            )
            self.models_ready.emit(
                backend.list_models(vision=self.vision)
            )
        except Exception as exc:
            logger.exception(
                "%s模型列表加载失败",
                "视觉" if self.vision else "语言",
            )
            self.error.emit(str(exc))


class WhisperModelCheckWorker(QThread):
    checked = pyqtSignal(str, str, str)

    def __init__(
        self,
        model_name: str,
        model_directory: str,
        parent=None,
    ):
        super().__init__(parent)
        self.model_name = model_name
        self.model_directory = model_directory

    def run(self) -> None:
        path = find_local_model(
            self.model_name,
            self.model_directory,
        ) or ""
        self.checked.emit(self.model_name, self.model_directory, path)


class TTSCheckWorker(QThread):
    checked = pyqtSignal(object, bool)
    failed = pyqtSignal(str)

    def __init__(self, settings: TTSSettings, parent=None):
        super().__init__(parent)
        self.settings = settings.model_copy(deep=True)

    def run(self) -> None:
        try:
            if self.settings.backend == "emotional_vits":
                profile = load_emotional_vits_profile(self.settings.emotional_vits_profile)
                manager = get_emotional_vits_service_manager()
                endpoint = manager.current_endpoint()
                self.checked.emit(
                    EmotionalVITSCheckState(profile, endpoint.signature if endpoint else None),
                    manager.owns_running_process(),
                )
                return
            state = locate_tts_assets(
                configured_engine_root=self.settings.engine_root,
                configured_model_dir=self.settings.model_dir,
                configured_voice_profile=self.settings.voice_profile,
            )
            reachable = tts_service_is_reachable(tts_base_url(self.settings))
            self.checked.emit(state, reachable)
        except Exception as exc:
            self.failed.emit(str(exc))


class TTSServiceWorker(QThread):
    stage_changed = pyqtSignal(str)
    succeeded = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(
        self,
        settings: TTSSettings,
        action: str,
        password: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self.settings = settings.model_copy(deep=True)
        self.action = action
        self.password = password
        self._cancelled = Event()

    def cancel(self) -> None:
        self._cancelled.set()

    def run(self) -> None:
        try:
            if self._cancelled.is_set():
                return
            if self.settings.backend == "emotional_vits":
                manager = get_emotional_vits_service_manager()
                if self.action == "stop":
                    manager.stop()
                    self.succeeded.emit("stopped")
                else:
                    self.stage_changed.emit("starting_emotional_vits")
                    manager.ensure_running(self.settings, cancelled=self._cancelled.is_set)
                    if not self._cancelled.is_set():
                        self.succeeded.emit("started")
                return
            manager = get_tts_service_manager()
            if self.action == "stop":
                manager.stop()
                self.succeeded.emit("stopped")
                return

            state = locate_tts_assets(
                configured_engine_root=self.settings.engine_root,
                configured_model_dir=self.settings.model_dir,
                configured_voice_profile=self.settings.voice_profile,
            )
            if not self.settings.uses_autodl() and not state.model_ready:
                raise TTSServiceError(
                    "The Murasame voice weights are incomplete."
                )
            if (
                not self.settings.uses_autodl()
                and not self.settings.voice_profile
                and not state.reference_voices_ready
            ):
                raise TTSServiceError(
                    "The Murasame reference voices are incomplete."
                )
            manager.ensure_running(
                self.settings,
                state=state,
                progress=self.stage_changed.emit,
                password=self.password,
            )
            if not self.settings.uses_autodl() and not self.settings.voice_profile:
                self.stage_changed.emit("loading_weights")
                configure_local_tts_weights(
                    self.settings.base_url,
                    state,
                    self.settings.timeout_seconds,
                )
            self.succeeded.emit("started")
        except Exception as exc:
            self.failed.emit(str(exc))


class TTSBootstrapWorker(QThread):
    stage_changed = pyqtSignal(str)
    succeeded = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(
        self,
        platform_runtime: PlatformRuntime,
        engine_root: Path,
        parent=None,
    ):
        super().__init__(parent)
        self.platform_runtime = platform_runtime
        self.engine_root = engine_root

    def run(self) -> None:
        try:
            self.platform_runtime.tts_bootstrap.install(
                self.engine_root,
                self.stage_changed.emit,
            )
            self.succeeded.emit(str(self.engine_root))
        except Exception as exc:
            self.failed.emit(str(exc))


class SettingsDialog(QDialog):
    """Visual bilingual configuration and personality creation window."""

    clear_history_requested = pyqtSignal()

    def __init__(
        self,
        settings: AppSettings,
        *,
        first_run: bool = False,
        download_manager: DownloadManager | None = None,
        parent=None,
        platform_runtime: PlatformRuntime | None = None,
    ):
        super().__init__(parent)
        self._platform_runtime = (
            platform_runtime or get_platform_runtime()
        )
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        self._original = settings.model_copy(deep=True)
        self._legacy_tts_base_url = settings.tts.base_url
        self._voice_profile_controls_active = False
        self._first_run = first_run
        self._model_worker: ModelListWorker | None = None
        self._model_target: tuple[str, str, str] | None = None
        self._model_fetch_queue: list[bool] = []
        self._auto_models_requested = False
        self._whisper_check_worker: WhisperModelCheckWorker | None = None
        self._pending_whisper_model: tuple[str, str] | None = None
        self._tts_check_worker: TTSCheckWorker | None = None
        self._tts_service_worker: TTSServiceWorker | None = None
        self._tts_bootstrap_worker: TTSBootstrapWorker | None = None
        self._tts_bootstrap_error: str | None = None
        self._tts_service_error: str | None = None
        self._tts_service_button_mode = "start"
        self._emotion_audition_dialog: EmotionalVITSAuditionDialog | None = None
        self._pending_tts_check = False
        self._tts_download_prompted = False
        self._tts_engine_download_needed = False
        self._closing = False
        self._result: AppSettings | None = None
        self._personality_prompts: dict[str, str] = {}
        self._editing_language = settings.ui_language
        self.download_manager = download_manager or DownloadManager(
            QApplication.instance(),
            platform_runtime=self._platform_runtime,
        )
        self._form_labels: dict[str, list[QLabel]] = {}
        self._form_fields: dict[str, list[QWidget]] = {}
        self._audio_device_signature: tuple[object, ...] | None = None
        self._status_key: str | None = None
        self._status_values: dict[str, object] = {}
        self._whisper_status_key = "whisper_disabled"
        self._whisper_status_values: dict[str, object] = {}
        self._tts_status_key = "tts_disabled"
        self._tts_status_values: dict[str, object] = {}

        self.setMinimumSize(700, 650)
        self.resize(760, 720)
        root = QVBoxLayout(self)

        language_form = QFormLayout()
        self.language_label = QLabel()
        self.language_combo = QComboBox()
        self.language_combo.addItem("English", "en")
        self.language_combo.addItem("简体中文", "zh-CN")
        language_form.addRow(self.language_label, self.language_combo)
        root.addLayout(language_form)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_models_tab(), "")
        self.tabs.addTab(self._build_extensions_tab(), "")
        self.tabs.addTab(self._build_character_tab(), "")
        self.tabs.addTab(self._build_automation_tab(), "")
        self.tabs.addTab(self._build_display_tab(), "")
        self.tabs.addTab(self._build_other_tab(), "")
        root.addWidget(self.tabs, 1)

        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        root.addWidget(self.buttons)

        self._audio_device_refresh_timer = QTimer(self)
        self._audio_device_refresh_timer.setInterval(2_000)
        self._audio_device_refresh_timer.timeout.connect(
            self._refresh_audio_input_devices
        )
        self._load_values(settings)
        self.language_combo.currentIndexChanged.connect(
            self._on_language_changed
        )
        self.stt_enabled.toggled.connect(self._update_whisper_state)
        self.stt_model.currentTextChanged.connect(self._update_whisper_state)
        self.whisper_model_dir.editingFinished.connect(
            self._update_whisper_state
        )
        self.vision_enabled.toggled.connect(self._update_vision_state)
        self.tts_enabled.toggled.connect(self._update_tts_state)
        self.tts_backend.currentIndexChanged.connect(
            self._on_tts_backend_changed
        )
        self.tts_url.editingFinished.connect(self._update_tts_state)
        self.tts_voice_profile.editingFinished.connect(self._update_tts_state)
        self.tts_emotional_vits_profile.editingFinished.connect(self._update_tts_state)
        self.tts_storage_root.editingFinished.connect(
            self._on_tts_storage_root_changed
        )
        self.tts_advanced_paths.toggled.connect(
            self._on_tts_advanced_paths_changed
        )
        self.tts_engine_root.editingFinished.connect(self._update_tts_state)
        self.tts_model_dir.editingFinished.connect(self._update_tts_state)
        self.tts_autodl_ssh_command.editingFinished.connect(
            self._update_tts_state
        )
        self.tts_autodl_password.editingFinished.connect(
            self._update_tts_state
        )
        self.tts_autodl_remote_command.editingFinished.connect(
            self._update_tts_state
        )
        self.tts_autodl_reference_root.editingFinished.connect(
            self._update_tts_state
        )
        self.download_manager.changed.connect(self._on_download_changed)
        self._update_backend_visibility()
        self._retranslate_ui()
        self._update_vision_state(self.vision_enabled.isChecked())
        self._update_whisper_state()
        self._update_tts_state()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._refresh_audio_input_devices()
        self._audio_device_refresh_timer.start()
        if self._auto_models_requested:
            return
        self._auto_models_requested = True
        QTimer.singleShot(0, self._auto_fetch_models)

    def hideEvent(self, event) -> None:
        self._audio_device_refresh_timer.stop()
        if self._emotion_audition_dialog is not None:
            self._emotion_audition_dialog.reject()
        super().hideEvent(event)

    def _add_row(
        self,
        form: QFormLayout,
        key: str,
        field: QWidget,
    ) -> None:
        label = QLabel()
        label.setWordWrap(True)
        form.addRow(label, field)
        self._form_labels.setdefault(key, []).append(label)
        self._form_fields.setdefault(key, []).append(field)

    def _set_form_labels_enabled(
        self,
        keys: tuple[str, ...],
        enabled: bool,
    ) -> None:
        for key in keys:
            for label in self._form_labels.get(key, ()):
                label.setEnabled(enabled)

    def _set_form_rows_visible(
        self,
        keys: tuple[str, ...],
        visible: bool,
    ) -> None:
        for key in keys:
            for label in self._form_labels.get(key, ()):
                label.setVisible(visible)
            for field in self._form_fields.get(key, ()):
                field.setVisible(visible)

    def _help_button(self, title_key: str, body_key: str) -> QToolButton:
        button = QToolButton()
        button.setText("?")
        button.setFixedSize(24, 24)
        button.clicked.connect(
            lambda: QMessageBox.information(
                self,
                self._text(title_key),
                self._text(body_key),
            )
        )
        return button

    @staticmethod
    def _right_aligned_widget(widget: QWidget) -> QWidget:
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addStretch(1)
        layout.addWidget(widget)
        return container

    def _build_models_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSizeConstraint(QLayout.SetMinimumSize)

        self.backend_group = QGroupBox()
        mode_form = QFormLayout(self.backend_group)
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("", "ollama")
        self.mode_combo.addItem("", "api")
        self.mode_combo.currentIndexChanged.connect(
            self._update_backend_visibility
        )
        self._add_row(mode_form, "mode", self.mode_combo)
        layout.addWidget(self.backend_group)

        self.backend_stack = QStackedWidget()
        self.backend_stack.addWidget(self._build_ollama_panel())
        self.backend_stack.addWidget(self._build_api_panel())
        layout.addWidget(self.backend_stack)

        test_row = QHBoxLayout()
        self.fetch_models_button = QPushButton()
        self.fetch_models_button.clicked.connect(self._fetch_models)
        test_row.addWidget(self.fetch_models_button)
        test_row.addStretch(1)
        layout.addLayout(test_row)
        self.model_list_help = QLabel()
        self.model_list_help.setWordWrap(True)
        layout.addWidget(self.model_list_help)
        layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        return scroll

    def _build_ollama_panel(self) -> QWidget:
        self.ollama_group = QGroupBox()
        form = QFormLayout(self.ollama_group)
        self.ollama_url = QLineEdit()
        self.ollama_chat_model = self._editable_combo()
        self.ollama_context_window = self._spinbox(2_048, 131_072)
        self.ollama_context_window.setSingleStep(1_024)
        self.ollama_timeout = self._spinbox(10, 600)
        self.ollama_keep_alive = QLineEdit()
        self._add_row(form, "server_url", self.ollama_url)
        self._add_row(form, "chat_model", self.ollama_chat_model)
        self._add_row(form, "context_window", self.ollama_context_window)
        self._add_row(form, "request_timeout", self.ollama_timeout)
        self._add_row(form, "keep_alive", self.ollama_keep_alive)
        return self.ollama_group

    def _build_api_panel(self) -> QWidget:
        self.api_group = QGroupBox()
        layout = QVBoxLayout(self.api_group)
        provider_form = QFormLayout()
        self.api_provider = QComboBox()
        self.api_provider.addItem("DeepSeek", "deepseek")
        self.api_provider.addItem("", "aliyun")
        self.api_provider.addItem("OpenAI", "openai")
        self.api_provider.currentIndexChanged.connect(
            self._update_api_provider
        )
        self._add_row(provider_form, "provider", self.api_provider)
        layout.addLayout(provider_form)

        self.api_provider_stack = QStackedWidget()
        self.api_provider_stack.addWidget(self._build_deepseek_panel())
        self.api_provider_stack.addWidget(self._build_aliyun_panel())
        self.api_provider_stack.addWidget(self._build_openai_panel())
        layout.addWidget(self.api_provider_stack)

        common_form = QFormLayout()
        self.api_timeout = self._spinbox(10, 600)
        self._add_row(common_form, "request_timeout", self.api_timeout)
        layout.addLayout(common_form)
        return self.api_group

    def _build_deepseek_panel(self) -> QWidget:
        panel = QWidget()
        form = QFormLayout(panel)
        self.deepseek_key = self._password_field()
        self.deepseek_url = QLineEdit()
        self.deepseek_chat_model = self._editable_combo()
        self.deepseek_chat_model.addItems(
            ["deepseek-v4-flash", "deepseek-v4-pro"]
        )
        self.deepseek_thinking = QCheckBox()
        self.deepseek_thinking_max_tokens = self._spinbox(1_024, 393_216)
        self.deepseek_thinking_max_tokens.setValue(8_192)
        self._add_row(form, "deepseek_api_key", self.deepseek_key)
        self._add_row(form, "base_url", self.deepseek_url)
        self._add_row(form, "chat_model", self.deepseek_chat_model)
        form.addRow(self.deepseek_thinking)
        self._add_row(
            form,
            "deepseek_thinking_max_tokens",
            self.deepseek_thinking_max_tokens,
        )
        self.deepseek_thinking.toggled.connect(
            self._update_deepseek_thinking_state
        )
        self._update_deepseek_thinking_state()
        self.deepseek_note = QLabel()
        self.deepseek_note.setWordWrap(True)
        form.addRow(self.deepseek_note)
        return panel

    def _build_aliyun_panel(self) -> QWidget:
        panel = QWidget()
        form = QFormLayout(panel)
        self.aliyun_key = self._password_field()
        self.aliyun_url = QLineEdit()
        self.aliyun_chat_model = self._editable_combo()
        self._add_row(form, "aliyun_api_key", self.aliyun_key)
        self._add_row(form, "base_url", self.aliyun_url)
        self._add_row(form, "chat_model", self.aliyun_chat_model)
        return panel

    def _build_openai_panel(self) -> QWidget:
        panel = QWidget()
        form = QFormLayout(panel)
        self.openai_key = self._password_field()
        self.openai_url = QLineEdit()
        self.openai_chat_model = self._editable_combo()
        self.openai_chat_model.addItems(
            ["gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"]
        )
        self._add_row(form, "openai_api_key", self.openai_key)
        self._add_row(form, "base_url", self.openai_url)
        self._add_row(form, "chat_model", self.openai_chat_model)
        return panel

    def _build_character_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        self.identity_group = QGroupBox()
        identity_form = QFormLayout(self.identity_group)
        self.user_name = QLineEdit()
        self.portrait = QComboBox()
        self.portrait.addItem("", "a")
        self.portrait.addItem("", "b")
        self.outfit = QComboBox()
        self.outfit.addItem("", "sleepwear")
        self.outfit.addItem("", "casual")
        self.outfit.addItem("", "uniform")
        self.outfit.addItem("", "kimono")
        self._add_row(identity_form, "user_name", self.user_name)
        self._add_row(identity_form, "portrait_set", self.portrait)
        self._add_row(identity_form, "outfit", self.outfit)
        self.external_character_note = QLabel()
        self.external_character_note.setWordWrap(True)
        identity_form.addRow(self.external_character_note)
        layout.addWidget(self.identity_group)

        self.prompt_group = QGroupBox()
        prompt_layout = QVBoxLayout(self.prompt_group)
        self.prompt_help = QLabel()
        self.prompt_help.setWordWrap(True)
        prompt_layout.addWidget(self.prompt_help)
        self.personality_prompt = QPlainTextEdit()
        prompt_layout.addWidget(self.personality_prompt, 1)
        self.import_button = QPushButton()
        self.import_button.clicked.connect(self._import_prompt)
        prompt_layout.addWidget(self.import_button)
        layout.addWidget(self.prompt_group, 1)
        return page

    def _build_extensions_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        self.vision_group = QGroupBox()
        vision_layout = QVBoxLayout(self.vision_group)
        self.vision_enabled = QCheckBox()
        vision_layout.addWidget(self.vision_enabled)

        self.vision_options = QWidget()
        vision_options_layout = QVBoxLayout(self.vision_options)
        vision_options_layout.setContentsMargins(0, 0, 0, 0)
        vision_provider_form = QFormLayout()
        self.vision_provider = QComboBox()
        self.vision_provider.addItem("", "ollama")
        self.vision_provider.addItem("", "aliyun")
        self.vision_provider.addItem("", "openai")
        self.vision_provider.currentIndexChanged.connect(
            self._update_vision_compatibility
        )
        self._add_row(
            vision_provider_form,
            "vision_provider",
            self.vision_provider,
        )
        vision_options_layout.addLayout(vision_provider_form)

        self.vision_provider_stack = QStackedWidget()
        self.vision_provider_stack.addWidget(
            self._build_vision_ollama_panel()
        )
        self.vision_provider_stack.addWidget(
            self._build_vision_aliyun_panel()
        )
        self.vision_provider_stack.addWidget(
            self._build_vision_openai_panel()
        )
        vision_options_layout.addWidget(self.vision_provider_stack)

        vision_model_row = QHBoxLayout()
        self.fetch_vision_models_button = QPushButton()
        self.fetch_vision_models_button.clicked.connect(
            self._fetch_vision_models
        )
        vision_model_row.addWidget(self.fetch_vision_models_button)
        vision_model_row.addStretch(1)
        vision_options_layout.addLayout(vision_model_row)
        self.vision_model_list_help = QLabel()
        self.vision_model_list_help.setWordWrap(True)
        vision_options_layout.addWidget(self.vision_model_list_help)

        vision_form = QFormLayout()
        self.vision_interval = self._spinbox(10, 86_400)
        self.vision_timeout = self._spinbox(10, 600)
        self.vision_compatibility = QLabel()
        self.vision_compatibility.setWordWrap(True)
        self._add_row(vision_form, "interval", self.vision_interval)
        self._add_row(
            vision_form,
            "request_timeout",
            self.vision_timeout,
        )
        vision_form.addRow(self.vision_compatibility)
        vision_options_layout.addLayout(vision_form)
        vision_layout.addWidget(self.vision_options)

        self.tts_group = QGroupBox()
        tts_form = QFormLayout(self.tts_group)
        self.tts_enabled = QCheckBox()
        self.tts_backend = QComboBox()
        self.tts_backend.addItem("", "local")
        self.tts_backend.addItem("", "autodl")
        self.tts_backend.addItem("", "emotional_vits")
        self.tts_url = QLineEdit()
        self.tts_voice_profile = QLineEdit()
        self.tts_voice_profile.setPlaceholderText("voice-profile.json")
        self._add_row(tts_form, "tts_voice_profile", self.tts_voice_profile)
        self.tts_emotional_vits_profile = QLineEdit()
        self.tts_emotional_vits_profile.setPlaceholderText("nene-emotional-vits.json")
        self.tts_emotional_vits_browse = QPushButton()
        self.tts_emotional_vits_browse.clicked.connect(self._browse_emotional_vits_profile)
        self._add_row(tts_form, "tts_emotional_vits_profile", self._path_picker(
            self.tts_emotional_vits_profile, self.tts_emotional_vits_browse,
        ))
        self.tts_emotional_vits_audition = QPushButton()
        self.tts_emotional_vits_audition.clicked.connect(self._open_emotion_audition)
        tts_form.addRow(self.tts_emotional_vits_audition)
        self.tts_emotional_vits_audition_result = QLabel()
        self.tts_emotional_vits_audition_result.setWordWrap(True)
        self.tts_emotional_vits_audition_result.setTextFormat(Qt.PlainText)
        self.tts_emotional_vits_audition_result.hide()
        tts_form.addRow(self.tts_emotional_vits_audition_result)
        self.tts_timeout = self._spinbox(10, 900)
        self.tts_storage_root = QLineEdit()
        self.tts_storage_browse = QPushButton()
        self.tts_storage_browse.clicked.connect(self._browse_tts_storage)
        self.tts_path_preview = QLabel()
        self.tts_path_preview.setWordWrap(True)
        self.tts_path_preview.setMinimumHeight(72)
        self.tts_advanced_paths = QCheckBox()
        self.tts_engine_root = QLineEdit()
        self.tts_engine_browse = QPushButton()
        self.tts_engine_browse.clicked.connect(self._browse_tts_engine)
        self.tts_model_dir = QLineEdit()
        self.tts_model_browse = QPushButton()
        self.tts_model_browse.clicked.connect(self._browse_tts_model)
        self.tts_autodl_ssh_command = QLineEdit()
        self.tts_autodl_password = self._password_field()
        self.tts_autodl_remote_command = QLineEdit()
        self.tts_autodl_reference_root = QLineEdit()
        self.tts_status = QLabel()
        self.tts_status.setWordWrap(True)
        self.tts_progress = QProgressBar()
        self.tts_progress.setTextVisible(True)
        self.tts_progress.hide()
        self.tts_extract_progress = QProgressBar()
        self.tts_extract_progress.setTextVisible(True)
        self.tts_extract_progress.hide()
        self.tts_download_button = QPushButton()
        self.tts_download_button.setEnabled(False)
        self.tts_download_button.clicked.connect(
            self._request_tts_download
        )
        self.tts_bootstrap_button = QPushButton()
        self.tts_bootstrap_button.clicked.connect(
            self._request_tts_bootstrap
        )
        self.tts_service_button = QPushButton()
        self.tts_service_button.setEnabled(False)
        self.tts_service_button.clicked.connect(
            self._toggle_tts_service
        )
        self.stt_enabled = QCheckBox()
        self.stt_model = self._editable_combo()
        self.stt_model.addItems(WHISPER_MODELS)
        self.whisper_model_dir = QLineEdit()
        self.whisper_model_browse = QPushButton()
        self.whisper_model_browse.clicked.connect(
            self._browse_whisper_model
        )
        self.stt_device = QComboBox()
        self.stt_device.addItem("", "auto")
        self.stt_device.addItem("", "cuda")
        self.stt_device.addItem("", "cpu")
        self.stt_language = self._editable_combo()
        self.stt_language.addItem("", "ui")
        self.stt_language.addItem("", "auto")
        for code, _english_name, _chinese_name in COMMON_WHISPER_LANGUAGES:
            self.stt_language.addItem("", code)
        self.stt_language_help = QLabel()
        self.stt_language_help.setWordWrap(True)
        self.stt_input_device = QComboBox()
        self._default_audio_input = None
        self._missing_audio_input_identifier = ""
        self.stt_input_device.addItem("", "")
        self._refresh_audio_input_devices(force=True)
        self.whisper_status = QLabel()
        self.whisper_status.setWordWrap(True)
        self.whisper_progress = QProgressBar()
        self.whisper_progress.setTextVisible(True)
        self.whisper_progress.hide()
        self.whisper_download_button = QPushButton()
        self.whisper_download_button.clicked.connect(
            self._download_whisper_model
        )
        tts_form.addRow(self.tts_enabled)
        self._add_row(tts_form, "tts_backend", self.tts_backend)
        self._add_row(tts_form, "tts_endpoint", self.tts_url)
        self._add_row(tts_form, "tts_timeout", self.tts_timeout)
        self._add_row(
            tts_form,
            "tts_storage_root",
            self._path_picker(
                self.tts_storage_root,
                self.tts_storage_browse,
            ),
        )
        tts_form.addRow(self.tts_path_preview)
        tts_form.addRow(self.tts_advanced_paths)
        self._add_row(
            tts_form,
            "tts_engine_root",
            self._path_picker(
                self.tts_engine_root,
                self.tts_engine_browse,
            ),
        )
        self._add_row(
            tts_form,
            "tts_bootstrap",
            self.tts_bootstrap_button,
        )
        self._add_row(
            tts_form,
            "tts_model_dir",
            self._path_picker(
                self.tts_model_dir,
                self.tts_model_browse,
            ),
        )
        self._add_row(
            tts_form,
            "tts_autodl_ssh_command",
            self.tts_autodl_ssh_command,
        )
        self._add_row(
            tts_form,
            "tts_autodl_password",
            self.tts_autodl_password,
        )
        self._add_row(
            tts_form,
            "tts_autodl_remote_command",
            self.tts_autodl_remote_command,
        )
        self._add_row(
            tts_form,
            "tts_autodl_reference_root",
            self.tts_autodl_reference_root,
        )
        tts_form.addRow(self.tts_status)
        tts_form.addRow(self.tts_service_button)
        tts_form.addRow(self.tts_progress)
        tts_form.addRow(self.tts_extract_progress)
        tts_form.addRow(self.tts_download_button)
        layout.addWidget(self.tts_group)

        self.whisper_group = QGroupBox()
        whisper_form = QFormLayout(self.whisper_group)
        whisper_form.addRow(self.stt_enabled)
        self._add_row(whisper_form, "whisper_model", self.stt_model)
        self._add_row(
            whisper_form,
            "whisper_model_dir",
            self._path_picker(
                self.whisper_model_dir,
                self.whisper_model_browse,
            ),
        )
        whisper_form.addRow(self.whisper_status)
        whisper_form.addRow(self.whisper_progress)
        whisper_form.addRow(self.whisper_download_button)
        self._add_row(
            whisper_form,
            "stt_language",
            self.stt_language,
        )
        whisper_form.addRow(self.stt_language_help)
        self._add_row(
            whisper_form,
            "stt_input_device",
            self.stt_input_device,
        )
        self._add_row(whisper_form, "stt_device", self.stt_device)
        layout.addWidget(self.whisper_group)
        layout.addWidget(self.vision_group)
        layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        return scroll

    def _build_vision_ollama_panel(self) -> QWidget:
        panel = QWidget()
        form = QFormLayout(panel)
        self.vision_ollama_url = QLineEdit()
        self.vision_ollama_model = self._editable_combo()
        self.vision_ollama_context_window = self._spinbox(
            2_048,
            131_072,
        )
        self.vision_ollama_context_window.setSingleStep(1_024)
        self.vision_ollama_keep_alive = QLineEdit()
        self._add_row(form, "server_url", self.vision_ollama_url)
        self._add_row(form, "vision_model", self.vision_ollama_model)
        self._add_row(
            form,
            "context_window",
            self.vision_ollama_context_window,
        )
        self._add_row(
            form,
            "keep_alive",
            self.vision_ollama_keep_alive,
        )
        return panel

    def _build_vision_aliyun_panel(self) -> QWidget:
        panel = QWidget()
        form = QFormLayout(panel)
        self.vision_aliyun_key = self._password_field()
        self.vision_aliyun_url = QLineEdit()
        self.vision_aliyun_model = self._editable_combo()
        self._add_row(
            form,
            "aliyun_api_key",
            self.vision_aliyun_key,
        )
        self._add_row(form, "base_url", self.vision_aliyun_url)
        self._add_row(form, "vision_model", self.vision_aliyun_model)
        return panel

    def _build_vision_openai_panel(self) -> QWidget:
        panel = QWidget()
        form = QFormLayout(panel)
        self.vision_openai_key = self._password_field()
        self.vision_openai_url = QLineEdit()
        self.vision_openai_model = self._editable_combo()
        self.vision_openai_model.addItems(
            ["gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"]
        )
        self._add_row(
            form,
            "openai_api_key",
            self.vision_openai_key,
        )
        self._add_row(form, "base_url", self.vision_openai_url)
        self._add_row(form, "vision_model", self.vision_openai_model)
        return panel

    def _build_automation_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.automation_group = QGroupBox()
        behavior_form = QFormLayout(self.automation_group)
        self.automation_help_button = self._help_button(
            "automation_help_title",
            "automation_help_body",
        )
        behavior_form.addRow(
            self._right_aligned_widget(self.automation_help_button)
        )
        self.thinking_minutes = self._spinbox(1, 1_440)
        self.away_minutes = self._spinbox(2, 1_440)
        self.history_limit = self._spinbox(4, 200)
        self.response_target = self._response_target_combo()
        self.important_response_target = self._response_target_combo()
        self.do_not_disturb = QCheckBox()
        self._add_row(
            behavior_form,
            "thinking_reminder",
            self.thinking_minutes,
        )
        self._add_row(behavior_form, "away_reminder", self.away_minutes)
        self._add_row(
            behavior_form,
            "conversation_memory",
            self.history_limit,
        )
        self._add_row(behavior_form, "response_target", self.response_target)
        self._add_row(
            behavior_form,
            "important_response_target",
            self.important_response_target,
        )
        behavior_form.addRow(self.do_not_disturb)
        layout.addWidget(self.automation_group)
        layout.addStretch(1)
        return page

    def _build_display_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.display_group = QGroupBox()
        display_form = QFormLayout(self.display_group)
        self.display_help_button = self._help_button(
            "display_help_title",
            "display_help_body",
        )
        display_form.addRow(
            self._right_aligned_widget(self.display_help_button)
        )
        self.screen_index = self._spinbox(0, 32)
        self.portrait_ratio = QDoubleSpinBox()
        self.portrait_ratio.setRange(0.2, 1.0)
        self.portrait_ratio.setSingleStep(0.05)
        self.portrait_ratio.setDecimals(2)
        self.show_log_console = QCheckBox()
        self._add_row(display_form, "screen_index", self.screen_index)
        self._add_row(
            display_form,
            "portrait_ratio",
            self.portrait_ratio,
        )
        display_form.addRow(self.show_log_console)
        layout.addWidget(self.display_group)
        layout.addStretch(1)
        return page

    def _build_other_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.data_group = QGroupBox()
        data_form = QFormLayout(self.data_group)
        self.clear_history_button = QPushButton()
        self.clear_history_button.setEnabled(not self._first_run)
        self.clear_history_button.clicked.connect(
            self._confirm_clear_history
        )
        self.clear_cache_button = QPushButton()
        self.clear_cache_button.clicked.connect(self._confirm_clear_cache)
        self._add_row(
            data_form,
            "conversation_history_data",
            self.clear_history_button,
        )
        self._add_row(
            data_form,
            "runtime_cache_data",
            self.clear_cache_button,
        )
        layout.addWidget(self.data_group)
        layout.addStretch(1)
        return page

    @staticmethod
    def _editable_combo() -> QComboBox:
        combo = QComboBox()
        combo.setEditable(True)
        combo.setInsertPolicy(QComboBox.NoInsert)
        return combo

    def _response_target_combo(self) -> QComboBox:
        combo = self._editable_combo()
        for seconds in (5, 10, 20, 30, 60):
            combo.addItem(str(seconds), seconds)
        combo.addItem("Unlimited", 0)
        combo.addItem("Custom…", None)
        combo.activated[int].connect(
            lambda index, field=combo: self._activate_response_target(field, index)
        )
        return combo

    @staticmethod
    def _activate_response_target(combo: QComboBox, index: int) -> None:
        if combo.itemData(index) is None:
            combo.setEditText("")
            combo.setFocus()

    @staticmethod
    def _set_response_target(combo: QComboBox, seconds: int) -> None:
        index = combo.findData(seconds)
        if index >= 0:
            combo.setCurrentIndex(index)
        else:
            combo.setEditText(str(seconds))

    @staticmethod
    def _response_target_value(combo: QComboBox) -> int | str:
        text = combo.currentText().strip()
        if text.casefold() in {"unlimited", "不限"}:
            return 0
        # Preserve invalid/empty input for the existing Pydantic validation UI.
        return text

    def _translate_response_target(self, combo: QComboBox) -> None:
        index = combo.currentIndex()
        text = combo.currentText()
        selected_label = index >= 0 and text == combo.itemText(index)
        combo.setItemText(combo.findData(0), self._text("response_unlimited"))
        combo.setItemText(combo.count() - 1, self._text("response_custom"))
        if not selected_label:
            # A custom number may retain the previous selected item's data.
            # Translating that item's label must not overwrite the typed value.
            combo.setEditText(text)
        combo.setToolTip(self._text("response_target_help"))
        combo.lineEdit().setPlaceholderText(self._text("response_custom_placeholder"))

    @staticmethod
    def _path_picker(field: QLineEdit, button: QPushButton) -> QWidget:
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(field, 1)
        layout.addWidget(button)
        return container

    @staticmethod
    def _password_field() -> QLineEdit:
        field = QLineEdit()
        field.setEchoMode(QLineEdit.Password)
        return field

    @staticmethod
    def _spinbox(minimum: int, maximum: int) -> QSpinBox:
        spinbox = QSpinBox()
        spinbox.setRange(minimum, maximum)
        return spinbox

    @staticmethod
    def _set_combo_data(combo: QComboBox, value: str) -> None:
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    @staticmethod
    def _set_combo_item_text(
        combo: QComboBox,
        data: str,
        text: str,
    ) -> None:
        index = combo.findData(data)
        if index >= 0:
            combo.setItemText(index, text)

    @staticmethod
    def _set_editable_combo(combo: QComboBox, value: str) -> None:
        if combo.findText(value) < 0:
            combo.addItem(value)
        combo.setCurrentText(value)

    def _stt_language_value(self) -> str:
        text = self.stt_language.currentText().strip()
        index = self.stt_language.currentIndex()
        if index >= 0 and text == self.stt_language.itemText(index):
            data = self.stt_language.itemData(index)
            if isinstance(data, str) and data:
                return data
        return text.lower()

    def _set_stt_language(self, value: str) -> None:
        index = self.stt_language.findData(value)
        if index < 0:
            self.stt_language.addItem(value, value)
            index = self.stt_language.count() - 1
        self.stt_language.setCurrentIndex(index)

    def _retranslate_stt_languages(self) -> None:
        current = self._stt_language_value()
        self._set_combo_item_text(
            self.stt_language,
            "ui",
            self._text("stt_language_ui"),
        )
        self._set_combo_item_text(
            self.stt_language,
            "auto",
            self._text("stt_language_auto"),
        )
        english_ui = self._language() == "en"
        for code, english_name, chinese_name in COMMON_WHISPER_LANGUAGES:
            name = english_name if english_ui else chinese_name
            self._set_combo_item_text(
                self.stt_language,
                code,
                f"{name} ({code})",
            )
        self._set_stt_language(current)
        help_text = self._text("stt_language_help")
        self.stt_language.setToolTip(help_text)
        self.stt_language_help.setText(help_text)

    def _load_values(self, settings: AppSettings) -> None:
        self._set_combo_data(self.language_combo, settings.ui_language)
        self._set_combo_data(self.mode_combo, settings.mode)
        self.ollama_url.setText(settings.ollama.base_url)
        self._set_editable_combo(
            self.ollama_chat_model,
            settings.ollama.chat_model,
        )
        self.ollama_timeout.setValue(settings.ollama.timeout_seconds)
        self.ollama_context_window.setValue(settings.ollama.context_window)
        self.ollama_keep_alive.setText(settings.ollama.keep_alive)

        self._set_combo_data(self.api_provider, settings.api.provider)
        self.deepseek_key.setText(settings.api.deepseek_api_key)
        self.deepseek_url.setText(settings.api.deepseek_base_url)
        self._set_editable_combo(
            self.deepseek_chat_model,
            settings.api.deepseek_chat_model,
        )
        self.deepseek_thinking.setChecked(settings.api.deepseek_thinking)
        self.deepseek_thinking_max_tokens.setValue(
            settings.api.deepseek_thinking_max_tokens
        )
        self.aliyun_key.setText(settings.api.aliyun_api_key)
        self.aliyun_url.setText(settings.api.aliyun_base_url)
        self._set_editable_combo(
            self.aliyun_chat_model,
            settings.api.aliyun_chat_model,
        )
        self.openai_key.setText(settings.api.openai_api_key)
        self.openai_url.setText(settings.api.openai_base_url)
        self._set_editable_combo(
            self.openai_chat_model,
            settings.api.openai_chat_model,
        )
        self.api_timeout.setValue(settings.api.timeout_seconds)

        self.user_name.setText(settings.character.user_name)
        self._set_combo_data(self.portrait, settings.character.portrait)
        self._set_combo_data(self.outfit, settings.character.outfit)
        for language in ("en", "zh-CN"):
            try:
                self._personality_prompts[language] = load_personality(
                    settings,
                    language,
                )
            except OSError:
                self._personality_prompts[language] = ""
        self._editing_language = settings.ui_language
        self.personality_prompt.setPlainText(
            self._personality_prompts.get(self._editing_language, "")
        )

        self.vision_enabled.setChecked(settings.vision.enabled)
        self.vision_interval.setValue(settings.vision.interval_seconds)
        self.vision_timeout.setValue(settings.vision.timeout_seconds)
        self._set_combo_data(
            self.vision_provider,
            settings.vision.provider,
        )
        self.vision_ollama_url.setText(
            settings.vision.ollama_base_url
        )
        self._set_editable_combo(
            self.vision_ollama_model,
            settings.vision.ollama_model,
        )
        self.vision_ollama_context_window.setValue(
            settings.vision.ollama_context_window
        )
        self.vision_ollama_keep_alive.setText(
            settings.vision.ollama_keep_alive
        )
        self.vision_aliyun_key.setText(settings.vision.aliyun_api_key)
        self.vision_aliyun_url.setText(settings.vision.aliyun_base_url)
        self._set_editable_combo(
            self.vision_aliyun_model,
            settings.vision.aliyun_model,
        )
        self.vision_openai_key.setText(settings.vision.openai_api_key)
        self.vision_openai_url.setText(settings.vision.openai_base_url)
        self._set_editable_combo(
            self.vision_openai_model,
            settings.vision.openai_model,
        )
        self.tts_enabled.setChecked(settings.tts.enabled)
        self._set_combo_data(self.tts_backend, settings.tts.backend)
        self.tts_url.setText(settings.tts.base_url)
        self.tts_voice_profile.setText(settings.tts.voice_profile)
        self.tts_emotional_vits_profile.setText(settings.tts.emotional_vits_profile)
        self.tts_timeout.setValue(settings.tts.timeout_seconds)
        self.tts_storage_root.setText(settings.tts.storage_root)
        self.tts_advanced_paths.setChecked(settings.tts.advanced_paths)
        self.tts_engine_root.setText(settings.tts.engine_root)
        self.tts_model_dir.setText(settings.tts.model_dir)
        self.tts_autodl_ssh_command.setText(
            settings.tts.autodl_ssh_command
        )
        self.tts_autodl_remote_command.setText(
            settings.tts.autodl_remote_command
        )
        self.tts_autodl_reference_root.setText(
            settings.tts.autodl_remote_reference_root
        )
        self.stt_enabled.setChecked(settings.stt.enabled)
        self._set_editable_combo(self.stt_model, settings.stt.model)
        self.whisper_model_dir.setText(settings.stt.model_dir)
        self._set_audio_input_device(settings.stt.input_device)
        self._set_combo_data(self.stt_device, settings.stt.device)
        self._set_stt_language(settings.stt.language)
        self.screen_index.setValue(settings.display.screen_index)
        self.portrait_ratio.setValue(
            settings.display.portrait_screen_ratio
        )
        self.show_log_console.setChecked(
            settings.display.show_log_console
        )
        self.thinking_minutes.setValue(settings.idle.thinking_minutes)
        self.away_minutes.setValue(settings.idle.away_minutes)
        self.do_not_disturb.setChecked(settings.idle.do_not_disturb)
        self.history_limit.setValue(settings.history_limit)
        self._set_response_target(
            self.response_target,
            settings.interaction.response_target_seconds,
        )
        self._set_response_target(
            self.important_response_target,
            settings.interaction.important_response_target_seconds,
        )

    def _language(self) -> str:
        language = self.language_combo.currentData()
        return language if language in TRANSLATIONS else "en"

    def _stash_current_personality(self) -> None:
        self._personality_prompts[self._editing_language] = (
            self.personality_prompt.toPlainText()
        )

    def _text(self, key: str, **values: object) -> str:
        text = TRANSLATIONS[self._language()][key]
        return text.format(**values) if values else text

    def _set_audio_input_device(self, identifier: str) -> None:
        index = self.stt_input_device.findData(identifier)
        if index < 0 and identifier:
            name, hostapi = decode_audio_input_device(identifier)
            description = f"{name} — {hostapi}" if hostapi else name
            self.stt_input_device.addItem(description, identifier)
            self._missing_audio_input_identifier = identifier
            index = self.stt_input_device.count() - 1
        self.stt_input_device.setCurrentIndex(max(index, 0))

    def _refresh_audio_input_devices(self, force: bool = False) -> None:
        if not hasattr(self, "stt_input_device"):
            return
        default_device, input_devices = refresh_audio_input_devices()
        signature: tuple[object, ...] = (
            (
                default_device.identifier,
                default_device.display_name,
            )
            if default_device is not None
            else None,
            tuple(
                (device.identifier, device.display_name)
                for device in input_devices
            ),
        )
        if not force and signature == self._audio_device_signature:
            return

        selected = self.stt_input_device.currentData() or ""
        self._audio_device_signature = signature
        self._default_audio_input = default_device
        self._missing_audio_input_identifier = ""
        self.stt_input_device.blockSignals(True)
        try:
            self.stt_input_device.clear()
            self.stt_input_device.addItem("", "")
            for input_device in input_devices:
                self.stt_input_device.addItem(
                    input_device.display_name,
                    input_device.identifier,
                )
            self._set_audio_input_device(selected)
            self._retranslate_audio_input_devices()
        finally:
            self.stt_input_device.blockSignals(False)

    def _retranslate_audio_input_devices(self) -> None:
        default_name = (
            self._default_audio_input.name
            if self._default_audio_input is not None
            else ""
        )
        default_text = (
            self._text("stt_input_default", device=default_name)
            if default_name
            else self._text("stt_input_default_unknown")
        )
        self._set_combo_item_text(
            self.stt_input_device,
            "",
            default_text,
        )
        if self._missing_audio_input_identifier:
            name, hostapi = decode_audio_input_device(
                self._missing_audio_input_identifier
            )
            description = f"{name} — {hostapi}" if hostapi else name
            self._set_combo_item_text(
                self.stt_input_device,
                self._missing_audio_input_identifier,
                self._text(
                    "stt_input_unavailable",
                    device=description,
                ),
            )

    def _on_language_changed(self) -> None:
        previous_language = self._editing_language
        self._stash_current_personality()
        selected_language = self._language()
        if previous_language != selected_language:
            current_name = self.user_name.text().strip()
            if previous_language == "zh-CN" and current_name == "主人":
                self.user_name.setText("Master")
            elif previous_language == "en" and current_name == "Master":
                self.user_name.setText("主人")
        self._editing_language = selected_language
        self.personality_prompt.setPlainText(
            self._personality_prompts.get(selected_language, "")
        )
        self._retranslate_ui()

    def _retranslate_ui(self) -> None:
        title_key = "title_setup" if self._first_run else "title_settings"
        self.setWindowTitle(self._text(title_key))
        self.language_label.setText(self._text("language"))
        self.tabs.setTabText(0, self._text("tab_models"))
        self.tabs.setTabText(1, self._text("tab_extensions"))
        self.tabs.setTabText(2, self._text("tab_character"))
        self.tabs.setTabText(3, self._text("tab_automation"))
        self.tabs.setTabText(4, self._text("tab_display"))
        self.tabs.setTabText(5, self._text("tab_other"))

        self.backend_group.setTitle(self._text("backend_group"))
        self.ollama_group.setTitle(self._text("ollama_group"))
        self.api_group.setTitle(self._text("api_group"))
        self.identity_group.setTitle(self._text("identity_group"))
        self.external_character_note.setText(self._text("external_character_note"))
        self._update_character_controls()
        self.prompt_group.setTitle(self._text("prompt_group"))
        self.vision_group.setTitle(self._text("vision_group"))
        self.tts_group.setTitle(self._text("tts_group"))
        self.whisper_group.setTitle(self._text("whisper_group"))
        self.automation_group.setTitle(self._text("automation_group"))
        self.display_group.setTitle(self._text("display_group"))
        self.data_group.setTitle(self._text("data_group"))

        for key, labels in self._form_labels.items():
            for label in labels:
                label.setText(self._text(key))

        self._set_combo_item_text(
            self.mode_combo,
            "ollama",
            self._text("mode_ollama"),
        )
        self._set_combo_item_text(
            self.mode_combo,
            "api",
            self._text("mode_api"),
        )
        self._set_combo_item_text(
            self.api_provider,
            "aliyun",
            self._text("provider_aliyun"),
        )
        self._set_combo_item_text(
            self.api_provider,
            "openai",
            self._text("provider_openai"),
        )
        self._set_combo_item_text(
            self.vision_provider,
            "ollama",
            self._text("vision_provider_ollama"),
        )
        self._set_combo_item_text(
            self.vision_provider,
            "aliyun",
            self._text("vision_provider_aliyun"),
        )
        self._set_combo_item_text(
            self.vision_provider,
            "openai",
            self._text("vision_provider_openai"),
        )
        self._set_combo_item_text(
            self.tts_backend,
            "local",
            self._text("tts_backend_local"),
        )
        self._set_combo_item_text(
            self.tts_backend,
            "autodl",
            self._text("tts_backend_autodl"),
        )
        self._set_combo_item_text(
            self.tts_backend,
            "emotional_vits",
            self._text("tts_backend_emotional_vits"),
        )
        self._set_combo_item_text(
            self.portrait,
            "a",
            self._text("portrait_a"),
        )
        self._set_combo_item_text(
            self.portrait,
            "b",
            self._text("portrait_b"),
        )
        for outfit in ("sleepwear", "casual", "uniform", "kimono"):
            self._set_combo_item_text(
                self.outfit,
                outfit,
                self._text(f"outfit_{outfit}"),
            )

        self.deepseek_thinking.setText(self._text("deepseek_thinking"))
        self.do_not_disturb.setText(self._text("do_not_disturb"))
        self.do_not_disturb.setToolTip(self._text("do_not_disturb_help"))
        self._translate_response_target(self.response_target)
        self._translate_response_target(self.important_response_target)
        self.automation_help_button.setToolTip(self._text("settings_help"))
        self.automation_help_button.setAccessibleName(
            self._text("settings_help")
        )
        self.display_help_button.setToolTip(self._text("settings_help"))
        self.display_help_button.setAccessibleName(
            self._text("settings_help")
        )
        self.clear_history_button.setText(self._text("clear_history"))
        self.clear_history_button.setToolTip(
            (
                ""
                if not self._first_run
                else self._text("history_unavailable_first_run")
            )
        )
        self.clear_cache_button.setText(self._text("clear_cache"))
        self.deepseek_note.setText(self._text("deepseek_note"))
        self.fetch_models_button.setText(self._text("load_models"))
        self.model_list_help.setText(self._text("model_list_help"))
        self.fetch_vision_models_button.setText(
            self._text("load_vision_models")
        )
        self.vision_model_list_help.setText(
            self._text("vision_model_list_help")
        )
        self.prompt_help.setText(self._text("prompt_help"))
        self.personality_prompt.setPlaceholderText(
            self._text("prompt_placeholder")
        )
        self.import_button.setText(self._text("import_prompt"))
        self.vision_enabled.setText(self._text("vision_enabled"))
        self.show_log_console.setText(
            self._text("show_log_console")
        )
        self.tts_enabled.setText(self._text("tts_enabled"))
        self.tts_autodl_password.setPlaceholderText(
            self._text("tts_autodl_password_placeholder")
        )
        self.tts_storage_browse.setText(self._text("browse"))
        self.tts_emotional_vits_browse.setText(self._text("browse"))
        self.tts_emotional_vits_audition.setText(self._text("tts_emotional_vits_audition"))
        self.tts_advanced_paths.setText(
            self._text("tts_advanced_paths")
        )
        self.tts_engine_browse.setText(self._text("browse"))
        self.tts_model_browse.setText(self._text("browse"))
        self.tts_bootstrap_button.setText(self._text("tts_bootstrap"))
        self.tts_download_button.setText(self._text("tts_download"))
        self._update_tts_path_preview()
        self._render_tts_service_button()
        self.stt_enabled.setText(
            self._text(
                "stt_enabled",
                shortcut=(
                    self._platform_runtime.input.voice_trigger_shortcut()
                ),
            )
        )
        self._retranslate_audio_input_devices()
        self._retranslate_stt_languages()
        self._set_combo_item_text(
            self.stt_device,
            "auto",
            self._text("stt_device_auto"),
        )
        self._set_combo_item_text(
            self.stt_device,
            "cuda",
            self._text("stt_device_cuda"),
        )
        self._set_combo_item_text(
            self.stt_device,
            "cpu",
            self._text("stt_device_cpu"),
        )
        self.whisper_download_button.setText(
            self._text("whisper_download")
        )
        self.whisper_model_browse.setText(self._text("browse"))
        self.deepseek_key.setPlaceholderText(
            self._text("password_placeholder")
        )
        self.aliyun_key.setPlaceholderText(
            self._text("password_placeholder")
        )
        self.openai_key.setPlaceholderText(
            self._text("password_placeholder")
        )
        self.vision_aliyun_key.setPlaceholderText(
            self._text("password_placeholder")
        )
        self.vision_openai_key.setPlaceholderText(
            self._text("password_placeholder")
        )

        seconds = self._text("seconds")
        for spinbox in (
            self.ollama_timeout,
            self.api_timeout,
            self.vision_interval,
            self.vision_timeout,
            self.tts_timeout,
        ):
            spinbox.setSuffix(seconds)
        minutes = self._text("minutes")
        self.thinking_minutes.setSuffix(minutes)
        self.away_minutes.setSuffix(minutes)
        self.history_limit.setSuffix(self._text("messages"))

        save_button = self.buttons.button(QDialogButtonBox.Save)
        cancel_button = self.buttons.button(QDialogButtonBox.Cancel)
        if save_button is not None:
            save_button.setText(self._text("save"))
        if cancel_button is not None:
            cancel_button.setText(self._text("cancel"))

        self._update_vision_compatibility()
        self._render_status()
        self._render_whisper_status()
        self._render_tts_status()

    def _set_status(self, key: str | None, **values: object) -> None:
        self._status_key = key
        self._status_values = values
        self._render_status()

    def _render_status(self) -> None:
        if self._status_key is None:
            self.status_label.clear()
            return
        self.status_label.setText(
            self._text(self._status_key, **self._status_values)
        )

    def _set_whisper_status(
        self,
        key: str,
        **values: object,
    ) -> None:
        self._whisper_status_key = key
        self._whisper_status_values = values
        self._render_whisper_status()

    def _render_whisper_status(self) -> None:
        if not hasattr(self, "whisper_status"):
            return
        self.whisper_status.setText(
            self._text(
                self._whisper_status_key,
                **self._whisper_status_values,
            )
        )

    def _set_tts_status(self, key: str, **values: object) -> None:
        self._tts_status_key = key
        self._tts_status_values = values
        self._render_tts_status()

    def _render_tts_status(self) -> None:
        if not hasattr(self, "tts_status"):
            return
        self.tts_status.setText(
            self._text(self._tts_status_key, **self._tts_status_values)
        )

    def _update_whisper_state(self) -> None:
        if not hasattr(self, "whisper_download_button"):
            return
        enabled = self.stt_enabled.isChecked()
        model_name = self.stt_model.currentText().strip()
        model_directory = self.whisper_model_dir.text().strip()
        repository = model_repository(model_name) if model_name else ""
        snapshot = self.download_manager.snapshot(
            whisper_job_id(repository)
        )
        downloading = snapshot.status in {
            "preparing",
            "checking",
            "downloading",
        }
        self.stt_model.setEnabled(enabled and not downloading)
        self.whisper_model_dir.setEnabled(enabled and not downloading)
        self.whisper_model_browse.setEnabled(enabled and not downloading)
        self.stt_device.setEnabled(enabled)
        self.stt_language.setEnabled(enabled)
        self.stt_input_device.setEnabled(enabled)
        self.whisper_download_button.setEnabled(False)
        self._render_progress(self.whisper_progress, snapshot)
        if not enabled:
            self._pending_whisper_model = None
            self._set_whisper_status("whisper_disabled")
            return
        if not model_name:
            self._set_whisper_status("whisper_missing")
            return
        if downloading:
            return
        if (
            self._whisper_check_worker is not None
            and self._whisper_check_worker.isRunning()
        ):
            self._pending_whisper_model = (
                model_name,
                model_directory,
            )
            self._set_whisper_status("whisper_checking")
            return
        self._start_whisper_check(model_name, model_directory)

    def _start_whisper_check(
        self,
        model_name: str,
        model_directory: str,
    ) -> None:
        self._set_whisper_status("whisper_checking")
        worker = WhisperModelCheckWorker(
            model_name,
            model_directory,
            self,
        )
        self._whisper_check_worker = worker
        worker.checked.connect(self._on_whisper_checked)
        worker.finished.connect(
            lambda: self._finish_whisper_check(worker)
        )
        worker.start()

    def _on_whisper_checked(
        self,
        model_name: str,
        model_directory: str,
        path: str,
    ) -> None:
        if (
            not self.stt_enabled.isChecked()
            or model_name != self.stt_model.currentText().strip()
            or model_directory != self.whisper_model_dir.text().strip()
        ):
            return
        if path:
            self.whisper_progress.hide()
            self.whisper_download_button.setEnabled(False)
            self._set_whisper_status("whisper_installed", path=path)
        else:
            repository = model_repository(model_name)
            snapshot = self.download_manager.snapshot(
                whisper_job_id(repository)
            )
            if snapshot.status in {
                "preparing",
                "checking",
                "downloading",
            }:
                self._render_whisper_download(model_name, snapshot)
            elif snapshot.status == "failed":
                self.whisper_download_button.setEnabled(True)
                self._set_whisper_status(
                    "whisper_download_failed",
                    message=snapshot.message,
                )
            else:
                is_path = looks_like_local_model_path(model_name)
                self.whisper_download_button.setEnabled(not is_path)
                self._set_whisper_status(
                    "whisper_path_missing" if is_path else "whisper_missing"
                )

    def _finish_whisper_check(
        self,
        worker: WhisperModelCheckWorker,
    ) -> None:
        if self._whisper_check_worker is worker:
            self._whisper_check_worker = None
        worker.deleteLater()
        if self._closing:
            self._pending_whisper_model = None
            return
        pending = self._pending_whisper_model
        self._pending_whisper_model = None
        current = (
            self.stt_model.currentText().strip(),
            self.whisper_model_dir.text().strip(),
        )
        if (
            self.stt_enabled.isChecked()
            and pending
            and pending == current
        ):
            self._start_whisper_check(*pending)

    def _download_whisper_model(self) -> None:
        model_name = self.stt_model.currentText().strip()
        if (
            not self.stt_enabled.isChecked()
            or not model_name
            or looks_like_local_model_path(model_name)
        ):
            return

        destination = self._require_download_directory(
            self.whisper_model_dir,
            "whisper_path_required",
        )
        if destination is None:
            return
        job_id = self.download_manager.start_whisper(
            model_name,
            destination,
        )
        snapshot = self.download_manager.snapshot(job_id)
        self.whisper_download_button.setEnabled(False)
        self.stt_model.setEnabled(False)
        self.whisper_model_dir.setEnabled(False)
        self.whisper_model_browse.setEnabled(False)
        self._render_whisper_download(model_name, snapshot)

    def _render_whisper_download(
        self,
        model_name: str,
        snapshot: DownloadSnapshot,
    ) -> None:
        self._render_progress(self.whisper_progress, snapshot)
        detail = self._download_detail(snapshot)
        if snapshot.status == "preparing":
            self._set_whisper_status(
                "whisper_preparing",
                model=model_name,
                detail=detail,
            )
            return
        if snapshot.status == "checking":
            self._set_whisper_status(
                "whisper_verifying",
                model=model_name,
                detail=detail,
            )
            return
        self._set_whisper_status(
            "whisper_downloading",
            model=model_name,
            detail=detail,
        )

    def _on_tts_backend_changed(self) -> None:
        # Editing a selection must not terminate a service still in use.
        if self.tts_backend.currentData() == "autodl":
            self.tts_url.setText("http://127.0.0.1:9880/tts")
        self._update_tts_state()

    def _uses_emotional_vits(self) -> bool:
        return self.tts_backend.currentData() == "emotional_vits"

    def _uses_gpt_voice_profile(self) -> bool:
        return not self._uses_emotional_vits() and bool(self.tts_voice_profile.text().strip())

    def _current_tts_settings(
        self,
        *,
        enabled: bool | None = None,
    ) -> TTSSettings:
        engine_root, model_dir = self._effective_tts_path_strings()
        profile = self.tts_voice_profile.text().strip()
        gpt_profile = self._uses_gpt_voice_profile()
        return TTSSettings(
            voice_profile=profile,
            emotional_vits_profile=self.tts_emotional_vits_profile.text().strip(),
            enabled=(
                self.tts_enabled.isChecked()
                if enabled is None
                else enabled
            ),
            backend="local" if gpt_profile else self.tts_backend.currentData() or "local",
            base_url=self._legacy_tts_base_url if gpt_profile else self.tts_url.text().strip(),
            storage_root=self.tts_storage_root.text().strip(),
            advanced_paths=gpt_profile or self.tts_advanced_paths.isChecked(),
            engine_root=engine_root,
            model_dir=model_dir,
            autodl_ssh_command=(
                self.tts_autodl_ssh_command.text().strip()
            ),
            autodl_remote_command=(
                self.tts_autodl_remote_command.text().strip()
            ),
            autodl_remote_reference_root=(
                self.tts_autodl_reference_root.text().strip()
            ),
            autodl_password_encrypted=(
                self._original.tts.autodl_password_encrypted
            ),
            timeout_seconds=self.tts_timeout.value(),
        )

    def _effective_tts_path_strings(self) -> tuple[str, str]:
        if self.tts_advanced_paths.isChecked() or self.tts_voice_profile.text().strip():
            return (
                self.tts_engine_root.text().strip(),
                self.tts_model_dir.text().strip(),
            )
        storage_root = self.tts_storage_root.text().strip()
        if not storage_root:
            return "", ""
        engine, model = tts_paths_for_storage_root(storage_root)
        return str(engine), str(model)

    def _update_tts_path_preview(self) -> None:
        if not hasattr(self, "tts_path_preview"):
            return
        engine, model = self._effective_tts_path_strings()
        self.tts_path_preview.setText(
            self._text(
                "tts_storage_preview",
                engine=engine or "—",
                model=model or "—",
            )
        )

    def _on_tts_storage_root_changed(self) -> None:
        storage_root = self.tts_storage_root.text().strip()
        if storage_root and not self.tts_advanced_paths.isChecked():
            engine, model = tts_paths_for_storage_root(storage_root)
            self.tts_engine_root.setText(str(engine))
            self.tts_model_dir.setText(str(model))
        self._update_tts_path_preview()
        self._update_tts_state()

    def _on_tts_advanced_paths_changed(self, checked: bool) -> None:
        if checked:
            engine, model = tts_paths_for_storage_root(
                self.tts_storage_root.text().strip() or Path.home()
            )
            if not self.tts_engine_root.text().strip():
                self.tts_engine_root.setText(str(engine))
            if not self.tts_model_dir.text().strip():
                self.tts_model_dir.setText(str(model))
        self._update_tts_path_preview()
        self._update_tts_state()

    def _set_tts_path_controls(self, *, downloading: bool) -> None:
        enabled = self.tts_enabled.isChecked()
        emotional = self._uses_emotional_vits()
        profile = self._uses_gpt_voice_profile()
        autodl = not profile and self.tts_backend.currentData() == "autodl"
        controls_enabled = enabled and not downloading
        local_enabled = controls_enabled and not autodl and not emotional
        autodl_enabled = controls_enabled and autodl
        normal_paths = not profile and not self.tts_advanced_paths.isChecked()
        local_keys = ("tts_endpoint",)
        normal_path_keys = ("tts_storage_root",)
        advanced_path_keys = ("tts_engine_root", "tts_model_dir")
        autodl_keys = (
            "tts_autodl_ssh_command",
            "tts_autodl_password",
            "tts_autodl_remote_command",
            "tts_autodl_reference_root",
        )

        self.tts_backend.setEnabled(controls_enabled)
        self.tts_timeout.setEnabled(controls_enabled)
        self.tts_voice_profile.setEnabled(controls_enabled and not emotional)
        self.tts_emotional_vits_profile.setEnabled(controls_enabled and emotional)
        self.tts_emotional_vits_browse.setEnabled(controls_enabled and emotional)
        self.tts_emotional_vits_audition.setVisible(
            emotional or self._original.character.profile_id == "nene"
        )
        self.tts_emotional_vits_audition.setEnabled(not downloading and not self._closing)
        self._set_form_rows_visible(("tts_emotional_vits_profile",), emotional)
        self._set_form_labels_enabled(("tts_emotional_vits_profile",), controls_enabled and emotional)
        self._set_form_labels_enabled(("tts_voice_profile",), controls_enabled and not emotional)
        self.tts_url.setEnabled(local_enabled)
        self.tts_storage_root.setEnabled(local_enabled and normal_paths)
        self.tts_storage_browse.setEnabled(local_enabled and normal_paths)
        self.tts_advanced_paths.setEnabled(local_enabled)
        self.tts_engine_root.setEnabled(local_enabled and not normal_paths)
        self.tts_engine_browse.setEnabled(local_enabled and not normal_paths)
        self.tts_model_dir.setEnabled(local_enabled and not normal_paths)
        self.tts_model_browse.setEnabled(local_enabled and not normal_paths)
        for field in (
            self.tts_autodl_ssh_command,
            self.tts_autodl_password,
            self.tts_autodl_remote_command,
            self.tts_autodl_reference_root,
        ):
            field.setEnabled(autodl_enabled)

        self._set_form_rows_visible(local_keys, not autodl and not emotional)
        self._set_form_rows_visible(
            normal_path_keys,
            not autodl and not emotional and normal_paths,
        )
        self._set_form_rows_visible(
            advanced_path_keys,
            not autodl and not emotional and not normal_paths,
        )
        self._set_form_rows_visible(autodl_keys, autodl)
        self.tts_path_preview.setVisible(not autodl and not emotional and normal_paths)
        self.tts_advanced_paths.setVisible(not autodl and not emotional)
        self.tts_download_button.setVisible(not autodl and not emotional)
        bootstrap_visible = (
            not autodl
            and not emotional
            and self._platform_runtime.capabilities.managed_tts_bootstrap
        )
        self._set_form_rows_visible(("tts_bootstrap",), bootstrap_visible)
        self.tts_bootstrap_button.setEnabled(
            local_enabled
            and self._tts_bootstrap_worker is None
        )
        self._set_voice_profile_controls(profile)
        if autodl or emotional:
            self.tts_progress.hide()
            self.tts_extract_progress.hide()

        self._set_form_labels_enabled(
            ("tts_backend", "tts_timeout"),
            controls_enabled,
        )
        self._set_form_labels_enabled(
            local_keys,
            local_enabled,
        )
        self._set_form_labels_enabled(
            normal_path_keys,
            local_enabled and normal_paths,
        )
        self._set_form_labels_enabled(
            advanced_path_keys,
            local_enabled and not normal_paths,
        )
        self._set_form_labels_enabled(
            ("tts_bootstrap",),
            local_enabled,
        )
        self._set_form_labels_enabled(
            autodl_keys,
            autodl_enabled,
        )

    def _update_tts_state(self) -> None:
        if self._closing or not hasattr(self, "tts_status"):
            return
        enabled = self.tts_enabled.isChecked()
        emotional = self._uses_emotional_vits()
        autodl = self.tts_backend.currentData() == "autodl"
        snapshot = self.download_manager.snapshot(TTS_JOB_ID)
        downloading = snapshot.status in {
            "preparing",
            "checking",
            "downloading",
            "extracting",
            "installing",
            "cleaning",
        }
        if emotional:
            downloading = False
        bootstrapping = (
            not emotional
            and
            self._tts_bootstrap_worker is not None
            and self._tts_bootstrap_worker.isRunning()
        )
        self._set_tts_path_controls(
            downloading=downloading or bootstrapping,
        )
        self.tts_download_button.setEnabled(False)
        self.tts_service_button.setVisible(True)
        self._set_tts_service_button("start", False)
        profile = self._uses_gpt_voice_profile()
        if (
            self._tts_service_worker is not None
            and self._tts_service_worker.isRunning()
        ):
            return
        if downloading and not profile:
            self._render_tts_download(snapshot)
            return
        if bootstrapping and not profile:
            return
        self.tts_progress.hide()
        self.tts_extract_progress.hide()
        if not enabled:
            self._set_tts_status("tts_disabled")
            return
        if (
            self._tts_check_worker is not None
            and self._tts_check_worker.isRunning()
        ):
            self._pending_tts_check = True
            self._set_tts_status("tts_emotional_vits_checking" if emotional else "tts_checking")
            return
        try:
            settings = self._current_tts_settings(enabled=True)
        except ValidationError:
            self._set_tts_status("tts_invalid")
            return
        if emotional and not settings.emotional_vits_profile:
            self._on_tts_check_failed(self._text("tts_emotional_vits_missing_path"))
            return
        self._set_tts_status("tts_emotional_vits_checking" if emotional else "tts_checking")
        worker = TTSCheckWorker(settings, self)
        self._tts_check_worker = worker
        worker.checked.connect(lambda state, reachable: self._on_tts_checked(
            state, reachable, checked_settings=worker.settings,
        ))
        worker.failed.connect(lambda message: self._on_tts_check_failed(
            message, checked_settings=worker.settings,
        ))
        worker.finished.connect(lambda: self._finish_tts_check(worker))
        worker.start()

    def _external_character_controls(self) -> bool:
        return (
            self._original.character.profile_id != "murasame"
            or bool(self.tts_voice_profile.text().strip())
        )

    def _update_character_controls(self) -> None:
        external = self._external_character_controls()
        self._set_form_rows_visible(("portrait_set", "outfit", "portrait_ratio"), not external)
        self.portrait.setEnabled(not external)
        self.outfit.setEnabled(not external)
        self.portrait_ratio.setEnabled(not external)
        self.external_character_note.setVisible(external)

    def _set_voice_profile_controls(self, active: bool) -> None:
        if active and not self._voice_profile_controls_active:
            self._legacy_tts_base_url = self.tts_url.text().strip()
        elif not active and self._voice_profile_controls_active:
            self.tts_url.setText(self._legacy_tts_base_url)
        self._voice_profile_controls_active = active
        self._update_character_controls()
        # A GPT profile cannot use AutoDL, but must allow choosing the
        # independent Emotional VITS backend without clearing that profile.
        self.tts_backend.model().item(self.tts_backend.findData("autodl")).setEnabled(not active)
        if not active:
            return
        previous = self.tts_backend.blockSignals(True)
        self._set_combo_data(self.tts_backend, "local")
        self.tts_backend.blockSignals(previous)
        self.tts_url.setText(PROFILE_BASE_URL)
        self.tts_url.setEnabled(False)
        self.tts_advanced_paths.setVisible(False)
        self.tts_advanced_paths.setEnabled(False)
        self._set_form_rows_visible(("tts_model_dir", "tts_bootstrap"), False)
        self.tts_model_dir.setEnabled(False)
        self.tts_model_browse.setEnabled(False)
        self.tts_bootstrap_button.setEnabled(False)
        self.tts_download_button.setEnabled(False)
        self.tts_download_button.hide()
        self.tts_progress.hide()
        self.tts_extract_progress.hide()
        self._tts_engine_download_needed = False

    def _update_voice_profile_state(
        self,
        enabled: bool,
        *,
        state: TTSAssetState | None = None,
        reachable: bool = False,
    ) -> None:
        """Display only verified profile references, never Murasame downloads."""
        if self._uses_emotional_vits():
            return
        if not enabled:
            self._set_tts_status("tts_disabled")
            return
        try:
            if state is None:
                settings = self._current_tts_settings()
                state = locate_tts_assets(
                    configured_engine_root=settings.engine_root,
                    configured_voice_profile=settings.voice_profile,
                )
            profile = state.voice_profile
            if profile is None:
                raise ValueError("没有找到所选角色语音配置")
            missing = sorted(EMOTIONS - profile.references.keys())
            owned = get_tts_service_manager().owns_running_process()
            if not state.engine_ready:
                service = self._text("tts_profile_incomplete_engine")
            elif reachable and not owned:
                service = self._text("tts_profile_external")
            else:
                service = self._text("tts_ready_online" if reachable else "tts_ready_offline")
            self._set_tts_status(
                "tts_profile_baseline_state" if profile.baseline_reference is not None and missing and state.engine_ready else "tts_profile_state",
                character=(self._original.character.name_en or "Nene") if self._language() == "en" else (self._original.character.name or "宁宁"),
                configured="、".join(sorted(profile.references)) or ("none" if self._language() == "en" else "无"),
                missing="、".join(missing) or ("none" if self._language() == "en" else "无"),
                service=service,
            )
            self._set_tts_service_button(
                "stop" if owned else "start",
                owned or (state.engine_ready and not reachable),
            )
        except Exception as exc:
            self._set_tts_status("tts_profile_failed", message=str(exc))
            self._set_tts_service_button("start", False)

    def _tts_check_matches_current(self, settings: TTSSettings | None) -> bool:
        if settings is None:
            return True
        try:
            current = self._current_tts_settings(enabled=True)
        except ValidationError:
            return False
        if current.backend != settings.backend:
            return False
        if current.backend == "emotional_vits":
            return current.emotional_vits_profile == settings.emotional_vits_profile
        # Timeout edits do not invalidate a resource/endpoint check or require
        # another worker. Reject only results for a different resource/service.
        fields = (
            "voice_profile", "engine_root", "model_dir", "base_url",
            "autodl_ssh_command", "autodl_remote_command", "autodl_remote_reference_root",
        )
        return all(getattr(current, name) == getattr(settings, name) for name in fields)

    def _update_emotional_vits_state(self, state: EmotionalVITSCheckState, owned: bool) -> None:
        profile = state.profile
        if profile.manifest_path != Path(self.tts_emotional_vits_profile.text().strip()).expanduser().resolve():
            return
        mapped = dict(profile.emotion_ids)
        missing = sorted(EMOTIONS - profile.configured_emotions())
        routes = [f"{emotion}={identifier} ({self._text('tts_emotional_vits_bank_route')})"
                  for emotion, identifier in sorted(mapped.items())]
        routes.extend(f"{emotion}={self._text('tts_emotional_vits_vector_route')} ({vector.path.name})"
                      for emotion, vector in sorted(profile.emotion_vectors.items()))
        if state.service_signature == profile.signature:
            service = "tts_emotional_vits_online"
        elif state.service_signature is not None:
            service = "tts_emotional_vits_changed"
        else:
            service = "tts_emotional_vits_starting" if owned else "tts_emotional_vits_offline"
        self._set_tts_status(
            "tts_emotional_vits_state",
            configured="、".join(routes) or "—",
            missing="、".join(missing) or ("none" if self._language() == "en" else "无"),
            status=str(profile.status),
            service=self._text(service),
        )
        self._set_tts_service_button("stop" if owned else "start", True)

    def _on_tts_check_failed(self, message: str, *, checked_settings: TTSSettings | None = None) -> None:
        if self._closing or not self.tts_enabled.isChecked() or not self._tts_check_matches_current(checked_settings):
            return
        emotional = self._uses_emotional_vits()
        key = "tts_emotional_vits_failed" if emotional else (
            "tts_profile_failed" if self._uses_gpt_voice_profile() else "tts_service_failed"
        )
        self._set_tts_status(key, message=message)
        self.tts_download_button.setEnabled(False)
        owned = emotional and get_emotional_vits_service_manager().owns_running_process()
        self._set_tts_service_button("stop" if owned else "start", owned)

    def _on_tts_checked(
        self,
        state: TTSAssetState | EmotionalVITSCheckState,
        reachable: bool,
        *,
        checked_settings: TTSSettings | None = None,
    ) -> None:
        if self._closing or not self.tts_enabled.isChecked() or not self._tts_check_matches_current(checked_settings):
            return
        if self._uses_emotional_vits():
            if isinstance(state, EmotionalVITSCheckState):
                self._update_emotional_vits_state(state, reachable)
            return
        if isinstance(state, EmotionalVITSCheckState):
            return
        configured_profile = self.tts_voice_profile.text().strip()
        if configured_profile:
            if state.voice_profile is None or state.voice_profile.manifest_path != Path(configured_profile).expanduser().resolve():
                return
            self._update_voice_profile_state(True, state=state, reachable=reachable)
            return
        if state.voice_profile is not None:
            return
        autodl = self.tts_backend.currentData() == "autodl"
        if autodl:
            self._tts_engine_download_needed = False
            if reachable:
                if get_tts_service_manager().owns_running_process():
                    self._set_tts_service_button("stop", True)
                else:
                    self._set_tts_service_button("online", False)
                self._set_tts_status("tts_external_online")
            else:
                has_password = bool(
                    self.tts_autodl_password.text()
                    or self._original.tts.autodl_password_encrypted
                )
                can_start = bool(
                    self.tts_autodl_ssh_command.text().strip()
                    and self.tts_autodl_remote_command.text().strip()
                    and has_password
                )
                self._set_tts_service_button("start", can_start)
                self._set_tts_status("tts_external_offline")
            return

        if self.tts_advanced_paths.isChecked():
            configured_engine = self.tts_engine_root.text().strip()
            if state.engine_root is not None and not configured_engine:
                self.tts_engine_root.setText(str(state.engine_root))
            if (
                state.model_directory is not None
                and not self.tts_model_dir.text().strip()
            ):
                self.tts_model_dir.setText(str(state.model_directory))

        self._tts_engine_download_needed = (
            self._platform_runtime.capabilities.managed_archives
            and not reachable
            and not state.engine_ready
        )
        if (
            self._platform_runtime.capabilities.managed_tts_bootstrap
            and not state.engine_ready
        ):
            self.tts_bootstrap_button.setEnabled(True)
        if reachable:
            if get_tts_service_manager().owns_running_process():
                self._set_tts_service_button("stop", True)
            else:
                self._set_tts_service_button("online", False)
        else:
            can_start = (
                state.engine_ready
                and state.model_ready
                and state.reference_voices_ready
            )
            self._set_tts_service_button("start", can_start)
        if not state.reference_voices_ready:
            self._set_tts_status("tts_references_missing")
            self.tts_download_button.setEnabled(True)
            if not self._tts_download_prompted:
                self._tts_download_prompted = True
                self._request_tts_download()
            return
        if state.model_ready:
            if reachable:
                self.tts_download_button.setEnabled(False)
                self._set_tts_status("tts_ready_online")
            elif state.engine_root is None:
                self.tts_download_button.setEnabled(
                    self._platform_runtime.capabilities.managed_archives
                )
                self._set_tts_status("tts_engine_missing")
            elif not state.engine_ready:
                self.tts_download_button.setEnabled(
                    self._platform_runtime.capabilities.managed_archives
                )
                self._set_tts_status("tts_engine_incomplete")
            else:
                self.tts_download_button.setEnabled(False)
                self._set_tts_status("tts_ready_offline")
            if (
                self._tts_engine_download_needed
                and not self._tts_download_prompted
            ):
                self._tts_download_prompted = True
                self._request_tts_download()
            return

        self._set_tts_status("tts_model_missing")
        self.tts_download_button.setEnabled(True)
        if self._tts_download_prompted:
            return
        self._tts_download_prompted = True
        self._request_tts_download()

    def _set_tts_service_button(
        self,
        mode: str,
        enabled: bool,
    ) -> None:
        self._tts_service_button_mode = mode
        self.tts_service_button.setEnabled(enabled)
        self._render_tts_service_button()

    def _render_tts_service_button(self) -> None:
        if not hasattr(self, "tts_service_button"):
            return
        key = {
            "start": "tts_service_start",
            "stop": "tts_service_stop",
            "online": "tts_service_online",
        }.get(self._tts_service_button_mode, "tts_service_start")
        self.tts_service_button.setText(self._text(key))

    def _toggle_tts_service(self) -> None:
        if (
            self._closing
            or not self.tts_enabled.isChecked()
            or (
                self._tts_service_worker is not None
                and self._tts_service_worker.isRunning()
            )
        ):
            return
        try:
            settings = self._current_tts_settings(enabled=True)
        except ValidationError:
            self._set_tts_status("tts_invalid")
            return

        manager = get_emotional_vits_service_manager() if settings.backend == "emotional_vits" else get_tts_service_manager()
        action = (
            "stop"
            if manager.owns_running_process()
            else "start"
        )
        self._tts_service_error = None
        self._set_tts_path_controls(downloading=True)
        self.tts_service_button.setEnabled(False)
        self._set_tts_status(
            "tts_service_stopping"
            if action == "stop"
            else (
                "tts_emotional_vits_starting"
                if settings.backend == "emotional_vits"
                else
                "tts_service_connecting_ssh"
                if settings.uses_autodl()
                else "tts_service_locating"
            )
        )
        worker = TTSServiceWorker(
            settings,
            action,
            self.tts_autodl_password.text(),
            self,
        )
        self._tts_service_worker = worker
        worker.stage_changed.connect(lambda stage: self._on_tts_service_stage(
            stage, checked_settings=worker.settings,
        ))
        worker.succeeded.connect(lambda action: self._on_tts_service_succeeded(
            action, checked_settings=worker.settings,
        ))
        worker.failed.connect(lambda message: self._on_tts_service_failed(
            message, checked_settings=worker.settings,
        ))
        worker.finished.connect(
            lambda: self._finish_tts_service_worker(worker)
        )
        worker.start()

    def _on_tts_service_stage(self, stage: str, *, checked_settings: TTSSettings | None = None) -> None:
        if self._closing or not self._tts_check_matches_current(checked_settings):
            return
        key = {
            "starting_emotional_vits": "tts_emotional_vits_starting",
            "locating_engine": "tts_service_locating",
            "starting_process": "tts_service_starting",
            "connecting_ssh": "tts_service_connecting_ssh",
            "starting_remote": "tts_service_starting_remote",
            "starting_tunnel": "tts_service_starting_tunnel",
            "waiting_for_api": "tts_service_waiting",
            "waiting_for_existing_start": "tts_service_waiting_existing",
            "loading_weights": "tts_service_loading_weights",
        }.get(stage)
        if key is not None:
            self._set_tts_status(key)

    def _on_tts_service_succeeded(self, action: str, *, checked_settings: TTSSettings | None = None) -> None:
        if self._closing or not self._tts_check_matches_current(checked_settings):
            return
        self._set_tts_status(
            "tts_service_stopped"
            if action == "stopped"
            else "tts_service_started"
        )

    def _on_tts_service_failed(self, message: str, *, checked_settings: TTSSettings | None = None) -> None:
        if self._closing or not self._tts_check_matches_current(checked_settings):
            return
        self._tts_service_error = message
        self._set_tts_status("tts_service_failed", message=message)

    def _finish_tts_service_worker(
        self,
        worker: TTSServiceWorker,
    ) -> None:
        if self._tts_service_worker is worker:
            self._tts_service_worker = None
        worker.deleteLater()
        if self._closing:
            return
        if self._tts_service_error is None:
            self._update_tts_state()
        else:
            self._set_tts_path_controls(downloading=False)
            self.tts_service_button.setEnabled(self.tts_enabled.isChecked())

    def _request_tts_download(self) -> None:
        if self._closing or self._uses_emotional_vits():
            return
        if self.tts_voice_profile.text().strip() or not self.tts_enabled.isChecked():
            return
        if self.tts_backend.currentData() == "autodl":
            return
        if self.tts_advanced_paths.isChecked():
            model_destination = self._require_download_directory(
                self.tts_model_dir,
                "tts_model_path_required",
            )
            if model_destination is None:
                return
            engine_destination = None
            if self._tts_engine_download_needed:
                engine_destination = self._require_download_directory(
                    self.tts_engine_root,
                    "tts_engine_path_required",
                )
                if engine_destination is None:
                    return
        else:
            storage_root = self._require_download_directory(
                self.tts_storage_root,
                "tts_storage_path_required",
            )
            if storage_root is None:
                return
            derived_engine, model_destination = tts_paths_for_storage_root(
                storage_root
            )
            engine_destination = (
                derived_engine if self._tts_engine_download_needed else None
            )
            self._update_tts_path_preview()

        if self._tts_engine_download_needed and engine_destination is None:
            engine_destination = self._require_download_directory(
                self.tts_engine_root,
                "tts_engine_path_required",
            )
            if engine_destination is None:
                return
        answer = QMessageBox.question(
            self,
            self._text("tts_download_consent_title"),
            self._text(
                "tts_download_consent_body_with_engine"
                if self._tts_engine_download_needed
                else "tts_download_consent_body"
            ),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if answer != QMessageBox.Yes:
            return
        try:
            self.download_manager.start_tts(
                model_destination,
                include_engine=self._tts_engine_download_needed,
                engine_destination=engine_destination,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            QMessageBox.warning(
                self,
                self._text("download_path_required_title"),
                self._text("tts_download_path_unsafe", message=exc),
            )
            return
        self._set_tts_path_controls(downloading=True)
        self.tts_download_button.setEnabled(False)
        self._render_tts_download(
            self.download_manager.snapshot(TTS_JOB_ID)
        )

    def _request_tts_bootstrap(self) -> None:
        if (
            self._closing
            or
            self._uses_emotional_vits()
            or
            self.tts_voice_profile.text().strip()
            or
            not self.tts_enabled.isChecked()
            or self.tts_backend.currentData() == "autodl"
            or not self._platform_runtime.capabilities.managed_tts_bootstrap
        ):
            return
        if self.tts_advanced_paths.isChecked():
            engine_root = self._require_download_directory(
                self.tts_engine_root,
                "tts_engine_path_required",
            )
        else:
            storage_root = self._require_download_directory(
                self.tts_storage_root,
                "tts_storage_path_required",
            )
            engine_root = (
                tts_paths_for_storage_root(storage_root)[0]
                if storage_root is not None
                else None
            )
        if engine_root is None:
            return
        answer = QMessageBox.question(
            self,
            self._text("tts_bootstrap_consent_title"),
            self._text("tts_bootstrap_consent_body"),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if answer != QMessageBox.Yes:
            return
        worker = TTSBootstrapWorker(
            self._platform_runtime,
            engine_root,
            self,
        )
        self._tts_bootstrap_error = None
        self._tts_bootstrap_worker = worker
        worker.stage_changed.connect(self._on_tts_bootstrap_stage)
        worker.succeeded.connect(self._on_tts_bootstrap_succeeded)
        worker.failed.connect(self._on_tts_bootstrap_failed)
        worker.finished.connect(
            lambda: self._finish_tts_bootstrap_worker(worker)
        )
        self._set_tts_path_controls(downloading=True)
        self._set_tts_status(
            "tts_bootstrap_installing",
            detail=self._text("download_preparing"),
        )
        worker.start()

    def _on_tts_bootstrap_stage(self, detail: str) -> None:
        self._set_tts_status("tts_bootstrap_installing", detail=detail)

    def _on_tts_bootstrap_succeeded(self, path: str) -> None:
        if self.tts_advanced_paths.isChecked():
            self.tts_engine_root.setText(path)
        else:
            self.tts_storage_root.setText(str(Path(path).parent))
            self._update_tts_path_preview()
        self._set_tts_status("tts_bootstrap_installed", path=path)

    def _on_tts_bootstrap_failed(self, message: str) -> None:
        self._tts_bootstrap_error = message
        self._set_tts_status("tts_bootstrap_failed", message=message)

    def _finish_tts_bootstrap_worker(
        self,
        worker: TTSBootstrapWorker,
    ) -> None:
        if self._tts_bootstrap_worker is worker:
            self._tts_bootstrap_worker = None
        worker.deleteLater()
        if not self._closing and self._tts_bootstrap_error is None:
            self._update_tts_state()
        else:
            self._set_tts_path_controls(downloading=False)

    def _finish_tts_check(self, worker: TTSCheckWorker) -> None:
        if self._tts_check_worker is worker:
            self._tts_check_worker = None
        worker.deleteLater()
        if self._closing:
            self._pending_tts_check = False
            return
        if self._pending_tts_check:
            self._pending_tts_check = False
            self._update_tts_state()

    def _render_tts_download(self, snapshot: DownloadSnapshot) -> None:
        if self._uses_emotional_vits() or self.tts_voice_profile.text().strip():
            return
        if snapshot.status == "extracting":
            self.tts_progress.hide()
            self._render_progress(
                self.tts_extract_progress,
                snapshot,
            )
            self._set_tts_status(
                "tts_extracting",
                detail=self._extraction_detail(snapshot),
            )
            return
        if snapshot.status in {"installing", "cleaning"}:
            self.tts_progress.hide()
            self._render_progress(
                self.tts_extract_progress,
                snapshot,
            )
            key = (
                "tts_installing"
                if snapshot.status == "installing"
                else "tts_cleaning"
            )
            self._set_tts_status(
                key,
                detail=self._installation_detail(snapshot),
            )
            return
        self.tts_extract_progress.hide()
        self._render_progress(self.tts_progress, snapshot)
        if snapshot.status == "preparing":
            self._set_tts_status(
                "tts_preparing",
                detail=self._download_detail(snapshot),
            )
            return
        if snapshot.status == "checking":
            self._set_tts_status(
                "tts_checking_files",
                detail=self._download_detail(snapshot),
            )
            return
        self._set_tts_status(
            "tts_downloading",
            detail=self._download_detail(snapshot),
        )

    def _on_download_changed(
        self,
        job_id: str,
        snapshot: DownloadSnapshot,
    ) -> None:
        if job_id == TTS_JOB_ID:
            if self._uses_emotional_vits() or self.tts_voice_profile.text().strip():
                return
            if snapshot.status in {
                "preparing",
                "checking",
                "downloading",
                "extracting",
                "installing",
                "cleaning",
            }:
                self._set_tts_path_controls(downloading=True)
                self._render_tts_download(snapshot)
            elif snapshot.status == "completed":
                self.tts_download_button.setEnabled(False)
                if self.tts_advanced_paths.isChecked():
                    self.tts_model_dir.setText(snapshot.destination)
                self._render_progress(self.tts_progress, snapshot)
                self.tts_extract_progress.hide()
                self._set_tts_status(
                    "tts_downloaded",
                    path=snapshot.destination,
                )
                self._update_tts_state()
            elif snapshot.status == "failed":
                self._set_tts_path_controls(downloading=False)
                self.tts_download_button.setEnabled(
                    self.tts_enabled.isChecked()
                )
                self._render_progress(self.tts_progress, snapshot)
                self.tts_extract_progress.hide()
                self._set_tts_status(
                    "tts_download_failed",
                    message=snapshot.message,
                )
            return

        model_name = self.stt_model.currentText().strip()
        if not model_name:
            return
        expected = whisper_job_id(model_repository(model_name))
        if job_id != expected:
            return
        if snapshot.status in {
            "preparing",
            "checking",
            "downloading",
        }:
            self._render_whisper_download(model_name, snapshot)
        elif snapshot.status == "completed":
            self.stt_model.setEnabled(self.stt_enabled.isChecked())
            self.whisper_model_dir.setEnabled(
                self.stt_enabled.isChecked()
            )
            self.whisper_model_browse.setEnabled(
                self.stt_enabled.isChecked()
            )
            self._render_progress(self.whisper_progress, snapshot)
            self._set_whisper_status(
                "whisper_downloaded",
                path=snapshot.destination,
            )
        elif snapshot.status == "failed":
            self.stt_model.setEnabled(self.stt_enabled.isChecked())
            self.whisper_model_dir.setEnabled(
                self.stt_enabled.isChecked()
            )
            self.whisper_model_browse.setEnabled(
                self.stt_enabled.isChecked()
            )
            self.whisper_download_button.setEnabled(
                self.stt_enabled.isChecked()
            )
            self._render_progress(self.whisper_progress, snapshot)
            self._set_whisper_status(
                "whisper_download_failed",
                message=snapshot.message,
            )

    @staticmethod
    def _render_progress(
        progress: QProgressBar,
        snapshot: DownloadSnapshot,
    ) -> None:
        active = snapshot.status in {
            "preparing",
            "checking",
            "downloading",
            "extracting",
            "installing",
            "cleaning",
        }
        should_show = active or snapshot.status == "failed"
        progress.setVisible(should_show)
        if not should_show:
            return
        if snapshot.total <= 0:
            progress.setRange(0, 0)
            return
        progress.setRange(0, 1_000)
        value = min(1_000, int(snapshot.received / snapshot.total * 1_000))
        progress.setValue(value)

    def _download_detail(self, snapshot: DownloadSnapshot) -> str:
        if snapshot.total <= 0:
            return self._text("download_preparing")
        percent = snapshot.received / snapshot.total * 100
        detail = (
            f"{percent:.1f}% · {self._format_bytes(snapshot.received)} / "
            f"{self._format_bytes(snapshot.total)}"
        )
        if snapshot.status == "downloading":
            detail += f" · {self._format_bytes(snapshot.speed_bps)}/s"
        if snapshot.current_file:
            detail += f" · {Path(snapshot.current_file).name}"
        return detail

    def _extraction_detail(self, snapshot: DownloadSnapshot) -> str:
        if snapshot.total <= 0:
            return "preparing"
        percent = snapshot.received / snapshot.total * 100
        detail = (
            f"{percent:.1f}% · {snapshot.received} / "
            f"{snapshot.total} {self._text('download_files')}"
        )
        if snapshot.current_file:
            detail += f" · {Path(snapshot.current_file).name}"
        return detail

    def _installation_detail(self, snapshot: DownloadSnapshot) -> str:
        if snapshot.total <= 0:
            return (
                Path(snapshot.current_file).name
                if snapshot.current_file
                else self._text("download_preparing")
            )
        percent = snapshot.received / snapshot.total * 100
        detail = (
            f"{percent:.1f}% · {snapshot.received} / "
            f"{snapshot.total} {self._text('download_steps')}"
        )
        if snapshot.current_file:
            detail += f" · {Path(snapshot.current_file).name}"
        return detail

    @staticmethod
    def _format_bytes(value: int | float) -> str:
        size = float(value)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if size < 1024 or unit == "TB":
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"

    def _browse_tts_storage(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self,
            self._text("tts_storage_root"),
            self.tts_storage_root.text().strip() or str(Path.home()),
        )
        if selected:
            self.tts_storage_root.setText(selected)
            self._on_tts_storage_root_changed()

    def _browse_emotional_vits_profile(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            self._text("tts_emotional_vits_profile"),
            self.tts_emotional_vits_profile.text().strip(),
            "JSON (*.json)",
        )
        if path:
            self.tts_emotional_vits_profile.setText(path)
            self._update_tts_state()

    def _open_emotion_audition(self) -> None:
        if self._closing:
            return
        if self._emotion_audition_dialog is not None:
            self._emotion_audition_dialog.raise_()
            self._emotion_audition_dialog.activateWindow()
            return
        dialog = EmotionalVITSAuditionDialog(language=self._language(), parent=self)
        self._emotion_audition_dialog = dialog
        dialog.profile_saved.connect(self._on_emotion_audition_saved)
        dialog.finished.connect(lambda _result: self._finish_emotion_audition(dialog))
        dialog.open()

    def _on_emotion_audition_saved(self, path: str) -> None:
        if self._closing:
            return
        self.tts_emotional_vits_profile.setText(path)
        self.tts_emotional_vits_audition_result.setText(
            self._text("tts_emotional_vits_audition_saved", path=path)
        )
        self.tts_emotional_vits_audition_result.show()
        self._update_tts_state()

    def _finish_emotion_audition(self, dialog) -> None:
        if self._emotion_audition_dialog is dialog:
            self._emotion_audition_dialog = None
        dialog.deleteLater()

    def _browse_tts_engine(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self,
            self._text("tts_engine_root"),
            self.tts_engine_root.text().strip() or str(Path.home()),
        )
        if selected:
            self.tts_engine_root.setText(selected)
            self._update_tts_state()

    def _browse_tts_model(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self,
            self._text("tts_model_dir"),
            self.tts_model_dir.text().strip() or str(Path.home()),
        )
        if selected:
            self.tts_model_dir.setText(selected)
            self._update_tts_state()

    def _browse_whisper_model(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self,
            self._text("whisper_model_dir"),
            self.whisper_model_dir.text().strip() or str(Path.home()),
        )
        if selected:
            self.whisper_model_dir.setText(selected)
            self._update_whisper_state()

    def _require_download_directory(
        self,
        field: QLineEdit,
        missing_key: str,
    ) -> Path | None:
        value = field.text().strip()
        if not value:
            QMessageBox.warning(
                self,
                self._text("download_path_required_title"),
                self._text(missing_key),
            )
            return None
        directory = Path(value).expanduser()
        try:
            directory.mkdir(parents=True, exist_ok=True)
            if not directory.is_dir():
                raise NotADirectoryError(str(directory))
            resolved = directory.resolve()
        except OSError as exc:
            QMessageBox.warning(
                self,
                self._text("download_path_required_title"),
                self._text("download_path_invalid", message=exc),
            )
            return None
        field.setText(str(resolved))
        return resolved

    def _update_backend_visibility(self) -> None:
        if not hasattr(self, "backend_stack"):
            return
        mode = self.mode_combo.currentData()
        self.backend_stack.setCurrentIndex(0 if mode == "ollama" else 1)
        self._update_vision_compatibility()

    def _update_deepseek_thinking_state(self) -> None:
        enabled = self.deepseek_thinking.isChecked()
        self.deepseek_thinking_max_tokens.setEnabled(enabled)
        self._set_form_labels_enabled(
            ("deepseek_thinking_max_tokens",), enabled
        )

    def _update_api_provider(self) -> None:
        if not hasattr(self, "api_provider_stack"):
            return
        provider = self.api_provider.currentData()
        provider_index = {
            "deepseek": 0,
            "aliyun": 1,
            "openai": 2,
        }
        self.api_provider_stack.setCurrentIndex(
            provider_index.get(provider, 0)
        )

    def _update_vision_compatibility(self) -> None:
        if not hasattr(self, "vision_provider_stack"):
            return
        provider_index = {
            "ollama": 0,
            "aliyun": 1,
            "openai": 2,
        }
        self.vision_provider_stack.setCurrentIndex(
            provider_index.get(self.vision_provider.currentData(), 0)
        )
        self.vision_compatibility.setText(
            self._text("vision_supported")
        )

    def _update_vision_state(self, enabled: bool) -> None:
        if not hasattr(self, "vision_options"):
            return
        self.vision_options.setEnabled(enabled)
        if not enabled:
            self._model_fetch_queue = [
                vision
                for vision in self._model_fetch_queue
                if not vision
            ]
        self._update_model_fetch_buttons()

    def _update_model_fetch_buttons(self) -> None:
        if not hasattr(self, "fetch_models_button"):
            return
        idle = self._model_worker is None
        self.fetch_models_button.setEnabled(idle)
        self.fetch_vision_models_button.setEnabled(
            idle and self.vision_enabled.isChecked()
        )

    def _import_prompt(self) -> None:
        file_name, _ = QFileDialog.getOpenFileName(
            self,
            self._text("import_title"),
            str(Path.home()),
            self._text("text_filter"),
        )
        if not file_name:
            return
        try:
            self.personality_prompt.setPlainText(
                Path(file_name).read_text(encoding="utf-8")
            )
        except OSError as exc:
            QMessageBox.warning(
                self,
                self._text("import_failed"),
                str(exc),
            )

    def _confirm_clear_history(self) -> None:
        if self._first_run:
            return
        answer = QMessageBox.question(
            self,
            self._text("clear_history_confirm_title"),
            self._text("clear_history_confirm_body"),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self.clear_history_requested.emit()
        self._set_status("history_cleared")

    def _confirm_clear_cache(self) -> None:
        answer = QMessageBox.question(
            self,
            self._text("clear_cache_confirm_title"),
            self._text("clear_cache_confirm_body"),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return

        result = clear_runtime_cache()
        values = {
            "files": result.removed_files,
            "size": self._format_byte_count(result.removed_bytes),
        }
        if result.failed_paths:
            self._set_status(
                "cache_clear_partial",
                failed=len(result.failed_paths),
                **values,
            )
        elif result.removed_files:
            self._set_status("cache_cleared", **values)
        else:
            self._set_status("cache_empty")

    @staticmethod
    def _format_byte_count(byte_count: int) -> str:
        size = float(max(0, byte_count))
        for unit in ("B", "KiB", "MiB", "GiB"):
            if size < 1024 or unit == "GiB":
                if unit == "B":
                    return f"{int(size)} {unit}"
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} GiB"

    def _form_settings(self) -> AppSettings:
        personality_path = (
            get_user_data_dir() / "personality.zh-CN.txt"
        )
        personality_path_en = get_user_data_dir() / "personality.en.txt"
        screen_index = self.screen_index.value()
        original_display = self._original.display
        return AppSettings(
            ui_language=self._language(),
            mode=self.mode_combo.currentData(),
            ollama=OllamaSettings(
                base_url=self.ollama_url.text().strip(),
                chat_model=self.ollama_chat_model.currentText().strip(),
                context_window=self.ollama_context_window.value(),
                timeout_seconds=self.ollama_timeout.value(),
                keep_alive=self.ollama_keep_alive.text().strip(),
            ),
            api=APISettings(
                provider=self.api_provider.currentData(),
                deepseek_api_key=self.deepseek_key.text().strip(),
                aliyun_api_key=self.aliyun_key.text().strip(),
                openai_api_key=self.openai_key.text().strip(),
                deepseek_base_url=self.deepseek_url.text().strip(),
                aliyun_base_url=self.aliyun_url.text().strip(),
                openai_base_url=self.openai_url.text().strip(),
                deepseek_chat_model=(
                    self.deepseek_chat_model.currentText().strip()
                ),
                deepseek_thinking=self.deepseek_thinking.isChecked(),
                deepseek_thinking_max_tokens=(
                    self.deepseek_thinking_max_tokens.value()
                ),
                aliyun_chat_model=(
                    self.aliyun_chat_model.currentText().strip()
                ),
                openai_chat_model=(
                    self.openai_chat_model.currentText().strip()
                ),
                timeout_seconds=self.api_timeout.value(),
            ),
            vision=VisionSettings(
                enabled=self.vision_enabled.isChecked(),
                interval_seconds=self.vision_interval.value(),
                provider=self.vision_provider.currentData(),
                ollama_base_url=self.vision_ollama_url.text().strip(),
                ollama_model=(
                    self.vision_ollama_model.currentText().strip()
                ),
                ollama_context_window=(
                    self.vision_ollama_context_window.value()
                ),
                ollama_keep_alive=(
                    self.vision_ollama_keep_alive.text().strip()
                ),
                aliyun_api_key=self.vision_aliyun_key.text().strip(),
                aliyun_base_url=self.vision_aliyun_url.text().strip(),
                aliyun_model=(
                    self.vision_aliyun_model.currentText().strip()
                ),
                openai_api_key=self.vision_openai_key.text().strip(),
                openai_base_url=self.vision_openai_url.text().strip(),
                openai_model=(
                    self.vision_openai_model.currentText().strip()
                ),
                timeout_seconds=self.vision_timeout.value(),
            ),
            tts=self._current_tts_settings(),
            stt=STTSettings(
                enabled=self.stt_enabled.isChecked(),
                model=self.stt_model.currentText().strip(),
                model_dir=self.whisper_model_dir.text().strip(),
                device=self.stt_device.currentData(),
                input_device=self.stt_input_device.currentData() or "",
                language=self._stt_language_value(),
            ),
            character=CharacterSettings(
                profile_id=self._original.character.profile_id,
                name=self._original.character.name,
                name_en=self._original.character.name_en,
                user_name=self.user_name.text().strip(),
                portrait=(self._original.character.portrait if self._external_character_controls() else self.portrait.currentData()),
                outfit=(self._original.character.outfit if self._external_character_controls() else self.outfit.currentData()),
                personality_file=str(personality_path),
                personality_file_en=str(personality_path_en),
            ),
            display=DisplaySettings(
                screen_index=screen_index,
                screen_name=(
                    original_display.screen_name
                    if screen_index == original_display.screen_index
                    else ""
                ),
                window_x=original_display.window_x,
                window_y=original_display.window_y,
                portrait_screen_ratio=self.portrait_ratio.value(),
                show_log_console=self.show_log_console.isChecked(),
            ),
            idle=IdleSettings(
                do_not_disturb=self.do_not_disturb.isChecked(),
                thinking_minutes=self.thinking_minutes.value(),
                away_minutes=self.away_minutes.value(),
            ),
            interaction=InteractionSettings.model_validate({
                **self._original.interaction.model_dump(),
                "response_target_seconds": self._response_target_value(self.response_target),
                "important_response_target_seconds": self._response_target_value(
                    self.important_response_target
                ),
            }),
            history_limit=self.history_limit.value(),
        )

    def _fetch_models(self) -> None:
        self._start_model_fetch(vision=False)

    def _fetch_vision_models(self) -> None:
        if not self.vision_enabled.isChecked():
            return
        self._start_model_fetch(vision=True)

    def _auto_fetch_models(self) -> None:
        if self._closing:
            return
        try:
            settings = self._form_settings()
        except ValidationError:
            return

        scopes: list[bool] = []
        if (
            settings.mode == "ollama"
            or bool(settings.api.selected_api_key())
        ):
            scopes.append(False)
        if settings.vision.enabled and (
            settings.vision.provider == "ollama"
            or bool(settings.vision.selected_api_key())
        ):
            scopes.append(True)
        self._model_fetch_queue = scopes
        self._start_next_model_fetch()

    def _start_next_model_fetch(self) -> None:
        if (
            self._closing
            or self._model_worker is not None
            or not self._model_fetch_queue
        ):
            return
        self._start_model_fetch(
            vision=self._model_fetch_queue.pop(0),
            notify_if_busy=False,
        )

    def _start_model_fetch(
        self,
        *,
        vision: bool,
        notify_if_busy: bool = True,
    ) -> None:
        if vision and not self.vision_enabled.isChecked():
            return
        if self._model_worker is not None:
            if notify_if_busy:
                self._show_running_message()
            return
        try:
            settings = self._form_settings()
        except ValidationError as exc:
            QMessageBox.warning(
                self,
                self._text("invalid_settings"),
                str(exc),
            )
            return

        self.fetch_models_button.setEnabled(False)
        self.fetch_vision_models_button.setEnabled(False)
        self._set_status("connecting")
        self._model_target = (
            "vision" if vision else "chat",
            (
                settings.vision.provider
                if vision
                else settings.mode
            ),
            (
                settings.vision.provider
                if vision
                else settings.api.provider
            ),
        )
        worker = ModelListWorker(
            settings,
            vision=vision,
            parent=self,
        )
        self._model_worker = worker
        worker.models_ready.connect(self._on_models_ready)
        worker.error.connect(self._on_models_error)
        worker.finished.connect(lambda: self._finish_model_worker(worker))
        worker.start()

    def _finish_model_worker(self, worker: ModelListWorker) -> None:
        if self._model_worker is worker:
            self._model_worker = None
        worker.deleteLater()
        if self._model_fetch_queue and not self._closing:
            QTimer.singleShot(0, self._start_next_model_fetch)
            return
        self._update_model_fetch_buttons()

    def _on_models_ready(self, models: list[str]) -> None:
        if not models:
            self._set_status("models_empty")
            return

        scope, mode, provider = self._model_target or (
            "chat",
            self.mode_combo.currentData(),
            self.api_provider.currentData(),
        )
        targets: list[QComboBox]
        if scope == "vision":
            if provider == "ollama":
                targets = [self.vision_ollama_model]
            elif provider == "aliyun":
                targets = [self.vision_aliyun_model]
            else:
                targets = [self.vision_openai_model]
        elif mode == "ollama":
            targets = [self.ollama_chat_model]
        elif provider == "aliyun":
            targets = [self.aliyun_chat_model]
        elif provider == "openai":
            targets = [self.openai_chat_model]
        else:
            targets = [self.deepseek_chat_model]

        unique_models = list(dict.fromkeys(models))
        for combo in targets:
            selected = combo.currentText().strip()
            choices = list(unique_models)
            if selected and selected not in choices:
                choices.insert(0, selected)
            combo.clear()
            combo.addItems(choices)
            if selected:
                combo.setCurrentText(selected)
        self._set_status("models_found", count=len(unique_models))

    def _on_models_error(self, message: str) -> None:
        self._set_status("connection_failed", message=message)

    def _show_running_message(self) -> None:
        QMessageBox.information(
            self,
            self._text("test_running_title"),
            self._text("test_running_body"),
        )

    def accept(self) -> None:
        if self._tts_bootstrap_is_running():
            self._show_running_message()
            return
        self._stash_current_personality()
        missing_language = next(
            (
                language
                for language in ("en", "zh-CN")
                if not self._personality_prompts.get(language, "").strip()
            ),
            None,
        )
        if missing_language is not None:
            self._set_combo_data(self.language_combo, missing_language)
            QMessageBox.warning(
                self,
                self._text("missing_personality"),
                self._text("missing_personality_body"),
            )
            return
        try:
            settings = self._form_settings()
        except ValidationError as exc:
            QMessageBox.warning(
                self,
                self._text("invalid_settings"),
                str(exc),
            )
            return
        if (
            settings.requires_api_key()
            or settings.requires_vision_api_key()
        ):
            QMessageBox.warning(
                self,
                self._text("missing_key"),
                self._text("missing_key_body"),
            )
            return
        if settings.tts.enabled and settings.tts.uses_autodl():
            has_password = bool(
                self.tts_autodl_password.text()
                or settings.tts.autodl_password_encrypted
            )
            if not all(
                (
                    settings.tts.autodl_ssh_command.strip(),
                    settings.tts.autodl_remote_command.strip(),
                    settings.tts.autodl_remote_reference_root.strip(),
                    has_password,
                )
            ):
                QMessageBox.warning(
                    self,
                    self._text("invalid_settings"),
                    self._text("tts_autodl_missing"),
                )
                return
            if self.tts_autodl_password.text():
                try:
                    settings.tts.autodl_password_encrypted = (
                        self._platform_runtime.credentials.protect(
                            self.tts_autodl_password.text()
                        )
                    )
                except CredentialError as exc:
                    QMessageBox.warning(
                        self,
                        self._text("invalid_settings"),
                        self._text(
                            "tts_password_store_failed",
                            message=exc,
                        ),
                    )
                    return

        try:
            for language in ("en", "zh-CN"):
                personality_path = settings.personality_path(language)
                personality_path.parent.mkdir(parents=True, exist_ok=True)
                personality_path.write_text(
                    self._personality_prompts[language].strip() + "\n",
                    encoding="utf-8",
                )
        except OSError as exc:
            QMessageBox.warning(
                self,
                self._text("save_failed"),
                str(exc),
            )
            return

        self._result = settings
        self._closing = True
        super().accept()

    def reject(self) -> None:
        if self._background_check_is_running():
            self._show_running_message()
            return
        super().reject()

    def closeEvent(self, event) -> None:
        if self._background_check_is_running():
            event.ignore()
            self._show_running_message()
            return
        super().closeEvent(event)

    def _background_check_is_running(self) -> bool:
        return bool(
            (
                self._emotion_audition_dialog is not None
                and self._emotion_audition_dialog.is_busy()
            )
            or
            (
                self._model_worker is not None
                and self._model_worker.isRunning()
            )
            or (
                self._whisper_check_worker is not None
                and self._whisper_check_worker.isRunning()
            )
            or (
                self._tts_check_worker is not None
                and self._tts_check_worker.isRunning()
            )
            or (
                self._tts_service_worker is not None
                and self._tts_service_worker.isRunning()
            )
            or self._tts_bootstrap_is_running()
        )

    def _tts_bootstrap_is_running(self) -> bool:
        return bool(
            self._tts_bootstrap_worker is not None
            and self._tts_bootstrap_worker.isRunning()
        )

    def result_settings(self) -> AppSettings:
        if self._result is None:
            return self._original.model_copy(deep=True)
        return self._result.model_copy(deep=True)

