# 微信合作问答

这条专用通道让 Codex 或 grok bot **主动创建新问题**，由本人在微信回答，再让原执行进程领取答案继续工作。它不接管已经弹出的原生问题卡，不提供命令批准、账号登录或远程执行入口。

## 先启用通道

1. 启动宁宁，确认现有微信绑定和本人上下文可用。
2. 打开“微信问答”窗口，勾选“启用本人微信问答”并保存。
3. 从该窗口复制“合作脚本的专用 --inbox”。它与任务状态的 `workflow-inbox` 是不同目录。
4. 保持宁宁和微信连接在线，关闭免打扰与角色测试模式。

该功能默认关闭。问题和回答会经过微信服务，只处理已绑定本人的 `编号 答案` 或 `回复 编号 答案` 指令，不把它们加入角色记忆。普通聊天消息不是命令，不会被发送给执行器。Bark 的单向状态提醒不能接收回答。

启用后会持续接收本人发给这个机器人的消息，普通消息只用于刷新通信上下文，正文随即丢弃；无需回桌面点击刷新。发送与唯一接收连接分别工作，发送问题不必等待一轮长轮询结束。入站长轮询参照[腾讯公开实现](https://github.com/Tencent/openclaw-weixin/blob/main/src/api/api.ts#L409)采用 35 秒，空闲读取超时不视为断线，也不会补发出站消息。第一次接收初始化完成前，通道会暂时显示 `paused`。

脚本创建问题前检查本机心跳：

| 状态 | 含义 |
| --- | --- |
| `ready` | 通道已启用、当前接收条件满足，可以创建问题；不代表手机已收到 |
| `disabled` | 问答通道未启用 |
| `paused` | 通道已启用，但当前暂停或连接条件不满足 |
| `offline` | 心跳不存在、超过 20 秒、格式异常或无法安全读取 |

心跳只含版本、生产者标记、两个布尔状态及 UTC 时间，没有问题、答案、身份、路径或密钥。UI 正常运行时约每 5 秒刷新。读取心跳不会创建目录；没有产品选项可跳过在线检查。

## 创建一个新问题

使用宁宁的 Python 环境和当前安装目录中的 `tools/nene_workflow_question.py`。下列路径是示例，需要替换；专用目录以问答窗口显示的值为准。

```powershell
$questionPython = 'C:\NenePet-Desktop\python\python.exe'
$questionTool = 'C:\NenePet-Desktop\tools\nene_workflow_question.py'
$questionInbox = 'C:\NenePet-Desktop\data\cache\workflow-questions'
$questionFile = 'C:\MyProject\new-question.txt'

& $questionPython $questionTool ask `
  --inbox $questionInbox --source grok_bot `
  --task-id sample-task --turn-id sample-phase-1 --project '示例项目' `
  --question-file $questionFile --option '先整理资料' --option '先写提纲' `
  --timeout-seconds 600 --wait-seconds 600
```

`new-question.txt` 应是本次主动创建的 UTF-8 问题文件，只放需要本人回答的问题。脚本不会搜索聊天、读取会话数据库或自动提取上下文。也可以用 `--question '先做哪一步？'` 代替 `--question-file`；二者不能同时使用。文件路径必须是绝对路径，链接、重解析点、硬链接、父级穿越和超大文件会被拒绝。

`--source` 支持 `codex`、`grok_bot`。`source/task-id/turn-id` 三项由调用者明确提供，并共同绑定这次合作任务；它们不会自动发现原生会话或回答原生卡片。使用稳定、独立的短编号，不把正文、答案或凭据放进编号。`--project` 是用于手机展示的简短项目名。

| 参数或内容 | 限制 |
| --- | --- |
| 问题 | 1–800 字符 |
| `--option` | 可重复，最多 6 项，每项 1–120 字符 |
| 手机答案 | 1–2000 字符 |
| `--timeout-seconds` | 请求有效期，60–900 秒，默认 600 秒 |
| `--wait-seconds` | 本次进程等待预算，0–900 秒，默认 0；最多等待到请求到期 |

微信中会显示项目、问题、选项、问题编号和剩余期限。回复格式是：

```text
回复 A1B2C3D4 B
```

也可直接发送 `A1B2C3D4 B`，省略“回复”前缀。上述编号仅为示例，使用实际收到的八位编号；不能只发选项字母，也不会按最近项目猜测接收者。选项可用 `A`–`F` 或 `1`–`6`，也可直接填写普通文字。`同意`、`允许` 等文字仍是普通答案，不会批准系统操作。

## 原进程怎样继续

`ask` 默认立即返回 `pending`。要在收到微信回答后继续，**同一原任务必须保留等待进程**，例如上例的 600 秒等待。脚本每次最多休眠 0.5 秒，按期限停止，不会忙循环或无限等待。

- Codex 使用支持后台会话的执行工具运行脚本。如果工具先返回 session ID，继续等待该 session 的结果；不要结束等待进程，也不要把手机答案写进它的 stdin。每次工具等待应保持有界。
- Grok 必须先核实其本机工具实际支持的后台进程或等待时长，使用它自己提供的句柄和取结果方法。不能假定它支持 Codex 的工具名称。若本轮工具不能保留足够长的进程，缩短等待预算或用 0，并如实保留待答 checkpoint。
- 收到 `answered` 后，将 JSON 中的 `answer` 作为普通用户输入交给**当前任务**继续判断，不拼接进 shell、Python、URL、SQL 或 `eval`。
- 得到 `expired`、`cancelled`，或 `pending` 且带 `channel_status` / `wait_timed_out` 时，停止本轮等待，保存 request ID 和 checkpoint，不无限反复轮询或自动重新发问。

当前版本没有配置 webhook 唤醒器。原等待进程结束后，文件中新出现的答案**不会自动唤醒** Codex 或 Grok，也不会打开新会话假装回答旧卡片。之后需要用户在原任务中继续，或由仍有权运行的原执行者明确调用 `get`。

## 领取或取消既有请求

使用创建时相同的目录、source、task-id、turn-id，以及输出中的完整 `request_id`：

```powershell
& $questionPython $questionTool get `
  --inbox $questionInbox --source grok_bot `
  --task-id sample-task --turn-id sample-phase-1 --request-id '<完整 request_id>'

& $questionPython $questionTool cancel `
  --inbox $questionInbox --source grok_bot `
  --task-id sample-task --turn-id sample-phase-1 --request-id '<完整 request_id>'
```

`get` 也支持有界的 `--wait-seconds`。领取和取消既有请求不要求通道在线。绑定不匹配不能获取答案或取消其他任务的问题。

stdout 始终是单行 JSON，错误使用固定代码，不包含异常正文或 traceback。正常状态的退出码为 0；参数错误为 2；存储、不可取消、未找到或创建前通道不可用为 1；等待被中断为 130。

| 返回状态 | 处理方式 |
| --- | --- |
| `pending` | 尚无可领取答案；只有等待预算耗尽时 `wait_timed_out` 才为 true |
| `answered` | 唯一包含 `answer` 的状态；表示答案已记录，不代表任务已经执行 |
| `cancelled` | 请求已取消，不包含答案 |
| `expired` | 请求期限已到，不再返回答案 |

等待中的 `ask` 会先领取存储结果，再检查心跳。已有答案、取消或过期状态优先；仍待答而通道暂停或失联时，返回 `pending` 并带固定 `channel_status`，保留完整请求编号。它不会把短等待预算结束伪装成请求过期。

重复 `get` 可能返回相同答案；执行者应按 `request_id` 记录已经处理的步骤，避免重复执行。问题没有回答时不会代替用户选择。

## 数据与边界

Windows 版本通过本机安全存储接口加密问题、选项、答案和本人路由信息，使用独立目录。过期后接口拒绝返回答案；后续创建新问题时会清理已过期的自有记录，不承诺在到期瞬间进行物理安全擦除。

密码、验证码和操作审批继续使用任务来源提供的安全界面。Grok 原生手机端可以处理原生问题和审批，但与本合作协议是两条通道，参见[官方手机说明](https://docs.x.ai/grok-bot/mobile)。Grok 的本机命令仍受原有权限策略约束，参见[官方本机执行边界](https://docs.x.ai/grok-bot/approvals-security-and-privacy#control-access-to-your-local-computer)。

离线测试只能证明代码边界。上线验收应核对本人微信收件、编号路由、实际工具等待能力、原任务继续、错误编号、重复消息、过期、免打扰及退出行为；接口受理不等于手机投递或任务完成。
