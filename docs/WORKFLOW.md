# AI 工作流伙伴：任务跟进

从人物右键或托盘打开“AI 任务跟进…”。这版先让宁宁接收有来源的任务状态，帮助你知道什么时候回到执行工具处理问题。它不会替你启动任务、发送续写、批准操作或自动审阅产物，也不会将任务内容加入角色记忆。

## 能看到什么

Codex 桥接使用[官方生命周期 hooks](https://learn.chatgpt.com/docs/hooks)，只保存来源、会话 ID、轮次 ID、项目文件夹名、事件与时间。不保存提问、回答、工具参数、工具输出或对话文件路径，不打开 transcript，不调用模型或网络。

| 事件 | 面板含义 |
| --- | --- |
| UserPromptSubmit / PreToolUse | 最近观察到正在运行 |
| PermissionRequest | 需要回 Codex 查看确认请求 |
| Stop | 本轮结束，待查看；不能据此判断整个任务完成或测试通过 |
| Interrupt | 本轮被中断 |
| SessionEnd | 会话结束；不代表工作成功 |

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

3. 将生成的 `hooks` 定义合并到自己的 `~/.codex/hooks.json`，或需要跟进的项目 `.codex/hooks.json`。已有 hooks 必须保留：各事件下的处理器是数组，应追加本观察器，不能替换其他工具的定义。项目级定义只适用于该配置层生效的项目。
4. 根据[官方信任流程](https://learn.chatgpt.com/docs/hooks#review-and-trust-hooks)，在 Codex CLI 中打开 `/hooks`，审阅并信任这份准确的定义。未信任、定义变更或版本不支持时，Codex 可能跳过它；“文件已生成”不等于“已连接”。不要通过关闭信任检查来安装。
5. 在需要跟进的 Codex 会话中进行一次普通交互，核对面板是否出现对应项目、事件和时间。真实事件抵达之前，面板保持“尚未连接或尚无事件”。桌面版对这些事件的实际支持仍需要当机验收。

观察脚本只输出空成功结果，不提供允许、拒绝、阻断或上下文指令；无法写入时也不会接管 Codex 的审批流程。它不启动服务，不读取账号令牌。

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

登录令牌、本人身份、上下文与游标只保存在独立的本机 DPAPI 加密文件 `workflow-weixin.json`，与角色记忆分开。默认关闭，无法解密时保持关闭。停止跟进、免打扰和退出会取消待发任务；已经发出的 HTTP 请求无法撤回，恢复后不补发旧事件。通知只含固定来源、状态和哈希后的短任务号，不发送项目名、路径、问题或回答。

腾讯公开实现没有给出通信上下文令牌的有效期保证。需要本人先发消息建立上下文，并实测静默后的主动提醒；失效时需重新获取，不能保证全天候任意时刻可推送。二维码验证或手机端功能受限时如实提示，不自动绕过验证。这个适配器不开放任意联系人发送、媒体消息或手机端远程审批。

## 数据与退出

桥接快照保存在明确指定的本机收件目录，只保留有界的自有任务事件。它们是临时任务元数据，不是聊天记忆。面板在 RAM 中去重；关闭面板仍可跟进，暂停跟进或退出桌宠则停止轮询。独立 Codex 任务继续由 Codex 管理。

收件目录不应提交到 GitHub。旧快照、伪造本地事件或丢失 hooks 都会影响准确性，面板不能作为产物验收依据。请回 Codex 查看实际回答、变更和测试，再决定后续操作。

手机提醒还依赖电脑处于可运行状态、网络可用及微信通道仍有权限；电脑睡眠或桌宠退出后，本机转发不能继续。Codex hook 的输入超过 64KiB 会整条忽略，状态可能缺项；沉默仍不能被当成任务失败。
