# AI 工作流伙伴：任务跟进

从人物右键或托盘打开“AI 任务跟进…”。这版先让宁宁接收有来源的任务状态，帮助你知道什么时候回到执行工具处理问题。它不会替你启动任务、发送续写、批准操作或自动审阅产物，也不会将任务内容加入角色记忆。

## 能看到什么

Codex 桥接使用[官方生命周期 hooks](https://learn.chatgpt.com/docs/hooks)，只保存来源、会话 ID、轮次 ID、项目文件夹名、事件与时间。提问记录另外保存工具调用编号和由元数据计算的去重编号。不保存提问、回答、工具参数、工具输出或对话文件路径，不打开 transcript，不调用模型或网络。

| 事件 | 面板含义 |
| --- | --- |
| UserPromptSubmit / PreToolUse | 最近观察到正在运行 |
| PermissionRequest | 需要回 Codex 查看确认请求 |
| Stop | 本轮结束，待查看；不能据此判断整个任务完成或测试通过 |
| Interrupt | 本轮被中断 |
| SessionEnd | 会话结束；不代表工作成功 |

`PermissionRequest` 是请求操作批准，例如命令提权或网络访问确认，不代表所有澄清提问。观察器另从 `PreToolUse` 的 `tool_name` 精确识别 `request_user_input`、`request_user_input_async` 及其 `functions.` 前缀形式：前者提示发起输入请求，后者提示发起提问且任务可能仍在继续；都不会据此断言任务已经暂停。工具调用前的观察也不能证明提问已经成功显示给用户。

这些提问作为独立记录保存在收件目录的 `input-notices/` 下，最多保留 128 条，每条不足 4 KiB；同一次工具调用重复上报不刷新时间、不重复提醒。之后的“运行中”或“本轮结束”不会覆盖提问记录。任务面板的“最近提问事件”仅说明曾观察到请求，不判断问题是否已回答。免打扰、暂停跟进或角色测试模式会暂停提示，恢复时不补发期间的旧记录。

助手只在回答文本中提出问题时，仍无法仅凭元数据识别为提问；此时只能收到正常的本轮结束提示。部分专用工具路径可能不触发 hooks，超过 64 KiB 的 hook 输入也会整条忽略，覆盖范围须逐项验收，见[官方工具覆盖说明](https://learn.chatgpt.com/docs/hooks#tool-coverage)。

超过十分钟没有新事件时，显示状态已经过期，需回来源确认。长时间运行也可能只是一个工具尚未返回；不会把沉默判成失败。开始跟进时已有事件只作基线，不集中弹出旧通知。免打扰暂停扫描和提示，恢复后重新建立基线，不补播积压通知。任务面板不自动朗读或调用对话模型。

Grok Bot 通过显式协作上报接入，尚无事件时显示“未连接或尚无协作上报”。本项目没有改动 Grok Bot 安装包，也没有订阅其内部 Electron 状态，或读取它的登录令牌、对话数据库。

## 准备 Codex 接入

本机只读连接现有 app-server 并非所有桌面版本都可用。另开一个 app-server 的 `notLoaded` 状态也不能代表桌面中任务已经结束，因此这版使用显式 hooks 上报，而不是读取聊天历史来猜进度。

1. 先打开宁宁的任务面板，查看收件目录。使用该面板显示的完整路径，确保桥和当前桌宠指向同一个目录。
2. 在本源码目录的 PowerShell 中生成配置。以下 `--inbox` 仅是示例，必须替换成面板显示的路径：

```powershell
.\.venv\Scripts\python.exe tools/prepare_codex_workflow.py --inbox 'C:\NenePet\data\cache\workflow-inbox' --output .\codex-hooks.nene.generated.json
```

生成器将当前 Python 与观察脚本的绝对路径写入待审阅文件。移动源码或虚拟环境后要重新生成。它只创建新文件，拒绝覆盖，不修改 Codex 配置或信任设置。

Windows 生成的命令面向 PowerShell，使用调用运算符 `&` 和单引号参数；若手工配置其他执行 shell，须按该 shell 的规则适配并验收。旧版 Windows 配置使用了仅将程序和参数用双引号相连的 cmd 风格命令，在 PowerShell 中可能直接解析失败。已有此类配置时，请用当前生成器输出到新文件，替换原有宁宁观察器定义并保留其他 hooks，再到 `/hooks` 审阅并信任修改后的定义；旧定义的信任不会自动覆盖新命令。

3. 将生成的 `hooks` 定义合并到自己的 `~/.codex/hooks.json`，或需要跟进的项目 `.codex/hooks.json`。已有 hooks 必须保留：各事件下的处理器是数组，应追加本观察器，不能替换其他工具的定义。项目级定义只适用于该配置层生效的项目。
4. 根据[官方信任流程](https://learn.chatgpt.com/docs/hooks#review-and-trust-hooks)，在 Codex CLI 中打开 `/hooks`，审阅并信任这份准确的定义。未信任、定义变更或版本不支持时，Codex 可能跳过它；“文件已生成”不等于“已连接”。不要通过关闭信任检查来安装。
5. 在需要跟进的 Codex 会话中进行一次普通交互，核对面板是否出现对应项目、事件和时间。真实事件抵达之前，面板保持“尚未连接或尚无事件”。桌面版对这些事件的实际支持仍需要当机验收。

观察脚本只输出空成功结果，不提供允许、拒绝、阻断或上下文指令；无法写入时也不会接管 Codex 的审批流程。它不启动服务，不读取账号令牌。

### Windows CLI 启动提示

从桌面应用的 PATH 入口启动 `codex` 时，如果提示 `TERM is set to "dumb"`，可以在供你交互的 PowerShell 窗口清除这个继承值。如果随后报 `this CLI has no complete local package`，可先尝试独立交互模式：

```powershell
Remove-Item Env:TERM -ErrorAction SilentlyContinue
codex --no-daemon --no-alt-screen
```

清除 `TERM` 只影响当前窗口及其后续子进程，不改系统环境变量。根据[官方 CLI 参数说明](https://learn.chatgpt.com/docs/cli/reference)，`--no-daemon` 避开共享后台服务器，`--no-alt-screen` 保留终端滚屏；它们不绕过 hook 信任。启动后仍需输入 `/hooks`，亲自审阅并信任观察器。

此方式已在一台出现上述缺包报错的 Windows 电脑上进入 hook 审阅界面，因此遇到相同提示时不必先更新或重装。它不保证所有缺包或损坏情况都能启动；若仍失败，应根据新的错误检查官方完整安装，不能将启动成功当作提醒已接通。

可在 Windows 中单独验收生成命令的原生 PowerShell 执行。先在源码目录创建临时父目录，再传入其绝对路径：

```powershell
New-Item -ItemType Directory -Path .\shell-check-scratch
.\.venv\Scripts\python.exe tests/check_workflow_shell_windows.py --scratch-root (Resolve-Path .\shell-check-scratch).Path
```

此脚本只在指定父目录中新建并清理随机子目录，用真实观察脚本写合成收件目录，核对空 JSON 输出、敏感占位内容排除及特殊字符参数保持原样。它不读取真实配置或收件目录，不联网；由于会启动本机 PowerShell 子进程，需单独运行，不属于禁止子进程的 `tests/run_offline.py` 测试集。失败返回非零，不以跳过表示通过。

## Grok Bot 协作上报

安装桌面客户端不代表智能体正在本机执行：只有 Grok Bot 本身有权在这台电脑上运行命令，才能调用下面的桥接脚本。云端执行环境无法直接写入本机路径。不要为了接入通知而放宽它的审批规则。

让 Grok 在你明确交给它的任务中，于开始、需要输入、阶段结束或明确失败时调用脚本。以下只展示语法；路径和任务编号要替换为自己的，不要照抄成真实进度：

```powershell
.\.venv\Scripts\python.exe tools/nene_workflow_event.py --inbox 'C:\NenePet\data\cache\workflow-inbox' --task-id grok-example --phase-id phase-1 --event running --project example
.\.venv\Scripts\python.exe tools/nene_workflow_event.py --inbox 'C:\NenePet\data\cache\workflow-inbox' --task-id grok-example --phase-id phase-1 --event phase-ended --project example
```

`--event` 支持 `running`、`needs-input`、`phase-ended`、`interrupted`、`failed`。同一个任务使用稳定的 `--task-id`，每一阶段使用独立 `--phase-id`；`--project` 只提供简短项目名。不要将提问、答案、工具输出、路径或凭据塞进任务编号。新阶段应换阶段编号，不把已经结束的阶段改回正在运行。

输出 `{"accepted":true}` 只表示本机事件快照已接受，不保证宁宁已读到，也不代表任务成功。`false` 表示没有写入；命令退出码仍为 `0`，让观察器故障不打断主任务。Bot 异常退出、失去本机执行权限或忘记调用时，可能缺少上报；这是协作约定，不是原生自动监测。面板会注明“协作上报”，并将过期状态标为待确认。

Grok Bot 自带的手机提醒是另一条独立通道，其桌面活跃抑制、需要输入时的延迟和设备权限见[官方通知说明](https://docs.x.ai/grok-bot/settings-and-notifications#control-notifications)，不要把它与本项目接入混为一谈。

## 微信直连提醒

按用户选择，本项目不采用公众号推送服务。当前已核对腾讯的 [openclaw-weixin](https://github.com/Tencent/openclaw-weixin) 开源微信渠道，以及 [DeepSeek Harness 微信社区插件](https://github.com/sliverp/DeepSeek-harness-weixin)。后者是社区集成，不是 DeepSeek 官方发布的微信 skill；仅使用 DeepSeek 对话 API 不会自动获得该插件能力。

本项目采用依据腾讯公开协议实现的轻量纯文本适配器，不要求安装整个 OpenClaw，也不安装社区 Harness 插件。对话仍使用原本选定的模型，发送固定状态不额外调用 DeepSeek。需要二维码组件时，在源码环境安装：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-weixin.txt
```

本机隔离安装也可用 `--no-deps --target vendor/weixin -r requirements-weixin.txt`；程序只从固定的项目依赖目录加载该组件，二维码在本机绘制，不把登录内容发给二维码生成网站。

1. 在“AI任务跟进…”中打开“微信提醒…”，明确点击扫码登录，再用自己的微信确认授权。
2. 给刚绑定的机器人发一条普通消息，再在设置中获取通信上下文。程序只取扫码者本人的上下文令牌和游标，丢弃正文，不将手机消息作为命令、审批或模型提示词。
3. 明确点击发送测试，自行检查本人手机是否收到。界面上的“服务已受理”只代表接口受理，不能替你确认实际投递。
4. 勾选启用并保存设置后，才转发后续新的、通过去重和新鲜度检查的任务状态。不补发启用前的消息。

登录令牌、本人身份、上下文与游标只保存在独立的本机 DPAPI 加密文件 `workflow-weixin.json`，与角色记忆分开。默认关闭，无法解密时保持关闭。停止跟进、免打扰、角色测试模式和退出会取消待发任务；已经发出的 HTTP 请求无法撤回，恢复后不补发旧事件。通知只含固定来源、状态和哈希后的短任务号，不发送项目名、路径、问题或回答。

提问和操作确认比普通结束通知优先。待发队列最多四条，其中普通结束类最多占三条；新输入请求可替换尚未发送的普通更新，不能撤回在途请求。多个输入请求保持先后顺序；全部位置均被输入请求占用时仍可能丢弃新提醒，不无限排队或事后补发。

腾讯公开实现没有给出通信上下文令牌的有效期保证。需要本人先发消息建立上下文，并实测静默后的主动提醒；失效时需重新获取，不能保证全天候任意时刻可推送。二维码验证或手机端功能受限时如实提示，不自动绕过验证。这个适配器不开放任意联系人发送、媒体消息或手机端远程审批。

观察到服务拒绝码 `-2` 时，程序会暂停本次运行的微信通知并清空待发队列；该码没有足够明确的唯一原因，不能直接认定为登录过期。可先由本人给机器人发一句话、重新建立上下文，再显式测试和启用；若仍失败，再核对登录或服务状态。上下文刷新后的测试成功也不构成长期主动推送的保证。详见[腾讯公开协议的响应与上下文说明](https://github.com/Tencent/openclaw-weixin/blob/main/docs/protocol.md)。

## 数据与退出

桥接快照保存在明确指定的本机收件目录，只保留有界的自有任务事件。它们是临时任务元数据，不是聊天记忆。面板在 RAM 中去重；关闭面板仍可跟进，暂停跟进或退出桌宠则停止轮询。独立 Codex 任务继续由 Codex 管理。

收件目录不应提交到 GitHub。旧快照、伪造本地事件或丢失 hooks 都会影响准确性，面板不能作为产物验收依据。请回 Codex 查看实际回答、变更和测试，再决定后续操作。

手机提醒还依赖电脑处于可运行状态、网络可用及微信通道仍有权限；电脑睡眠或桌宠退出后，本机转发不能继续。Codex hook 的输入超过 64KiB 会整条忽略，状态可能缺项；沉默仍不能被当成任务失败。
