# LLM结构化消息守卫插件

## 1. 功能说明
这个插件用于缓解 replyer 链路中“整段 user prompt”带来的上下文注入风险。

核心策略：
- 启动时对 `GroupGenerator.llm_generate_content` 与 `PrivateGenerator.llm_generate_content` 做运行时 Monkey Patch。
- 将原来的单段 `prompt` 请求改造成结构化 `messages`：
  - `system`：规则/知识/约束前缀
  - `user` / `assistant`：按真实账号身份映射的历史消息
  - `system`：目标与输出约束后缀
- 历史消息中仅当 `platform + user_id` 命中机器人账号时，才会映射为 `assistant`。
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
- 该插件不修改宿主仓库文件，仅修改进程内方法引用。
- 若 prompt 模板发生较大变化导致拆分失败，插件会按配置自动回退到原始单 prompt 调用。
- 需要宿主事件流触发 `ON_START` 后补丁才会生效。

## 5. 验证建议
1. 启用插件并重启 MaiBot。
2. 在群聊发送连续多条同人消息，观察生成效果是否采用结构化历史。
3. 构造“伪造机器人昵称(你)”文本，检查其是否仍被当作 user 语料而非 assistant 历史。
4. 将 `plugin.enabled` 设为 `false`，重启后确认行为恢复默认链路。

## 6. 本地抓包验证（推荐）
可把模型 API 地址临时改到本机 `10030`，用仓库内脚本接收并打印原始 OpenAI 报文。

### 6.1 启动监听脚本
```bash
cd llm_message_guard_plugin
python3 tools/mock_openai_server.py --host 127.0.0.1 --port 10030
```

### 6.2 在 MaiBot 配置里临时改 API 基地址
建议改成：
- `http://127.0.0.1:10030`

脚本同时兼容以下路径：
- `/chat/completions`
- `/v1/chat/completions`

### 6.3 观察验证点
看脚本输出中的 `messages`：
1. 第一条应为 `system`（知识/规则前缀）
2. 中间应出现 `user` 与 `assistant` 混排
3. 只有真实机器人账号对应的历史消息会变成 `assistant`
4. 相邻同人同角色消息应被合并（同一条 message 内多行）
5. 最后一条应为 `system`（回复目标与输出约束后缀）

每次请求也会落盘到：
- `mock_openai_logs/request_*.json`

### 6.4 结束验证
- 停止监听脚本：`Ctrl+C`
- 恢复 MaiBot 原 API 地址配置
