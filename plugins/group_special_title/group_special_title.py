import ncatbot.webui.server
from ncatbot.core import registrar
from ncatbot.event.qq import GroupMessageEvent
from ncatbot.plugin import NcatBotPlugin
from ncatbot.types import At


def _get_halfwidth_units(text: str) -> int:
    """计算文本占用的半角字符单位数。

    Args:
        text: 需要计算长度的文本。

    Returns:
        文本占用的半角字符单位数。ASCII 字符计 1，非 ASCII 字符计 2。
    """

    units = 0

    for char in text:
        units += 1 if ord(char) < 128 else 2

    return units


class GroupSpecialTitle(NcatBotPlugin):
    name = "group_special_title"
    version = "0.1.0"
    author = "Li"
    description = "设置群成员专属头衔"

    @registrar.qq.on_group_command("#申请头衔")
    async def apply_special_title(self, event: GroupMessageEvent, title: str = ""):
        """处理群主设置自己专属头衔的命令。

        Args:
            event: 触发命令的群消息事件。
            title: 用户申请设置的专属头衔。为空时提示用户补充内容。
        """

        if not title:
            await event.reply("请填写头衔喵")
            return

        if _get_halfwidth_units(title) > 12:
            await event.reply("头衔过长了喵，最多只能设置 6 个汉字或 12 个英文/数字/半角字符喵")
            return

        if not await self._can_set_special_title(event):
            await event.reply("只有群主可以设置专属头衔喵")
            return

        try:
            # 这里调用的是 api.qq.manage 的 set_group_special_title 方法。
            await self.api.qq.manage.set_group_special_title(
                event.group_id, event.user_id, special_title=title
            )
            await event.reply(f"✅ 已为 {event.user_id} 设置头衔「{title}」了喵~")
        except Exception:
            await event.reply(f"❌ 为 {event.user_id} 设置头衔「{title}」失败了喵QWQ")


    @registrar.qq.on_group_command("#发放头衔")
    async def assign_special_title(
            self,
            event: GroupMessageEvent,
            target: At = None,
            title: str = ""
    ):
        """处理群主为群成员发放专属头衔的命令。

        Args:
            event: 触发命令的群消息事件。
            target: 通过 @ 指定的目标群成员。为空时提示用户重新 @ 目标。
            title: 需要发放的专属头衔。为空时提示用户补充内容。
        """

        if target is None:
            await event.reply("请 @ 要设置头衔的人喵，不要手动复制或者直接输入文本信息要正确地 @ 出来喵")
            return
        if not title:
            await event.reply("请填写头衔喵")
            return

        if not await self._can_set_special_title(event):
            await event.reply("只有群主可以设置专属头衔喵")
            return

        try:
            await self.api.qq.manage.set_group_special_title(
                event.group_id, target.user_id, special_title=title
            )
            await event.reply(f"✅ 已为 {target.user_id} 设置头衔「{title}」了喵~")
        except Exception:
            await event.reply(f"❌ 为 {target.user_id} 设置头衔「{title}」失败了喵")

    async def _can_set_special_title(self, event: GroupMessageEvent) -> bool:
        """判断当前命令发送者是否为群主。

        Args:
            event: 触发命令的群消息事件，用于获取当前群号和命令发送者 QQ 号。

        Returns:
            命令发送者是当前群群主时返回 True；命令发送者不是群主，
            或查询群成员信息失败时返回 False。
        """

        try:
            member_info = await self.api.qq.query.get_group_member_info(
                group_id=event.group_id,
                user_id=event.sender.user_id,
            )
        except Exception as exc:
            self.logger.exception("查询群成员权限失败: %s", exc)
            return False

        return getattr(member_info, "role", None) == "owner"
