import json
import time
from typing import Any, Optional

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)


@register(
    "group_member_context",
    "WWWA7",
    "为 AstrBot 注入群成员身份信息，让模型知道群主、管理员、群昵称、头衔、QQ 与昵称",
    "1.0.1",
)
class GroupMemberContextPlugin(Star):
    SENDER_INFO_CACHE_KEY = "_group_member_context_sender_info"
    GROUP_SNAPSHOT_CACHE_KEY = "_group_member_context_group_snapshot"

    def __init__(self, context: Context, config: Optional[AstrBotConfig] = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self.enable_auto_inject = bool(self.config.get("enable_auto_inject", True))
        self.inject_group_admin_list = bool(
            self.config.get("inject_group_admin_list", True)
        )
        self.no_cache = bool(self.config.get("no_cache", False))
        self.sender_info_ttl_seconds = int(
            self.config.get("sender_info_ttl_seconds", 120)
        )
        self.group_snapshot_ttl_seconds = int(
            self.config.get("group_snapshot_ttl_seconds", 300)
        )
        self.smart_query_group_snapshot = bool(
            self.config.get("smart_query_group_snapshot", True)
        )
        self.extra_instruction = self._normalize_text(
            self.config.get("extra_instruction", "")
        )
        self._sender_info_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
        self._group_snapshot_cache: dict[tuple[str, bool], tuple[float, dict[str, Any]]] = {}

    @staticmethod
    def _normalize_text(text: object) -> str:
        if not isinstance(text, str):
            return ""
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        if "\n" not in normalized and (
            "\\r\\n" in normalized or "\\n" in normalized or "\\r" in normalized
        ):
            normalized = (
                normalized.replace("\\r\\n", "\n")
                .replace("\\n", "\n")
                .replace("\\r", "\n")
            )
        return normalized.strip()

    @staticmethod
    def _role_to_cn(role: str) -> str:
        role_map = {
            "owner": "群主",
            "admin": "管理员",
            "member": "普通成员",
            "unknown": "未知",
        }
        return role_map.get(str(role).lower(), "未知")

    @staticmethod
    def _safe_str(value: Any, fallback: str = "") -> str:
        if value is None:
            return fallback
        text = str(value).strip()
        return text if text else fallback

    @staticmethod
    def _is_group_event(event: AstrMessageEvent) -> bool:
        try:
            return bool(event.get_group_id())
        except Exception:
            return False

    @staticmethod
    def _is_group_role_related_text(text: str) -> bool:
        if not text:
            return False

        normalized = text.lower().strip()
        keywords = [
            "群主",
            "管理员",
            "管理",
            "权限",
            "身份",
            "owner",
            "admin",
            "bot是不是管理员",
            "你是管理员",
            "你是不是管理员",
            "你是不是群主",
            "你有管理权限",
            "谁是群主",
            "谁是管理员",
        ]
        return any(keyword in normalized for keyword in keywords)

    @staticmethod
    def _is_cache_valid(
        cache_item: Optional[tuple[float, dict[str, Any]]], ttl_seconds: int
    ) -> bool:
        if not cache_item:
            return False
        timestamp, _ = cache_item
        return (time.time() - timestamp) < max(ttl_seconds, 0)

    async def _call_action(
        self, event: AiocqhttpMessageEvent, action: str, **params: Any
    ) -> Any:
        return await event.bot.api.call_action(action, **params)

    @staticmethod
    def _unwrap_action_data(result: Any) -> Any:
        if not isinstance(result, dict):
            return result
        if "data" in result:
            return result.get("data")
        return result

    def _extract_sender_info_from_event(
        self, event: AiocqhttpMessageEvent
    ) -> dict[str, Any]:
        sender: Any = None

        for attr_name in ("message_obj", "message_event", "_event", "event"):
            obj = getattr(event, attr_name, None)
            if obj is not None and hasattr(obj, "sender"):
                sender = getattr(obj, "sender", None)
                if sender is not None:
                    break
            if isinstance(obj, dict):
                sender = obj.get("sender")
                if sender is not None:
                    break

        if sender is None:
            raw_message = getattr(event, "raw_message_obj", None)
            if isinstance(raw_message, dict):
                sender = raw_message.get("sender")

        if sender is None:
            return {}

        if not isinstance(sender, dict):
            try:
                sender = dict(sender)
            except Exception:
                sender = {
                    "user_id": getattr(sender, "user_id", None),
                    "nickname": getattr(sender, "nickname", None),
                    "card": getattr(sender, "card", None),
                    "role": getattr(sender, "role", None),
                    "level": getattr(sender, "level", None),
                    "title": getattr(sender, "title", None),
                }

        user_id = self._safe_str(sender.get("user_id"), self._safe_str(event.get_sender_id()))
        nickname = self._safe_str(sender.get("nickname"), "未知")
        card = self._safe_str(sender.get("card"))
        return {
            "user_id": user_id,
            "nickname": nickname,
            "card": card,
            "card_or_nickname": card or nickname,
            "role": self._safe_str(sender.get("role"), "member"),
            "title": self._safe_str(sender.get("title")),
            "level": self._safe_str(sender.get("level")),
        }

    async def _get_sender_member_info(
        self, event: AiocqhttpMessageEvent
    ) -> dict[str, Any]:
        cached = event.get_extra(self.SENDER_INFO_CACHE_KEY)
        if isinstance(cached, dict):
            return cached

        event_sender_info = self._extract_sender_info_from_event(event)
        if event_sender_info:
            event.set_extra(self.SENDER_INFO_CACHE_KEY, event_sender_info)
            return event_sender_info

        group_id = str(event.get_group_id())
        sender_id = str(event.get_sender_id())
        cache_key = (group_id, sender_id)

        memory_cached = self._sender_info_cache.get(cache_key)
        if self._is_cache_valid(memory_cached, self.sender_info_ttl_seconds):
            member_info = memory_cached[1]
            event.set_extra(self.SENDER_INFO_CACHE_KEY, member_info)
            return member_info

        member_info = self._unwrap_action_data(
            await self._call_action(
                event,
                "get_group_member_info",
                group_id=int(group_id),
                user_id=int(sender_id),
                no_cache=self.no_cache,
            )
        )

        if not isinstance(member_info, dict):
            member_info = {}

        self._sender_info_cache[cache_key] = (time.time(), member_info)
        event.set_extra(self.SENDER_INFO_CACHE_KEY, member_info)
        return member_info

    async def _get_group_snapshot(
        self, event: AiocqhttpMessageEvent, include_admin_list: bool = True
    ) -> dict[str, Any]:
        cached = event.get_extra(self.GROUP_SNAPSHOT_CACHE_KEY)
        if isinstance(cached, dict):
            return cached

        group_id = str(event.get_group_id())
        self_id = int(event.get_self_id())
        cache_key = (group_id, include_admin_list)

        memory_cached = self._group_snapshot_cache.get(cache_key)
        if self._is_cache_valid(memory_cached, self.group_snapshot_ttl_seconds):
            snapshot = memory_cached[1]
            event.set_extra(self.GROUP_SNAPSHOT_CACHE_KEY, snapshot)
            return snapshot

        members = self._unwrap_action_data(
            await self._call_action(
                event,
                "get_group_member_list",
                group_id=int(group_id),
                no_cache=self.no_cache,
            )
        )

        if not isinstance(members, list):
            members = []

        owner = None
        admins: list[dict[str, str]] = []
        bot_member = None

        for member in members:
            if not isinstance(member, dict):
                continue

            user_id = self._safe_str(member.get("user_id"))
            nickname = self._safe_str(member.get("nickname"), user_id)
            card = self._safe_str(member.get("card"))
            title = self._safe_str(member.get("title"))
            role = self._safe_str(member.get("role"), "member")

            item = {
                "user_id": user_id,
                "nickname": nickname,
                "card": card,
                "card_or_nickname": self._safe_str(
                    member.get("card_or_nickname"), card or nickname or user_id
                ),
                "role": role,
                "role_name": self._role_to_cn(role),
                "title": title,
            }

            if role == "owner":
                owner = item
            elif role == "admin" and include_admin_list:
                admins.append(item)

            if user_id == str(self_id):
                bot_member = item

        snapshot = {
            "group_id": group_id,
            "owner": owner,
            "admins": admins if include_admin_list else [],
            "bot_member": bot_member,
        }
        self._group_snapshot_cache[cache_key] = (time.time(), snapshot)
        event.set_extra(self.GROUP_SNAPSHOT_CACHE_KEY, snapshot)
        return snapshot

    def _format_sender_info(self, member_info: dict[str, Any]) -> str:
        user_id = self._safe_str(member_info.get("user_id"), "未知")
        nickname = self._safe_str(member_info.get("nickname"), "未知")
        card = self._safe_str(member_info.get("card"), "无")
        card_or_nickname = self._safe_str(
            member_info.get("card_or_nickname"), card if card != "无" else nickname
        )
        title = self._safe_str(member_info.get("title"), "无")
        role = self._safe_str(member_info.get("role"), "unknown")
        role_name = self._role_to_cn(role)
        level = self._safe_str(member_info.get("level"), "未知")

        return (
            "当前发言者信息：\n"
            f"- QQ：{user_id}\n"
            f"- 昵称：{nickname}\n"
            f"- 群昵称/群名片：{card}\n"
            f"- 当前显示名称（群名片优先）：{card_or_nickname}\n"
            f"- 群身份：{role_name}\n"
            f"- 专属头衔：{title}\n"
            f"- 群等级：{level}"
        )

    def _format_member_brief(self, member: Optional[dict[str, Any]]) -> str:
        if not member:
            return "未知"
        card = self._safe_str(member.get("card"))
        nickname = self._safe_str(member.get("nickname"), "未知")
        display_name = card or nickname
        user_id = self._safe_str(member.get("user_id"), "未知")
        role_name = self._safe_str(member.get("role_name"), "未知")
        title = self._safe_str(member.get("title"), "无")
        return f"{display_name}（QQ:{user_id}，身份:{role_name}，头衔:{title}）"

    def _format_group_snapshot(self, snapshot: dict[str, Any]) -> str:
        owner_text = self._format_member_brief(snapshot.get("owner"))
        bot_text = self._format_member_brief(snapshot.get("bot_member"))

        parts = [
            "当前群身份概览：",
            f"- 群主：{owner_text}",
            f"- Bot 自身身份：{bot_text}",
        ]

        if self.inject_group_admin_list:
            admins = snapshot.get("admins", [])
            if admins:
                admin_lines = "\n".join(
                    f"  - {self._format_member_brief(admin)}" for admin in admins
                )
                parts.append(f"- 管理员列表：\n{admin_lines}")
            else:
                parts.append("- 管理员列表：无")

        return "\n".join(parts)

    def _build_injected_prompt(
        self,
        sender_info: dict[str, Any],
        snapshot: Optional[dict[str, Any]] = None,
    ) -> str:
        segments = [
            "\n[群成员身份上下文]",
            "以下信息来自 QQ 群实时接口，可信度高，回答涉及群成员身份时应优先依据这些信息，不要臆测。",
            self._format_sender_info(sender_info),
        ]

        if snapshot:
            segments.append(self._format_group_snapshot(snapshot))

        segments.extend(
            [
                (
                    "回答规则：\n"
                    "1. 群主/管理员/普通成员身份必须以接口返回 role 为准；\n"
                    "2. 群昵称/群名片以 card 字段为准，若为空再用 nickname；\n"
                    "3. 专属头衔以 title 字段为准，若为空则表示没有头衔；\n"
                    "4. 需要提及 QQ 号时，直接使用 user_id；\n"
                    "5. 若用户问“你是不是管理员/群主”“你几级了”“你的头衔/群名片/昵称是什么”等涉及 Bot 自身在当前群的信息，而上下文里未直接提供 Bot 字段，必须直接调用 `get_group_identity_snapshot` 后再继续回答；\n"
                    "6. 若需要查询其他成员是谁、谁是群主、谁是管理员、某个 QQ 对应谁、某人的头衔或群名片，优先调用 `query_group_member_identity` 或 `get_group_identity_snapshot`；\n"
                    "7. 需要调用工具时，直接调用工具并在拿到结果后一次性回答，不要先向用户发送“我先查一下”“我去看看”等过渡句。"
                ),
                "[/群成员身份上下文]\n",
            ]
        )

        if self.extra_instruction:
            segments.append(self.extra_instruction)

        return "\n".join(segments)

    @filter.on_llm_request()
    async def inject_group_member_context(
        self, event: AstrMessageEvent, req: ProviderRequest
    ):
        if not self.enable_auto_inject:
            return

        if not self._is_group_event(event):
            return

        if not isinstance(event, AiocqhttpMessageEvent):
            return

        try:
            sender_info = await self._get_sender_member_info(event)
            injected_prompt = self._build_injected_prompt(sender_info, None)

            req.system_prompt = (req.system_prompt or "") + "\n" + injected_prompt
            req.prompt = f"{injected_prompt}\n{req.prompt}" if req.prompt else injected_prompt

            logger.info(
                "已注入发送者身份上下文: "
                f"group_id={event.get_group_id()} "
                f"sender_id={event.get_sender_id()} "
                f"sender_role={sender_info.get('role', 'unknown')} "
                "inject_target=system_prompt+prompt "
                "source=event_sender_or_member_info"
            )
        except Exception as exc:
            logger.warning(f"注入群成员身份上下文失败: {exc}")

    @filter.llm_tool(name="get_group_identity_snapshot")
    async def get_group_identity_snapshot(self, event: AstrMessageEvent) -> str:
        """
        获取当前群的身份快照。
        适用于模型需要查询“其他成员”或整群身份信息时主动调用：
        - 当前群谁是群主
        - 当前群有哪些管理员
        - Bot 自己是不是管理员/群主
        - 需要查看完整群身份快照

        Returns:
            string: JSON 字符串，包含 sender、owner、admins、bot_member 等字段
        """
        if not self._is_group_event(event):
            return json.dumps(
                {"status": "error", "message": "当前不是群聊，无法查询群身份快照。"},
                ensure_ascii=False,
            )

        if not isinstance(event, AiocqhttpMessageEvent):
            return json.dumps(
                {"status": "error", "message": "当前平台暂不支持该查询。"},
                ensure_ascii=False,
            )

        try:
            sender_info = await self._get_sender_member_info(event)
            snapshot = await self._get_group_snapshot(
                event, include_admin_list=self.inject_group_admin_list
            )
            return json.dumps(
                {
                    "status": "success",
                    "group_id": str(event.get_group_id()),
                    "sender": {
                        "user_id": self._safe_str(sender_info.get("user_id")),
                        "nickname": self._safe_str(sender_info.get("nickname")),
                        "card": self._safe_str(sender_info.get("card")),
                        "card_or_nickname": self._safe_str(
                            sender_info.get("card_or_nickname")
                        ),
                        "role": self._safe_str(sender_info.get("role"), "member"),
                        "role_name": self._role_to_cn(
                            self._safe_str(sender_info.get("role"), "member")
                        ),
                        "title": self._safe_str(sender_info.get("title")),
                        "level": self._safe_str(sender_info.get("level")),
                    },
                    "owner": snapshot.get("owner"),
                    "admins": snapshot.get("admins", []),
                    "bot_member": snapshot.get("bot_member"),
                },
                ensure_ascii=False,
                indent=2,
            )
        except Exception as exc:
            logger.error(f"获取群身份快照失败: {exc}")
            return json.dumps(
                {"status": "error", "message": f"获取群身份快照失败: {exc}"},
                ensure_ascii=False,
            )

    @filter.llm_tool(name="query_group_member_identity")
    async def query_group_member_identity(
        self, event: AstrMessageEvent, keyword: str = ""
    ) -> str:
        """
        查询当前群中成员的身份信息。
        可用于查询群主、管理员、某个 QQ、昵称、群昵称、头衔对应的成员。
        Args:
            keyword(string): 搜索关键词，可输入 QQ 号、昵称、群昵称、头衔关键词。留空时返回群主、管理员和当前发言者。
        """
        if not self._is_group_event(event):
            return json.dumps(
                {"status": "error", "message": "当前不是群聊，无法查询群成员身份。"},
                ensure_ascii=False,
            )

        if not isinstance(event, AiocqhttpMessageEvent):
            return json.dumps(
                {"status": "error", "message": "当前平台暂不支持该查询。"},
                ensure_ascii=False,
            )

        try:
            group_id = int(event.get_group_id())
            sender_id = str(event.get_sender_id())
            members = self._unwrap_action_data(
                await self._call_action(
                    event,
                    "get_group_member_list",
                    group_id=group_id,
                    no_cache=self.no_cache,
                )
            )

            if not isinstance(members, list):
                members = []

            normalized_keyword = self._safe_str(keyword).lower()
            results = []

            for member in members:
                if not isinstance(member, dict):
                    continue

                user_id = self._safe_str(member.get("user_id"))
                nickname = self._safe_str(member.get("nickname"))
                card = self._safe_str(member.get("card"))
                card_or_nickname = self._safe_str(
                    member.get("card_or_nickname"), card or nickname
                )
                role = self._safe_str(member.get("role"), "member")
                title = self._safe_str(member.get("title"))

                if normalized_keyword:
                    searchable = " ".join(
                        [
                            user_id.lower(),
                            nickname.lower(),
                            card.lower(),
                            card_or_nickname.lower(),
                            title.lower(),
                            role.lower(),
                            self._role_to_cn(role).lower(),
                        ]
                    )
                    if normalized_keyword not in searchable:
                        continue
                else:
                    if role not in {"owner", "admin"} and user_id != sender_id:
                        continue

                results.append(
                    {
                        "user_id": user_id,
                        "nickname": nickname,
                        "card": card,
                        "card_or_nickname": card_or_nickname,
                        "role": role,
                        "role_name": self._role_to_cn(role),
                        "title": title,
                    }
                )

            return json.dumps(
                {
                    "status": "success",
                    "group_id": str(group_id),
                    "count": len(results),
                    "keyword": keyword,
                    "members": results,
                },
                ensure_ascii=False,
                indent=2,
            )
        except Exception as exc:
            logger.error(f"查询群成员身份失败: {exc}")
            return json.dumps(
                {"status": "error", "message": f"查询群成员身份失败: {exc}"},
                ensure_ascii=False,
            )
