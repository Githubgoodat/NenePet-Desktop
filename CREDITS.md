# 来源与致谢

## 桌宠代码基础与灵感

本项目直接基于 **[kuxiaowo/AIpet-Murasame](https://github.com/kuxiaowo/AIpet-Murasame)** 改造，沿用其模块化程序架构、透明 Qt 桌宠、点击输入、对话服务、语音、设置及系统托盘等实现。该仓库既是本项目的灵感来源，也是直接代码上游。感谢 kuxiaowo 和上游贡献者的工作。

本项目采用的上游基线为 [`74599a3def49cf0d57a29f511dae8d063d695612`](https://github.com/kuxiaowo/AIpet-Murasame/tree/74599a3def49cf0d57a29f511dae8d063d695612)，亦记录于 [UPSTREAM_COMMIT.txt](UPSTREAM_COMMIT.txt)。之后的本地改动包括宁宁角色预设、Live2DViewerEX 适配、外部日语语音、表演时间轴、互动控制、共同阅读工具和便携启动入口。源码保留 [AGPL-3.0 许可证](LICENSE)；已有源文件中的版权和许可声明继续保留。

AIpet-Murasame 的[上游说明](https://github.com/kuxiaowo/AIpet-Murasame/blob/74599a3def49cf0d57a29f511dae8d063d695612/README.md#credits-and-license)还致谢了更早的桌宠项目 **[LemonQu-GIT/MurasamePet](https://github.com/LemonQu-GIT/MurasamePet)**。

## 宁宁 Live2D 模型

- 作品：**[《宁宁live2d》](https://steamcommunity.com/sharedfiles/filedetails/?id=3578664864)**。
- 作者：**[07nene](https://steamcommunity.com/profiles/76561199513188793/myworkshopfiles?appid=616720)**。
- 原始发布平台：Steam Workshop / Live2DViewerEX，物品 ID `3578664864`。

本项目本地使用的 `nene-editable` 运行模型及其恢复的 Cubism 工程，来源于上述作品。工程恢复、参数适配和新增程序动作不改变原模型作者署名，也不意味着恢复了作者的原始 PSD、图层命名或编辑历史。感谢 07nene 制作模型。

**公开仓库不包含该模型本体、纹理、LPK、可编辑 CMO3 工程或原始美术。** 请从原始发布渠道了解作品，使用自己有权使用的素材。解包或个人使用许可不自动包含公开分发权；署名也不表示作者认可本项目的全部改动或提供官方背书。

## 原作、语音与外部工具

绫地宁宁（綾地寧々 / Ayachi Nene）及《魔女的夜宴》（サノバウィッチ / Sanoba Witch）为柚子社（YUZUSOFT）原作角色与作品。本项目为非官方同人程序，角色、原作美术、台词和声音的权利归各自权利人，程序源码许可不覆盖这些素材。仓库不提供游戏语音、声优录音、游戏截图或角色语音模型权重。

可选语音合成使用 **[RVC-Boss/GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS)**；人物显示依赖用户自行安装的 **Live2DViewerEX**，模型编辑使用 **Live2D Cubism Editor**。这些是独立项目或产品，分别适用自己的许可和使用条件，未作为本项目原创软件署名。

随仓库提供的思源黑体字体，其版权记录与 Apache-2.0 许可单独见 [licenses/font](licenses/font/)。其他依赖按各自的许可使用；打包发布时还应附上对应版本的依赖许可。

## 微信通知协议参考

微信纯文本通知适配器参考腾讯开源的 **[Tencent/openclaw-weixin](https://github.com/Tencent/openclaw-weixin)** 中的[协议说明](https://github.com/Tencent/openclaw-weixin/blob/main/docs/protocol.md)及扫码、消息请求实现，保留 [Tencent MIT 许可](licenses/weixin/LICENSE-Tencent-MIT.txt)。本项目的轻量适配器不是腾讯提供的独立 SDK，也不是 DeepSeek 官方微信 skill；未附带 OpenClaw 或社区 Harness 插件。对话模型与微信传输分开，发送固定任务状态不需要新的模型请求。
