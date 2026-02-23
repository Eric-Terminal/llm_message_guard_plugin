import importlib.util
import sys
import types
import unittest
from enum import Enum
from pathlib import Path


PLUGIN_PATH = Path(__file__).resolve().parents[1] / "plugin.py"


def _install_stub_modules() -> None:
    src_pkg = types.ModuleType("src")
    src_pkg.__path__ = []
    sys.modules["src"] = src_pkg

    plugin_system = types.ModuleType("src.plugin_system")

    class BasePlugin:
        pass

    class BaseEventHandler:
        def get_config(self, _key, default=None):
            return default

    class ComponentInfo:
        pass

    class ConfigField:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class EventType:
        ON_START = "ON_START"
        ON_STOP = "ON_STOP"

    class MaiMessages:
        pass

    def register_plugin(cls):
        return cls

    plugin_system.BasePlugin = BasePlugin
    plugin_system.BaseEventHandler = BaseEventHandler
    plugin_system.ComponentInfo = ComponentInfo
    plugin_system.ConfigField = ConfigField
    plugin_system.EventType = EventType
    plugin_system.MaiMessages = MaiMessages
    plugin_system.register_plugin = register_plugin
    sys.modules["src.plugin_system"] = plugin_system

    apis = types.ModuleType("src.plugin_system.apis")

    class DummyLogger:
        def info(self, *_args, **_kwargs):
            return None

        def warning(self, *_args, **_kwargs):
            return None

        def error(self, *_args, **_kwargs):
            return None

        def debug(self, *_args, **_kwargs):
            return None

    class MessageAPI:
        def __init__(self):
            self.get_messages_before_time_in_chat = lambda **_kwargs: []

    apis.get_logger = lambda _name: DummyLogger()
    apis.message_api = MessageAPI()
    sys.modules["src.plugin_system.apis"] = apis

    config_pkg = types.ModuleType("src.config")
    config_pkg.__path__ = []
    sys.modules["src.config"] = config_pkg
    config_mod = types.ModuleType("src.config.config")
    config_mod.global_config = types.SimpleNamespace(
        chat=types.SimpleNamespace(max_context_size=20),
        bot=types.SimpleNamespace(nickname="麦麦"),
    )
    sys.modules["src.config.config"] = config_mod

    chat_pkg = types.ModuleType("src.chat")
    chat_pkg.__path__ = []
    sys.modules["src.chat"] = chat_pkg
    chat_utils_pkg = types.ModuleType("src.chat.utils")
    chat_utils_pkg.__path__ = []
    sys.modules["src.chat.utils"] = chat_utils_pkg

    chat_builder = types.ModuleType("src.chat.utils.chat_message_builder")
    chat_builder.replace_user_references = lambda text, _platform, replace_bot_name=True: text
    sys.modules["src.chat.utils.chat_message_builder"] = chat_builder

    chat_utils = types.ModuleType("src.chat.utils.utils")
    chat_utils.is_bot_self = lambda _platform, user_id: user_id == "bot-id"
    chat_utils.translate_timestamp_to_human_readable = lambda ts, mode="relative": f"T{int(ts)}"
    sys.modules["src.chat.utils.utils"] = chat_utils

    llm_pkg = types.ModuleType("src.llm_models")
    llm_pkg.__path__ = []
    sys.modules["src.llm_models"] = llm_pkg
    payload_pkg = types.ModuleType("src.llm_models.payload_content")
    payload_pkg.__path__ = []
    sys.modules["src.llm_models.payload_content"] = payload_pkg

    payload_message_mod = types.ModuleType("src.llm_models.payload_content.message")

    class RoleType(Enum):
        System = "system"
        User = "user"
        Assistant = "assistant"
        Tool = "tool"

    class Message:
        def __init__(self, role, content):
            self.role = role
            self.content = content

    class MessageBuilder:
        def __init__(self):
            self._role = RoleType.User
            self._content = ""

        def set_role(self, role):
            self._role = role
            return self

        def add_text_content(self, text):
            self._content = text
            return self

        def build(self):
            return Message(self._role, self._content)

    payload_message_mod.RoleType = RoleType
    payload_message_mod.Message = Message
    payload_message_mod.MessageBuilder = MessageBuilder
    sys.modules["src.llm_models.payload_content.message"] = payload_message_mod

    model_client_pkg = types.ModuleType("src.llm_models.model_client")
    model_client_pkg.__path__ = []
    sys.modules["src.llm_models.model_client"] = model_client_pkg

    model_client_base_mod = types.ModuleType("src.llm_models.model_client.base_client")

    class BaseClient:
        pass

    model_client_base_mod.BaseClient = BaseClient
    sys.modules["src.llm_models.model_client.base_client"] = model_client_base_mod

    person_pkg = types.ModuleType("src.person_info")
    person_pkg.__path__ = []
    sys.modules["src.person_info"] = person_pkg
    person_mod = types.ModuleType("src.person_info.person_info")

    class Person:
        def __init__(self, platform, user_id):
            self.platform = platform
            self.user_id = user_id
            self.person_name = ""

    person_mod.Person = Person
    sys.modules["src.person_info.person_info"] = person_mod


def _load_plugin_module():
    module_name = "llm_message_guard_plugin_under_test"
    sys.modules.pop(module_name, None)
    _install_stub_modules()

    spec = importlib.util.spec_from_file_location(module_name, PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _make_message(user_id: str, nickname: str, content: str, ts: float):
    user_info = types.SimpleNamespace(
        platform="qq",
        user_id=user_id,
        user_nickname=nickname,
        user_cardname="",
    )
    return types.SimpleNamespace(
        user_info=user_info,
        display_message=content,
        processed_plain_text=content,
        time=ts,
    )


class AssistantHistorySplitTestCase(unittest.TestCase):
    def setUp(self):
        self.plugin = _load_plugin_module()
        self.plugin._RUNTIME_CONFIG["merge_consecutive"] = True
        self.plugin._RUNTIME_CONFIG["max_context_size_override"] = 0

    def test_assistant_history_is_split_into_user_prefix_and_assistant_content(self):
        history_messages = [
            _make_message("u1", "小明", "今晚吃啥？", 1),
            _make_message("bot-id", "麦麦", "火锅可以，我知道一家店", 2),
            _make_message("u1", "小明", "人均大概多少？", 3),
        ]

        self.plugin._split_prompt_blocks = lambda _prompt: self.plugin.PromptSplitResult(
            system_prefix="sys-prefix",
            system_suffix="sys-suffix",
        )
        self.plugin.message_api.get_messages_before_time_in_chat = lambda **_kwargs: history_messages

        self_obj = types.SimpleNamespace(chat_stream=types.SimpleNamespace(stream_id="chat-1"))
        messages = self.plugin._build_structured_messages(self_obj, prompt="dummy")

        expected = [
            (self.plugin.RoleType.System, "sys-prefix"),
            (self.plugin.RoleType.User, "T1, 小明: 今晚吃啥？"),
            (self.plugin.RoleType.User, "T2, 麦麦(你):"),
            (self.plugin.RoleType.Assistant, "火锅可以，我知道一家店"),
            (self.plugin.RoleType.User, "T3, 小明: 人均大概多少？"),
            (self.plugin.RoleType.System, "sys-suffix"),
        ]
        self.assertEqual(messages, expected)

    def test_consecutive_assistant_history_keeps_alignment_after_split(self):
        history_messages = [
            _make_message("u1", "小明", "今晚吃啥？", 1),
            _make_message("bot-id", "麦麦", "火锅可以", 2),
            _make_message("bot-id", "麦麦", "我知道一家店", 3),
            _make_message("u1", "小明", "那就去", 4),
        ]

        self.plugin._split_prompt_blocks = lambda _prompt: self.plugin.PromptSplitResult(
            system_prefix="sys-prefix",
            system_suffix="sys-suffix",
        )
        self.plugin.message_api.get_messages_before_time_in_chat = lambda **_kwargs: history_messages

        self_obj = types.SimpleNamespace(chat_stream=types.SimpleNamespace(stream_id="chat-1"))
        messages = self.plugin._build_structured_messages(self_obj, prompt="dummy")

        expected = [
            (self.plugin.RoleType.System, "sys-prefix"),
            (self.plugin.RoleType.User, "T1, 小明: 今晚吃啥？"),
            (self.plugin.RoleType.User, "T2, 麦麦(你):\nT3, 麦麦(你):"),
            (self.plugin.RoleType.Assistant, "火锅可以\n我知道一家店"),
            (self.plugin.RoleType.User, "T4, 小明: 那就去"),
            (self.plugin.RoleType.System, "sys-suffix"),
        ]
        self.assertEqual(messages, expected)


if __name__ == "__main__":
    unittest.main()
