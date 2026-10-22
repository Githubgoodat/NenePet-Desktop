# 本地素材、外部程序与默认路径

仓库只发布代码、提示词、六个参数动作 JSON 和单独附许可的字体。**克隆仓库不等于已经获得或配置了角色桌宠。** 不要向公开仓库上传游戏文件、模型、截图、参考音频、训练权重、Steam 文件或自己的设置。

## 两种运行布局

本文使用两个根目录变量：

| 模式 | 资源根 `R` | 可写根 `B` |
| --- | --- | --- |
| `python packaging/share_main.py` 源码运行 | 仓库根目录 | 仓库根目录下 `data/share-development` |
| PyInstaller onedir 构建 | exe 目录下 `_internal` | exe 所在目录 |

主程序不会自动将这两个根目录混用。以下路径中的 `R/`、`B/` 是说明符号，不是实际需要创建的文件夹名称。

| 项目 | 路径或默认值 | 仓库状态 |
| --- | --- | --- |
| 角色设定 | `R/prompt.nene.txt` | 已提供 |
| 通用上游设定 | `R/prompt.txt`、`R/prompt.en.txt` | 已提供，分享入口默认选宁宁设定 |
| 六种基础情绪动作 | `R/assets/motions/{calm,happy,shy,angry,surprised,worried}.motion3.json` | 已提供，只是参数动作，不是人物模型 |
| 字体 | `R/思源黑体Bold.otf` | 附独立许可；缺失时回退系统字体 |
| 模型入口 | `R/assets/models/nene-editable/nene.model3.json` | 未提供，需自行准备合法素材 |
| 场景目录 | `R/assets/persona/nene-scenes.json` | 未提供 |
| 场景图像与记录 | `R/voice-sources/` 内由场景目录引用的截图和 metadata JSON | 未提供 |
| 公开构建代码的基准参考音频 | `R/voice-sources/c2-2-nene-01-clause.wav` 与同名 `.txt` | 未提供；名称只是历史兼容约定，不授予任何游戏声音使用权 |
| 可选语音配置 | `B/voices/nene-cpu.json`、`B/voices/nene-cuda.json` | 未提供，需按自己的素材生成 |
| 可选语音引擎 | `B/engine/GPT-SoVITS/` | 未提供 |
| 用户配置 | `B/data/profile/config.json` | 用户首次确认设置后创建，包含本机 API Key |
| Viewer 选择及校准 | `B/data/profile/share-viewer.json` | 用户本机创建 |
| 单实例锁 | `B/data/profile/nene.lock` | 运行时使用 |
| 历史、关系状态及日志 | `B/data/profile/` 及其子目录 | 只属于本机用户，禁止上传 |
| 缓存 | `B/data/cache/` | 本机运行时产生 |
| 默认模型下载位置 | 分享入口设置 `AIPET_MODEL_DIR=R/models` | 未提供；本分享入口不提供 STT，勿把此目录当作已安装模型 |
| Viewer 端口文件 | 用户选择的 Viewer 安装目录下 `bin/app_port` | 由已安装的 Viewer 维护，不从本仓库复制 |

源码运行的配置路径因此是 `仓库/data/share-development/data/profile/config.json`，不是上游的用户目录，也不是其他已经部署的桌宠配置。

## Live2D 模型

需要有权使用、可以被 Live2DViewerEX 读取的标准 Version 3 导出模型。当前最小兼容文件布局是以下 **7 个文件**：

```text
R/assets/models/nene-editable/
  nene.model3.json
  nene.moc3
  nene.physics3.json
  nene.cdi3.json
  textures/texture_00.png
  motions/idle.motion3.json
  motions/tap.motion3.json
```

文件名与 `nene.model3.json` 的 `FileReferences` 必须一致；仅把不相干文件重命名不能获得兼容模型。引用更多纹理、表情、物理或动作时，也必须补齐全部引用资源。程序读取导出模型，不能直接使用 `.cmo3` 编辑工程。

当前动作层使用特定的眼睛、嘴、头部等参数；身体点击依赖模型中的点击动作/事件。任意来源的模型不保证拥有相同参数或点击事件。请在有权编辑的模型里核对这些接口，并保留通过托盘输入的备用入口。

安装并拥有可用的 Live2DViewerEX，启用桌面模式和 ExAPI；首次设置会自动查找 Steam 库，也可手动选择含 `launcher.exe` 和 `bin` 的安装目录。代码只自动加载第二槽（槽号 `1`）；请先保留该槽原有模型。默认不改人物大小和位置，需要手动摆位并校准身体点击范围。ExAPI 坐标与 Qt 身体范围的原点不同，不能直接互填。

## 原作场景证据

没有 `assets/persona/nene-scenes.json`，或目录引用的截图、metadata 与 SHA-256 不匹配时，`scene_context()` 返回空依据；普通对话仍可使用人物提示词，**不会补造截图或原作台词**。

若自行准备有权使用的场景材料，需要按 `aipet/core/nene_persona.py` 的 schema 放置 catalog、截图与 metadata。每条依据必须有明确的观察、反应前因、后续转折、适用关系阶段和推断边界，记录真实文件的哈希；不能把 ASR 草稿标成已核实日语原文。

当前完整包的严格自检预期 **12 张独立截图及相应 metadata**。这些截图和原 catalog 均未在本仓库发布。只补一个 JSON、把别的图片改名，或将自己的材料写成原始证据，都不能通过有效核对。素材不足时保留检索降级即可；完整包专用的严格自检仍会报告缺失。

## 可选日语语音

**可以一直关闭 TTS，使用文字对话和阅读工具。** 开启语音需要同时准备独立 GPT-SoVITS 环境、匹配的通用基座权重、自己有权使用的参考音频及准确参考文字。仓库不提供这些资源，不会通过安装主程序 Python 依赖自动获得它们。

当前路径约定为：

```text
B/
  engine/GPT-SoVITS/
    api_v2.py
    runtime/python.exe
    GPT_SoVITS/pretrained_models/
      s1v3.ckpt
      v2Pro/s2Gv2Pro.pth
      chinese-roberta-wwm-ext-large/...
      chinese-hubert-base/...
      sv/...
      fast_langdetect/...
    ...其他完整运行依赖
  voices/
    nene-cpu.json
    nene-cuda.json
```

这张目录图不是完整引擎清单；`packaging/build_voice_bundle.py` 的固定清单和 `aipet/core/tts_assets.py` 会核对更多文件。不要仅下载两个权重就认为引擎完整。引擎的 Python 与主程序 Python 独立；源码主程序按 Python 3.13 运行，引擎使用其发行版自带的兼容运行环境。

`nene-cpu.json` / `nene-cuda.json` 使用 `schema_version=1`、`version=v2Pro`。CPU 配置必须是 `device=cpu`、`is_half=false`；CUDA 配置需要兼容显卡与驱动。权重路径和参考音频路径相对于该配置文件解析，参考文字也必须是实际存在的 UTF-8 文件。`baseline_reference` 必须指向你有权使用的 **3–10 秒**参考音频及匹配文字；只有通用基准参考并不代表六种情绪分别得到校准。

现有构建器为历史兼容使用 `c2-2-nene-01-clause.wav/.txt` 这一名称。你必须自行提供合法素材，或在自己的构建副本里一致修改引用；不能据此向仓库索取、抓取或重新分发游戏原声。模型、声音与配音演员的权利不由代码许可证覆盖。

正常本地语音服务使用 `127.0.0.1:9881`。独立 `--share-voice-check` 验收使用 `127.0.0.1:19883`，只在明确执行时启动自己拥有的 CPU 引擎并生成 WAV，不播放声音、不连接 Viewer 或对话 API，不复用已经占用的验收端口。报告路径必须是引擎、资源和正式 profile 外的一个新文件。该验收当前沿用上面的基准参考文件约定，没有参考素材时不能运行。

## API、隐私与退出

- 对话 API 必须使用自己的 Key、地址、模型与余额。不要提交 Key、`.env`、完整请求日志或使用后的 `data` 目录。
- 手动触发复制文本和共同阅读会把选中的资料摘录发给配置的对话服务；只提供自己愿意发给该服务的内容。
- PDF 导入不执行 OCR。手动框选或读取剪贴板图片会将所选图片发送给配置的视觉服务，识别文字先放入编辑区，供核对后再提问；这一步与自动屏幕观察不同，不会自动把识别结果发送到普通聊天。
- 普通聊天可能持久保存到本机 profile；阅读窗口中的临时材料与普通聊天不是同一个存储用途。公开仓库不包含任何真实用户的历史。
- 关闭桌宠与关闭 Live2DViewerEX 是两项独立操作；分别退出。不要为排查问题删除 Steam 扩展、游戏文件或 Viewer 资源。
- `.gitignore` 默认忽略 JSON 配置、游戏图像/音频、模型权重和归档。它不能撤回已经提交的文件；上传前仍须检查待提交内容。

## 构建前仍需补齐的内容

`packaging/build_share.py` 保留完整本地构建的资源检查，因此需要上述导出模型、完整 catalog 及其引用文件、合法基准参考、字体，以及本仓库的 README、路径说明和启动器测试。它不会自动补齐任何第三方角色资源。构建依赖还包括 PyInstaller、pypdf 和对应的主程序依赖，许可汇总需单独核对。

如果只准备了运行所需的合法模型而没有截图或声音，可以从源码运行并关闭 TTS；场景依据检索会降级。不要将“源码可以导入”或“单元测试通过”表述成完整桌宠、所有显卡或情绪音色已经验收。
