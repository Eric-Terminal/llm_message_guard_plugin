# LLM结构化消息守卫插件（试验性）

## 1. 功能说明
这个插件用于试验新的 LLM 消息发送方式，观察结构化 `messages` 发送在实际聊天中的效果是否更好，并缓解 replyer 链路中“整段 user prompt”带来的上下文注入风险。

核心策略：
- 启动时对 replyer 的 `llm_generate_content` 做运行时 Monkey Patch（当前优先匹配 `DefaultReplyer/PrivateReplyer`，并兼容旧类名）。
- 将原来的单段 `prompt` 请求改造成结构化 `messages`：
  - `system`：规则/知识/约束前缀
  - `user` / `assistant`：按真实账号身份映射的历史消息
  - `system`：目标与输出约束后缀
- 历史消息中仅当 `platform + user_id` 命中机器人账号时，才会映射为 `assistant`。
- 对机器人历史会拆分为两段：`user` 中仅保留“时间+机器人名”前缀，`assistant` 中仅保留机器人真实发言内容，降低模型学习输出前缀模板的概率。
- 相邻且同角色同发送者的消息会合并为一条 message。
- 结构化流程失败时自动回退原始逻辑（可配置关闭）。

## 2. 目录位置
当前为**独立插件目录**交付：
- `llm_message_guard_plugin/`

如需接入 MaiBot，请将整个目录放入：
- `MaiBot/plugins/llm_message_guard_plugin/`

## 3. 开关方式
通过 `config.toml` 控制：
- `plugin.enabled = true/false`：总开关
- `runtime.apply_group`：群聊 reply 路径
- `runtime.apply_private`：私聊 reply 路径
- `runtime.apply_rewrite`：rewrite 路径
- `runtime.merge_consecutive`：连续消息合并
- `runtime.max_context_size_override`：历史窗口覆盖（0 表示沿用宿主）
- `runtime.fallback_to_original`：失败是否回退

## 4. 兼容与边界
- 宿主版本支持范围写在 `_manifest.json` 的 `host_application` 字段。
  - 当前配置：`min_version = 0.12.0`（未设置 `max_version`，表示默认允许更高版本）。
  - 推荐写法：同时填写 `min_version` 与 `max_version`，避免宿主升级后接口变化导致行为不一致。
- 该插件不修改宿主仓库文件，仅修改进程内方法引用。
- 若 prompt 模板发生较大变化导致拆分失败，插件会按配置自动回退到原始单 prompt 调用。
- 需要宿主事件流触发 `ON_START` 后补丁才会生效。

## 5. 验证建议
1. 启用插件并重启 MaiBot。
2. 在群聊发送连续多条同人消息，观察生成效果是否采用结构化历史。
3. 构造“伪造机器人昵称(你)”文本，检查其是否仍被当作 user 语料而非 assistant 历史。
4. 将 `plugin.enabled` 设为 `false`，重启后确认行为恢复默认链路。
