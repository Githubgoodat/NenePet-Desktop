# NenePet Desktop

基于 [AIpet-Murasame](https://github.com/kuxiaowo/AIpet-Murasame) 改造的 Windows 桌面角色陪伴程序。它通过 Live2DViewerEX 显示人物，通过用户自己的对话 API 生成回答；可选的 GPT-SoVITS 独立进程负责日语语音。

**代码基础与灵感来源：** [kuxiaowo/AIpet-Murasame](https://github.com/kuxiaowo/AIpet-Murasame)。本项目直接沿用并修改其源码与交互架构，感谢上游作者及贡献者；不是从零独立实现。

**Live2D 模型作者：** [07nene](https://steamcommunity.com/profiles/76561199513188793/myworkshopfiles?appid=616720)，作品[《宁宁live2d》](https://steamcommunity.com/sharedfiles/filedetails/?id=3578664864)。本地使用的可编辑工程和运行模型由这份模型恢复、适配而来，原模型作者署名保留。模型本体及工程未在本仓库公开。完整来源与许可边界见 [CREDITS.md](CREDITS.md)。

**这是源码仓库，不是开箱即用的角色整合包。** 本仓库不包含游戏立绘、Live2D 模型、游戏截图、参考语音、模型权重、GPT-SoVITS 运行环境或 Live2DViewerEX。下载源码后，需要先按 [本地素材说明](docs/LOCAL_ASSETS.md) 配置有权使用的素材及外部程序，才能显示完整桌宠。只需文字对话时，可以关闭 TTS，但仍须配置角色显示所需的模型和 Viewer。

## 包含的功能

- 点击人物输入，支持中文输入法；系统托盘也可打开输入入口。
- 人物表情、字幕和音频播放进度联动；模型位置与点击范围需在本机校准。
- `Ctrl+Alt+N` 随手问：明确触发后读取复制的文字，进行解释、翻译或提炼。
- 共同阅读 TXT、Markdown 和 PDF，可查看回答引用的原文片段。PDF 导入只读取文字层；扫描页可按需使用“框选屏幕”或“识别剪贴板图片”，需单独配置可用的视觉模型，并核对识别结果。
- 文字优先、停止、重试、复制与按需朗读；免打扰暂停互动。
- AI 任务跟进面板：接收 Codex 生命周期事件、独立保留同步或异步提问记录，以及 Grok Bot 显式协作上报。提问不会被后续运行状态覆盖，异步提问不代表任务暂停；两种来源都要先接入，见 [工作流说明](docs/WORKFLOW.md)。
- 可选微信直连提醒：扫码绑定后向本人发送项目名和固定任务状态，用宁宁语气提示，不经公众号、不调用额外对话模型。需要本人先发消息建立上下文，并实测手机是否收到。
- 同一台 iPhone 可启用 Bark 备用通知：微信已暂停或明确拒绝时接手，显示项目名与状态；设备密钥在本机加密保存。微信结果不明时不自动双发。Codex 授权提示先观察 30 秒，同轮具体提问可将两项事实合并提醒；不同问题和新轮次仍单独通知。
- 可选[微信问答通道](docs/WEIXIN_QUESTIONS.md)：执行者主动注册普通问题，你在微信回复“回复 问题编号 答案”，由原等待进程取答后继续。问题有明确有效期，单独短期加密保存；已有原生提问、操作审批和超时后自动唤醒不在这条通道内。
- Windows 可从托盘勾选[开机启动](docs/AUTOSTART.md)，在当前用户登录后恢复已有启动流程；可以随时关闭，不需要管理员权限，不修改其他启动项。
- 用户自行选择对话服务及 API Key。服务可用性、模型回复质量和费用取决于对应服务商及自己的账号。

如果只需要任务提醒与手机问答，可使用独立项目 [WorkflowBridge](https://github.com/Githubgoodat/WorkflowBridge)，无需运行桌宠、Live2D 或模型服务。同一个微信机器人请只启用一个接收器；两个项目的绑定、收件目录和 hooks 不会自动迁移。

宁宁人格是同人预设，不代表游戏原作、作者或配音演员背书。预设以已经较亲密的关系开始，可在本地修改 `prompt.nene.txt`。没有本地场景证据时，系统不会假称已从游戏截图检索到依据；角色一致性仍受提示词、上下文和模型能力影响。

## 开发运行

面向 **Windows 64 位、Python 3.13**。主程序不需要在同一 Python 环境安装 Torch。可选语音引擎使用自己的独立运行环境；不要把引擎的 Python 或 Torch 复制进主程序虚拟环境。

在仓库目录打开 PowerShell：

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-reading.txt "pynput>=1.7,<2" "Pillow>=12,<13" "sounddevice>=0.5,<1"
```

### 启动前诊断（源码模式）

先运行只依赖 Python 标准库的准备检查；尚未装齐 Qt 等依赖时也能输出缺项：

```powershell
.\.venv\Scripts\python.exe packaging/share_main.py --doctor
```

`required` 表示需要准备的项目，`optional` 表示按功能准备的项目。刚克隆时缺少未公开的 Live2D 模型会列为阻断项，退出码为 `2`；缺少场景证据、语音配置或 PDF 等可选组件不会单独导致失败。依赖检查只读取安装元数据，模型检查只核对 JSON 与声明文件，不能据此确认版本兼容、DLL、画面、点击、API 或音色正常。

默认只输出 JSON，不读取用户配置、聊天或密钥，不触网、不启动 Viewer、语音或窗口。可显式检查自己的 Viewer 目录，并写入已有普通目录中的一个新报告：

```powershell
.\.venv\Scripts\python.exe packaging/share_main.py --doctor --viewer-root 'D:\SteamLibrary\steamapps\common\Live2DViewerEX' --report .\doctor-report.json
```

上例路径应换成自己的安装目录。报告不包含绝对安装路径或凭据；不会覆盖已有文件，也不允许写进配置、素材或引擎目录。未传 `--viewer-root` 时，Viewer 标为尚未检查，不自动扫描安装或读取设置。此诊断只支持源码模式，不替代完整资源包的 `--share-self-test` 或实机验收。

然后完成以下准备：

1. 自行安装、拥有并登录可用的 Live2DViewerEX，切换到桌面模式并启用 ExAPI。
2. 将有权使用的标准 Cubism 导出模型放到 [约定目录](docs/LOCAL_ASSETS.md#live2d-模型)。程序读取 `.model3.json` 和依赖资源，不能直接渲染 `.cmo3` 编辑工程。
3. 准备自己的对话 API Key 及可用额度；首次设置中可选择服务商、模型和 API 地址。请勿将 Key 写进源码或提交到 GitHub。
4. 默认关闭 TTS、麦克风识别和屏幕视觉。语音合成是可选依赖，安装方式见 [本地素材说明](docs/LOCAL_ASSETS.md#可选日语语音)。

运行入口：

```powershell
.\.venv\Scripts\python.exe packaging/share_main.py
```

首次设置会在独立子进程打开；保存后父进程继续启动桌宠。需要重新设置 Viewer 安装目录或语音选项时，先退出桌宠，再执行：

```powershell
.\.venv\Scripts\python.exe packaging/share_main.py --share-setup
```

这个命令只保存设置并退出，之后再次运行普通入口。

源码模式的用户配置保存在 `data/share-development/data/profile/`，缓存保存在 `data/share-development/data/cache/`。首次 API Key 会按现有设置格式保存在本机配置文件中；**不要分享使用过的 `data` 目录**。`.gitignore` 只是防误提交措施，不会保护已经提交或强制加入的敏感文件。

## Viewer 和桌宠是两个程序

桌宠退出后，Live2DViewerEX 可能继续显示人物；请分别从两个程序的托盘菜单退出。桌宠不会自动退出、更新或登录你的 Steam / Viewer，也不会接管其他模型槽位。

本地模型固定载入 Viewer 的第二槽（API 槽号 `1`），请先确认该槽没有需要保留的其他人物。初次使用不自动修改模型大小或位置；人物、身体点击范围和字幕需要按你的屏幕布局校准。身体点击还没对准时，可以先从托盘选择“输入对话”。

## 验证范围与限制

- 主界面、资料工具和单元测试可离线检查；实际对话仍需要自己的 API。
- 既有 Windows 构建已验证“冻结主程序启动独立 CPU 引擎并生成非静音 WAV”的链路，**这不证明所有 NVIDIA 显卡、驱动、CUDA 版本或本地安装都兼容**，也不证明语音一定像角色或六种情绪已校准。该公开副本还做了路径一般化，仍需按自己的环境测试。
- 当前分享入口不提供麦克风语音识别，启用时会明确提示不可用。`requirements-voice.txt` 属于保留的上游可选依赖清单；安装它不等于该入口会启用 STT。
- 角色记忆、测试隔离和退出清理代码保留现有实现；本次公开整理没有增加记忆功能。自己的配置、历史、日志和资料内容不应上传到仓库。
- 历史保存使用同目录唯一临时文件和原子替换，Windows 文件占用或访问冲突只作短暂有限重试；永久失败保留旧文件并报告错误。这是存储可靠性修复，没有增加记忆功能或改变历史格式。
- 字体 `思源黑体Bold.otf` 单独附带，许可及版权证据见 `licenses/font/`。缺字体时会回退系统字体。字体许可不延伸到角色模型、游戏图像或声音。

`--share-self-test --report 某个新报告.json` 是“完整本地资源包”的严格检查，包含原作场景证据与语音参考的既有布局要求；刚克隆的源码没有这些素材，该检查会如实报告缺失。它不是“源码已坏”的结论，也不能替代真实人物、点击、API 和听感验收。

## 离线测试

在 Windows / Python 3.13 的开发环境中执行：

```powershell
.\.venv\Scripts\python.exe -m pip install -r tests/requirements-ci.txt
.\.venv\Scripts\python.exe tests/run_offline.py
```

这个专用 runner 将配置、历史和缓存隔离到临时目录，清除继承的应用凭据，并拒绝真实 Python 网络连接和子进程操作；API、音频输出和 Viewer 交互使用模拟对象。测试不需要 Steam、游戏、模型权重、GPU、麦克风或 API Key。

依赖未公开角色素材、被省略旧入口以及真实 loopback / 7-Zip 集成的测试会明确跳过，并列出原因。合成图片、metadata、音频和假响应只验证程序边界，不冒充原作证据或真实声音验收。详细范围见 [tests/README.md](tests/README.md)。GitHub 上是否自动运行取决于仓库是否安装并启用工作流；本地测试通过不表示远端 CI 已运行。

## 构建与贡献

`packaging/build_share.py` 使用显式资源清单生成 Windows onedir 构建，缺资源时应先补齐本地素材。`packaging/build_voice_bundle.py` 仅用于对用户自己准备的语音引擎做清单检查和本地分包；默认源目录为 `local-assets/GPT-SoVITS`，默认暂存目录为 `build/voice-stage`，也可用 `--source`、`--stage-root` 指定新目录。不会从本仓库下载游戏素材或补发第三方权重。

发布自己的二进制前，应检查素材授权、依赖许可证、对应源码和隐私文件。请先阅读 [本地素材与路径](docs/LOCAL_ASSETS.md)，不要把构建脚本需要的本地素材误当成允许重新分发的素材。

本项目保留上游 AGPL-3.0 许可证，见 [LICENSE](LICENSE)；上游基线提交见 [UPSTREAM_COMMIT.txt](UPSTREAM_COMMIT.txt)。外部程序、模型、字体及其他素材分别适用其自己的许可条件。

## 自动测试工作流示例

离线测试可直接按 `tests/README.md` 运行。`ci/windows-tests.example.yml` 是已准备的 GitHub Actions 配置，首次上传未自动启用，避免为可选 CI 增加 GitHub OAuth 权限。需要启用时，在自己有权维护工作流的环境中将它放到 `.github/workflows/tests.yml`；它仅运行离线测试，不读取 API 密钥或角色素材。
