"""签到者身份解析：群昵称优先级、OneBot / QQ 官方接口兜底与诊断文本。

从原仓库 1.7.22–1.7.25 的实现移植而来，只依赖事件与 logger，不 import main。
签到主流程与 `/溯源昵称` 诊断共用同一套解析规则，避免两处各写一份优先级。
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..common import sanitize_error_text

# OneBot 系平台名：只有这些平台才有群名片（card）概念
ONEBOT_PLATFORM_NAMES = frozenset({"aiocqhttp", "onebot", "napcat", "lagrange"})
QQ_OFFICIAL_PLATFORM = "qq_official"


def onebot_id(value: Any) -> int | str:
    """OneBot 的 group_id / user_id 是整数；纯数字才转 int，其余原样带回。"""
    text = str(value or "").strip()
    return int(text) if text.isdigit() else text


def onebot_platform(event: AstrMessageEvent) -> str:
    """事件所属平台名（小写）；取不到返回空串。"""
    with contextlib.suppress(Exception):
        return str(event.get_platform_name() or "").strip().lower()
    return ""


def onebot_caller(event: AstrMessageEvent):
    """取当前事件可用的 OneBot call_action；bot / bot.api 两种挂载都兼容。"""
    bot = getattr(event, "bot", None)
    for holder in (bot, getattr(bot, "api", None)):
        caller = getattr(holder, "call_action", None)
        if callable(caller):
            return caller
    return None


def onebot_unwrap(info: Any) -> Any:
    """aiocqhttp 的 call_action 已解包 data；这里兼容完整信封形态。"""
    if (
        isinstance(info, dict)
        and isinstance(info.get("data"), dict)
        and ("status" in info or "retcode" in info)
    ):
        return info["data"]
    return info


def checkin_event_card(event: AstrMessageEvent) -> str:
    """从事件自身取群名片：MessageMember.card → 原始事件 sender.card。

    群名片可能挂在 ``message_obj.sender.card``（部分适配器/框架版本），
    也可能只在 OneBot 原始事件 ``message_obj.raw_message["sender"]["card"]``
    里。取不到返回空串，由调用方决定是否再查群成员接口。
    """
    candidates = []
    with contextlib.suppress(Exception):
        message_obj = getattr(event, "message_obj", None)
        sender = getattr(message_obj, "sender", None)
        candidates.append(getattr(sender, "card", None))
        raw = getattr(message_obj, "raw_message", None)
        payload = raw.get("sender") if isinstance(raw, dict) else getattr(raw, "sender", None)
        if isinstance(payload, dict):
            candidates.append(payload.get("card"))
        else:
            candidates.append(getattr(payload, "card", None))
    for candidate in candidates:
        name = " ".join(str(candidate or "").split())
        if name:
            return name
    return ""


async def checkin_member_info(event: AstrMessageEvent) -> tuple[dict | None, str]:
    """查 OneBot 群成员信息（权威 card）。

    返回 ``(info, error)``：info 为成员对象时 error 为空；不适用（私聊 /
    非 OneBot）返回 ``(None, "")``；调用失败返回 ``(None, 脱敏原因)``，
    供诊断指令展示。签到主流程只关心 info，失败时回退事件昵称。
    """
    platform = onebot_platform(event)
    if platform not in ONEBOT_PLATFORM_NAMES:
        return None, ""
    try:
        group_id = str(event.get_group_id() or "").strip()
        user_id = str(event.get_sender_id() or "").strip()
    except Exception:
        return None, ""
    caller = onebot_caller(event)
    if not (group_id and user_id and callable(caller)):
        return None, ""
    try:
        info = await asyncio.wait_for(
            caller(
                "get_group_member_info",
                group_id=onebot_id(group_id),
                user_id=onebot_id(user_id),
                no_cache=True,
            ),
            timeout=5,
        )
    except Exception as exc:
        detail = sanitize_error_text(exc)
        logger.warning(
            f"签到群昵称查询失败（平台={platform}, 群={group_id}, "
            f"用户={user_id}）: {detail}"
        )
        return None, detail
    info = onebot_unwrap(info)
    if isinstance(info, dict):
        return info, ""
    logger.warning(
        f"签到群昵称查询返回非对象结果（平台={platform}, 群={group_id}, "
        f"用户={user_id}）: {info!r}"
    )
    return None, "接口返回了非对象结果"


async def qq_official_member_info(event: AstrMessageEvent) -> tuple[dict | None, str]:
    """查 QQ 官方群成员信息（只有 username，没有群名片）。

    官方 ``GET /v2/groups/{group_openid}/members/{member_openid}`` 目前是
    内邀白名单能力（无权限返回 11253）。官方群消息事件本身已带
    ``author.username``，所以只有事件昵称为空时才需要这里兜底；返回
    ``(info, error)`` 供诊断指令展示，失败不阻断签到。
    """
    if onebot_platform(event) != QQ_OFFICIAL_PLATFORM:
        return None, ""
    try:
        group_id = str(event.get_group_id() or "").strip()
        user_id = str(event.get_sender_id() or "").strip()
    except Exception:
        return None, ""
    bot = getattr(event, "bot", None)
    http = getattr(getattr(bot, "api", None), "_http", None)
    request = getattr(http, "request", None)
    if not (group_id and user_id and callable(request)):
        return None, ""
    try:
        from botpy.http import Route
    except Exception as exc:
        return None, sanitize_error_text(exc)
    route = Route(
        "GET",
        "/v2/groups/{group_openid}/members/{member_openid}",
        group_openid=group_id,
        member_openid=user_id,
    )
    try:
        info = await asyncio.wait_for(request(route), timeout=5)
    except Exception as exc:
        detail = sanitize_error_text(exc)
        logger.warning(f"QQ 官方群成员查询失败（群={group_id}, 用户={user_id}）: {detail}")
        return None, detail
    if isinstance(info, dict):
        return info, ""
    logger.warning(
        f"QQ 官方群成员查询返回非对象结果（群={group_id}, 用户={user_id}）: {info!r}"
    )
    return None, "接口返回了非对象结果"


def pick_checkin_name(
    event_card: str, member_info: dict | None, event_name: str
) -> tuple[str, str]:
    """按来源优先级挑签到显示名，返回 ``(name, source)``。

    接口成功时以接口为准：``card`` 是权威群名片，空则用接口 ``nickname``；
    不再拿可能过期的事件 card 硬顶。接口失败/不适用时才用事件
    ``sender.card``，最后回退事件昵称。
    """
    if isinstance(member_info, dict):
        candidates = [
            (member_info.get("card"), "群成员接口.card"),
            (member_info.get("nickname"), "群成员接口.nickname"),
            (event_name, "事件昵称"),
        ]
    else:
        candidates = [
            (event_card, "事件.sender.card"),
            (event_name, "事件昵称"),
        ]
    for candidate, source in candidates:
        name = " ".join(str(candidate or "").split())
        if name:
            return name, source
    return "", "无"


async def resolve_checkin_name(event: AstrMessageEvent) -> tuple[str, str]:
    """签到者显示名与来源：QQ 官方 / OneBot / 事件字段。"""
    event_card = ""
    event_name = ""
    with contextlib.suppress(Exception):
        event_card = checkin_event_card(event)
    with contextlib.suppress(Exception):
        event_name = " ".join(str(event.get_sender_name() or "").split())
    if onebot_platform(event) == QQ_OFFICIAL_PLATFORM:
        # QQ 官方没有群名片：事件 username 就是唯一可用昵称；只有它为空
        # 时才去试内邀制的群成员接口（失败/无权限都回退空串）。
        if event_name:
            return event_name, "事件昵称"
        info, _error = await qq_official_member_info(event)
        if isinstance(info, dict):
            name = " ".join(str(info.get("username") or "").split())
            if name:
                return name, "官方群成员接口.username"
        return "", "无"
    member_info, _error = await checkin_member_info(event)
    return pick_checkin_name(event_card, member_info, event_name)


async def checkin_sender_name(event: AstrMessageEvent, limit: int = 16) -> str:
    """签到者群昵称：OneBot 群成员接口 card → 接口昵称 → 事件群名片 → 事件昵称。

    OneBot 群消息自带的 ``sender.card`` 可能缺失或过期，所以群聊里会主动
    查一次 ``get_group_member_info``（数字 ID + no_cache）。接口失败时不
    阻断签到，只把原因写进日志并回退事件字段。名字长度不可控：这里只做
    空白折叠与限长，emoji 留给 ``compose_poster`` 在绘制前剔除。
    """
    name, _source = await resolve_checkin_name(event)
    if len(name) > limit:
        name = name[:limit].rstrip() + "…"
    return name


async def nickname_diag_text(event: AstrMessageEvent) -> str:
    """`/溯源昵称` 诊断文本：事件字段、接口值与最终来源。"""
    platform = onebot_platform(event) or "未知"
    platform_id = ""
    group_id = ""
    user_id = ""
    event_name = ""
    with contextlib.suppress(Exception):
        platform_id = str(event.get_platform_id() or "")
    with contextlib.suppress(Exception):
        group_id = str(event.get_group_id() or "").strip()
    with contextlib.suppress(Exception):
        user_id = str(event.get_sender_id() or "").strip()
    with contextlib.suppress(Exception):
        event_name = " ".join(str(event.get_sender_name() or "").split())
    event_card = checkin_event_card(event)

    lines = ["🔎 群昵称识别诊断"]
    lines.append(f"平台：{platform}" + (f"（ID: {platform_id}）" if platform_id else ""))
    if group_id:
        lines.append(f"会话：群 {group_id} / 用户 {user_id or '未知'}")
    else:
        lines.append(f"会话：私聊 / 用户 {user_id or '未知'}")
    lines.append(f"事件昵称：{event_name or '（空）'}")
    lines.append(f"事件群名片：{event_card or '（空）'}")

    if platform == QQ_OFFICIAL_PLATFORM:
        member_info, api_error = await qq_official_member_info(event)
        api_name = ""
        if isinstance(member_info, dict):
            api_name = " ".join(str(member_info.get("username") or "").split())
            api_role = str(member_info.get("member_role") or "").strip()
            lines.append(
                f"官方群成员接口：username「{api_name or '（空）'}」"
                + (f" / member_role「{api_role}」" if api_role else "")
            )
        elif api_error:
            lines.append(f"官方群成员接口：失败（{api_error}）")
        else:
            lines.append("官方群成员接口：不可用（缺少 bot / 群号 / 用户号）。")
        lines.append(
            "说明：QQ 官方机器人没有群名片/群昵称，事件与接口都只提供 "
            "username（QQ 昵称）；无法显示与 QQ 昵称不同的群昵称。"
        )
        if event_name:
            name, source = event_name, "事件昵称"
        elif api_name:
            name, source = api_name, "官方群成员接口.username"
        else:
            name, source = "", "无"
    else:
        member_info, api_error = await checkin_member_info(event)
        if isinstance(member_info, dict):
            api_card = " ".join(str(member_info.get("card") or "").split())
            api_nick = " ".join(str(member_info.get("nickname") or "").split())
            lines.append(
                f"群成员接口：card「{api_card or '（空）'}」/ "
                f"nickname「{api_nick or '（空）'}」"
            )
            if not api_card:
                lines.append(
                    "说明：接口返回的群名片为空，该成员未设置群昵称（QQ 显示的就是昵称）。"
                )
        elif platform not in ONEBOT_PLATFORM_NAMES:
            lines.append("群成员接口：未调用（当前平台不是 OneBot 系，没有群名片概念）。")
        elif not group_id:
            lines.append("群成员接口：未调用（私聊没有群昵称）。")
        elif api_error:
            lines.append(f"群成员接口：失败（{api_error}）")
        else:
            lines.append("群成员接口：不可用（缺少 bot / 群号 / 用户号）。")

        name, source = pick_checkin_name(event_card, member_info, event_name)
    shown = name if len(name) <= 16 else name[:16].rstrip() + "…"
    lines.append(f"最终：{shown or '（空）'}（来源：{source}）")
    return "\n".join(lines)
