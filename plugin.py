import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple, Type

from src.plugin_system import (
    BasePlugin,
    BaseEventHandler,
    ComponentInfo,
    ConfigField,
    EventType,
    MaiMessages,
    register_plugin,
)
from src.plugin_system.apis import get_logger, message_api
from src.config.config import global_config
from src.chat.utils.chat_message_builder import replace_user_references
from src.chat.utils.utils import is_bot_self, translate_timestamp_to_human_readable
from src.llm_models.payload_content.message import Message, MessageBuilder, RoleType
from src.llm_models.model_client.base_client import BaseClient
from src.person_info.person_info import Person

logger = get_logger("llm_message_guard_plugin")


@dataclass
class PromptSplitResult:
    system_prefix: str
    system_suffix: str


@dataclass
class MergedHistoryBlock:
    role: RoleType
    speaker_key: str
    lines: List[str]


_PATCHED: bool = False
_ORIGINAL_METHODS: Dict[str, Callable[..., Any]] = {}
_RUNTIME_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "apply_group": True,
    "apply_private": True,
    "apply_rewrite": True,
    "merge_consecutive": True,
    "max_context_size_override": 0,
    "fallback_to_original": True,
    "verbose": False,
}


_TIMESTAMP_LINE_RE = re.compile(
    r"^\s*(\[[^\]]+\])?(刚刚|\d+秒前|\d+分钟前|\d+小时前|\d+天前|"
    r"\d{1,2}:\d{2}(?::\d{2})?|\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2}(?::\d{2})?),\s+.+$"
)


def _debug_log(text: str) -> None:
    if _RUNTIME_CONFIG.get("verbose", False):
        logger.info(text)


def _is_rewrite_prompt(prompt: str) -> bool:
    markers = [
        "现在请你对这句内容进行改写",
        "改写后的回复",
        "你现在想补充说明你刚刚自己的发言内容",
    ]
    return any(marker in prompt for marker in markers)


def _should_apply_for_stream(chat_stream: Any) -> bool:
    is_group = bool(getattr(chat_stream, "group_info", None))
    if is_group:
        return bool(_RUNTIME_CONFIG.get("apply_group", True))
    return bool(_RUNTIME_CONFIG.get("apply_private", True))


def _resolve_context_limit() -> int:
    override = int(_RUNTIME_CONFIG.get("max_context_size_override", 0) or 0)
    if override > 0:
        return override
    return int(global_config.chat.max_context_size)


def _infer_time_mode(prompt: str) -> str:
    if re.search(r"(秒前|分钟前|小时前|天前),\s", prompt):
        return "relative"
    if re.search(r"\d{1,2}:\d{2}(?::\d{2})?,\s", prompt):
        return "normal_no_YMD"
    return "relative"


def _split_prompt_blocks(prompt: str) -> Optional[PromptSplitResult]:
    lines = prompt.splitlines()
    if not lines:
        return None

    # 方案1：基于“当前时间：”锚点（reply 路径）
    time_line_index = next((idx for idx, line in enumerate(lines) if line.strip().startswith("当前时间：")), None)
    if time_line_index is not None:
        history_start = time_line_index + 1
        history_end = _find_suffix_start(lines, history_start)
        if history_end is None:
            history_end = len(lines)

        system_prefix = "\n".join(lines[:history_start]).strip()
        system_suffix = "\n".join(lines[history_end:]).strip()
        if system_prefix or system_suffix:
            return PromptSplitResult(system_prefix=system_prefix, system_suffix=system_suffix)

    # 方案2：基于聊天头锚点（rewrite 路径）
    header_keywords = ("下面是群里正在聊的内容", "这是你们之前聊的内容")
    header_index = next((idx for idx, line in enumerate(lines) if any(key in line for key in header_keywords)), None)
    if header_index is not None:
        history_start = header_index + 1
        while history_start < len(lines) and not lines[history_start].strip():
            history_start += 1
        history_end = _find_suffix_start(lines, history_start)
        if history_end is None:
            history_end = len(lines)

        system_prefix = "\n".join(lines[:history_start]).strip()
        system_suffix = "\n".join(lines[history_end:]).strip()
        if system_prefix or system_suffix:
            return PromptSplitResult(system_prefix=system_prefix, system_suffix=system_suffix)

    # 方案3：兜底扫描时间轴块
    fallback = _fallback_split_by_timeline(lines)
    if fallback:
        return fallback

    return None


def _find_suffix_start(lines: List[str], start_index: int) -> Optional[int]:
    suffix_prefixes = (
        "现在",
        "你现在想补充说明",
        "你正在",
        "现在请你对这句内容进行改写",
        "请你根据聊天内容",
        "改写后的回复",
        "你的名字是",
    )
    for idx in range(start_index, len(lines)):
        stripped = lines[idx].strip()
        if stripped.startswith(suffix_prefixes):
            return idx
    return None


def _fallback_split_by_timeline(lines: List[str]) -> Optional[PromptSplitResult]:
    best_start: Optional[int] = None
    best_end: Optional[int] = None
    current_start: Optional[int] = None
    timestamp_count = 0
    best_score = -1

    for idx in range(len(lines) + 1):
        is_end = idx == len(lines)
        line = "" if is_end else lines[idx].strip()
        is_history_line = _is_history_like_line(line, has_open_block=current_start is not None)

        if not is_end and is_history_line:
            if current_start is None:
                current_start = idx
                timestamp_count = 0
            if _is_timestamped_line(line):
                timestamp_count += 1
            continue

        if current_start is not None:
            block_len = idx - current_start
            if timestamp_count >= 1 and block_len > best_score:
                best_start = current_start
                best_end = idx
                best_score = block_len
            current_start = None
            timestamp_count = 0

    if best_start is None or best_end is None:
        return None

    system_prefix = "\n".join(lines[:best_start]).strip()
    system_suffix = "\n".join(lines[best_end:]).strip()
    if not system_prefix and not system_suffix:
        return None

    return PromptSplitResult(system_prefix=system_prefix, system_suffix=system_suffix)


def _is_history_like_line(line: str, has_open_block: bool) -> bool:
    if not line:
        return has_open_block
    if _is_timestamped_line(line):
        return True
    if line == "图片信息：":
        return True
    if line.startswith("[图片") and "的内容：" in line:
        return True
    if line.startswith("以下聊天开始时间："):
        return True
    return False


def _is_timestamped_line(line: str) -> bool:
    return bool(_TIMESTAMP_LINE_RE.match(line))


def _resolve_speaker_name(message: Any) -> str:
    platform = str(getattr(message.user_info, "platform", "") or "")
    user_id = str(getattr(message.user_info, "user_id", "") or "")

    if is_bot_self(platform, user_id):
        return f"{global_config.bot.nickname}(你)"

    try:
        if platform and user_id:
            person_name = Person(platform=platform, user_id=user_id).person_name
            if person_name:
                return str(person_name)
    except Exception:
        pass

    nickname = str(getattr(message.user_info, "user_nickname", "") or "")
    card_name = str(getattr(message.user_info, "user_cardname", "") or "")
    if nickname:
        return nickname
    if card_name:
        return card_name
    return user_id or "某人"


def _normalize_message_content(message: Any) -> str:
    platform = str(getattr(message.user_info, "platform", "") or "")
    raw_content = str(getattr(message, "display_message", "") or getattr(message, "processed_plain_text", "") or "")
    if not raw_content.strip():
        return ""

    # 统一处理 @/回复 引用，避免 <name:id> 形式直接暴露给模型
    content = replace_user_references(raw_content, platform, replace_bot_name=True)

    # 结构化历史里不保留 picid 原始令牌，统一为图片占位
    content = re.sub(r"\[picid:[^\]]+\]", "[图片]", content)
    return content.strip()


def _build_history_blocks(messages: List[Any], time_mode: str) -> List[MergedHistoryBlock]:
    merged_blocks: List[MergedHistoryBlock] = []
    merge_consecutive = bool(_RUNTIME_CONFIG.get("merge_consecutive", True))

    for message in messages:
        platform = str(getattr(message.user_info, "platform", "") or "")
        user_id = str(getattr(message.user_info, "user_id", "") or "")
        role = RoleType.Assistant if is_bot_self(platform, user_id) else RoleType.User

        content = _normalize_message_content(message)
        if not content:
            continue

        timestamp = getattr(message, "time", 0.0)
        if not isinstance(timestamp, (float, int)) or timestamp <= 0:
            timestamp = time.time()
        readable_time = translate_timestamp_to_human_readable(float(timestamp), mode=time_mode)

        speaker = _resolve_speaker_name(message)
        one_line = f"{readable_time}, {speaker}: {content}"
        speaker_key = f"{platform}:{user_id}:{role.value}"

        if merge_consecutive and merged_blocks and merged_blocks[-1].speaker_key == speaker_key:
            merged_blocks[-1].lines.append(one_line)
        else:
            merged_blocks.append(MergedHistoryBlock(role=role, speaker_key=speaker_key, lines=[one_line]))

    return merged_blocks


def _build_structured_messages(self_obj: Any, prompt: str) -> Optional[List[Tuple[RoleType, str]]]:
    split_result = _split_prompt_blocks(prompt)
    if split_result is None:
        _debug_log("[LLM消息守卫] 未能从prompt中拆分system前后段，准备回退")
        return None

    chat_stream = getattr(self_obj, "chat_stream", None)
    stream_id = str(getattr(chat_stream, "stream_id", "") or "")
    if not stream_id:
        _debug_log("[LLM消息守卫] 当前上下文缺少stream_id，准备回退")
        return None

    context_limit = _resolve_context_limit()
    history_messages = message_api.get_messages_before_time_in_chat(
        chat_id=stream_id,
        timestamp=time.time(),
        limit=context_limit,
        filter_intercept_message_level=1,
    )
    if not history_messages:
        _debug_log("[LLM消息守卫] 未读取到历史消息，准备回退")
        return None

    time_mode = _infer_time_mode(prompt)
    history_blocks = _build_history_blocks(history_messages, time_mode=time_mode)
    if not history_blocks:
        _debug_log("[LLM消息守卫] 历史消息构建为空，准备回退")
        return None

    messages: List[Tuple[RoleType, str]] = []
    if split_result.system_prefix:
        messages.append((RoleType.System, split_result.system_prefix))

    for block in history_blocks:
        messages.append((block.role, "\n".join(block.lines)))

    if split_result.system_suffix:
        messages.append((RoleType.System, split_result.system_suffix))

    if len(messages) < 2:
        _debug_log("[LLM消息守卫] 结构化消息条数过少，准备回退")
        return None

    return messages


def _make_patched_llm_generate_content(target_key: str):
    async def _patched(self_obj: Any, prompt: str):
        original_method = _ORIGINAL_METHODS.get(target_key)
        if not original_method:
            raise RuntimeError(f"缺少原始方法引用: {target_key}")

        if not bool(_RUNTIME_CONFIG.get("enabled", True)):
            return await original_method(self_obj, prompt)

        if not _should_apply_for_stream(getattr(self_obj, "chat_stream", None)):
            return await original_method(self_obj, prompt)

        if _is_rewrite_prompt(prompt) and not bool(_RUNTIME_CONFIG.get("apply_rewrite", True)):
            return await original_method(self_obj, prompt)

        try:
            structured_messages = _build_structured_messages(self_obj, prompt)
            if not structured_messages:
                if bool(_RUNTIME_CONFIG.get("fallback_to_original", True)):
                    return await original_method(self_obj, prompt)
                raise RuntimeError("结构化消息构建失败，且已关闭回退")

            def message_factory(_client: BaseClient) -> List[Message]:
                result: List[Message] = []
                for role, text in structured_messages:
                    content = text.strip()
                    if not content:
                        continue
                    result.append(MessageBuilder().set_role(role).add_text_content(content).build())
                return result

            content, (reasoning_content, model_name, tool_calls) = await self_obj.express_model.generate_response_with_message_async(
                message_factory=message_factory
            )

            safe_content = (content or "").strip()
            _debug_log(
                f"[LLM消息守卫] 结构化请求成功: mode={target_key}, model={model_name}, message_count={len(structured_messages)}"
            )
            return safe_content, reasoning_content, model_name, tool_calls

        except Exception as exc:
            logger.warning(f"[LLM消息守卫] 结构化请求失败: {exc}")
            if bool(_RUNTIME_CONFIG.get("fallback_to_original", True)):
                return await original_method(self_obj, prompt)
            raise

    return _patched


def _apply_monkey_patch() -> Tuple[bool, str]:
    global _PATCHED

    if _PATCHED:
        return True, "运行时补丁已存在，跳过重复应用"

    try:
        from src.chat.replyer.group_generator import GroupGenerator
        from src.chat.replyer.private_generator import PrivateGenerator

        _ORIGINAL_METHODS["group"] = GroupGenerator.llm_generate_content
        _ORIGINAL_METHODS["private"] = PrivateGenerator.llm_generate_content

        GroupGenerator.llm_generate_content = _make_patched_llm_generate_content("group")  # type: ignore[method-assign]
        PrivateGenerator.llm_generate_content = _make_patched_llm_generate_content("private")  # type: ignore[method-assign]

        _PATCHED = True
        return True, "运行时补丁应用成功"
    except Exception as exc:
        return False, f"运行时补丁应用失败: {exc}"


def _restore_monkey_patch() -> Tuple[bool, str]:
    global _PATCHED

    if not _PATCHED:
        return True, "运行时补丁未生效，无需恢复"

    try:
        from src.chat.replyer.group_generator import GroupGenerator
        from src.chat.replyer.private_generator import PrivateGenerator

        group_original = _ORIGINAL_METHODS.get("group")
        private_original = _ORIGINAL_METHODS.get("private")

        if group_original:
            GroupGenerator.llm_generate_content = group_original  # type: ignore[method-assign]
        if private_original:
            PrivateGenerator.llm_generate_content = private_original  # type: ignore[method-assign]

        _ORIGINAL_METHODS.clear()
        _PATCHED = False
        return True, "运行时补丁已恢复"
    except Exception as exc:
        return False, f"恢复运行时补丁失败: {exc}"


def _refresh_runtime_config(handler: BaseEventHandler) -> None:
    _RUNTIME_CONFIG["enabled"] = bool(handler.get_config("plugin.enabled", True))
    _RUNTIME_CONFIG["apply_group"] = bool(handler.get_config("runtime.apply_group", True))
    _RUNTIME_CONFIG["apply_private"] = bool(handler.get_config("runtime.apply_private", True))
    _RUNTIME_CONFIG["apply_rewrite"] = bool(handler.get_config("runtime.apply_rewrite", True))
    _RUNTIME_CONFIG["merge_consecutive"] = bool(handler.get_config("runtime.merge_consecutive", True))
    _RUNTIME_CONFIG["max_context_size_override"] = int(handler.get_config("runtime.max_context_size_override", 0) or 0)
    _RUNTIME_CONFIG["fallback_to_original"] = bool(handler.get_config("runtime.fallback_to_original", True))
    _RUNTIME_CONFIG["verbose"] = bool(handler.get_config("log.verbose", False))


class RuntimePatchOnStart(BaseEventHandler):
    event_type = EventType.ON_START
    handler_name = "llm_message_guard_on_start"
    handler_description = "启动时应用replyer运行时补丁"

    async def execute(self, message: MaiMessages | None) -> Tuple[bool, bool, Optional[str], None, None]:
        _refresh_runtime_config(self)

        if not _RUNTIME_CONFIG.get("enabled", True):
            _debug_log("[LLM消息守卫] 插件配置为禁用，跳过补丁应用")
            return True, True, "插件已禁用，跳过补丁应用", None, None

        ok, result = _apply_monkey_patch()
        if ok:
            logger.info(f"[LLM消息守卫] {result}")
            return True, True, result, None, None

        logger.error(f"[LLM消息守卫] {result}")
        return False, True, result, None, None


class RuntimePatchOnStop(BaseEventHandler):
    event_type = EventType.ON_STOP
    handler_name = "llm_message_guard_on_stop"
    handler_description = "停止时恢复replyer原始方法"

    async def execute(self, message: MaiMessages | None) -> Tuple[bool, bool, Optional[str], None, None]:
        ok, result = _restore_monkey_patch()
        if ok:
            logger.info(f"[LLM消息守卫] {result}")
            return True, True, result, None, None

        logger.error(f"[LLM消息守卫] {result}")
        return False, True, result, None, None


@register_plugin
class LLMMessageGuardPlugin(BasePlugin):
    plugin_name = "llm_message_guard_plugin"
    enable_plugin = True
    dependencies: List[str] = []
    python_dependencies: List[str] = []
    config_file_name = "config.toml"

    config_section_descriptions = {
        "plugin": "插件基础配置",
        "runtime": "运行时行为配置",
        "log": "日志配置",
    }

    config_schema = {
        "plugin": {
            "config_version": ConfigField(type=str, default="1.0.0", description="配置版本"),
            "enabled": ConfigField(type=bool, default=True, description="是否启用结构化消息守卫"),
        },
        "runtime": {
            "apply_group": ConfigField(type=bool, default=True, description="是否在群聊reply路径启用"),
            "apply_private": ConfigField(type=bool, default=True, description="是否在私聊reply路径启用"),
            "apply_rewrite": ConfigField(type=bool, default=True, description="是否在rewrite路径启用"),
            "merge_consecutive": ConfigField(
                type=bool,
                default=True,
                description="是否合并相邻同角色同一发送者的历史消息",
            ),
            "max_context_size_override": ConfigField(
                type=int,
                default=0,
                description="历史消息窗口覆盖值，0表示沿用宿主max_context_size",
            ),
            "fallback_to_original": ConfigField(
                type=bool,
                default=True,
                description="结构化流程失败时是否回退原始单prompt请求",
            ),
        },
        "log": {
            "verbose": ConfigField(type=bool, default=False, description="是否输出详细调试日志"),
        },
    }

    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        return [
            (RuntimePatchOnStart.get_handler_info(), RuntimePatchOnStart),
            (RuntimePatchOnStop.get_handler_info(), RuntimePatchOnStop),
        ]
