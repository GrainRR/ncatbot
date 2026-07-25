from ncatbot.core import registrar
from ncatbot.event.qq import GroupMessageEvent, MessageEvent as BaseMessageEvent
from ncatbot.plugin import NcatBotPlugin
from ncatbot.types import MessageArray, PlainText
from ncatbot.types.qq import Json
from ncatbot.utils import get_log
from urllib.parse import urlparse, ParseResult
from bilibili_api import video
from datetime import datetime
import json
import aiohttp
import traceback
import re

LOG = get_log("bilibili_url_parser")


class BilibiliUrlParser(NcatBotPlugin):
    name = "bilibili_url_parser"
    version = "1.0.1"
    author = "物起"
    description = "解析bv号av号url链接b站小程序"

    async def on_load(self):
        #self.register_config("sessdata", "", value_type=str)
        LOG.info("BilibiliUrlParser解析器已启动")

    async def get_bili_vid(self, url:str):
        """解析B站链接，返回BV号或AV号"""
        if url is not None:
            try:
                # 正则匹配b23
                url_pattern = r'https?://b23\.tv/\S+'
                url_match = re.search(url_pattern, url)

                if url_match:
                    url = url_match.group(0)
                    parsed: ParseResult = urlparse(url)
                    LOG.info(f"从文本中提取b23链接: {url_match}")

                    if parsed.netloc == "b23.tv":
                        try:
                            async with aiohttp.ClientSession() as session:
                                async with session.head(url, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=10)) as response:
                                    raw_url = url
                                    url = str(response.url)
                                    LOG.info(f"重定向链接 {raw_url} 成功，最终链接为 {response.url}")
                        except Exception as e:
                            error_traceback = traceback.format_exc()
                            LOG.error(f"解析短链接 {url} 失败: {e}")
                            LOG.error(error_traceback)
                            return None
                # 正则匹配bv号
                bv_pattern = r"[bB][vV][1-9A-HJ-NP-Za-km-z]{10}"
                bv_match = re.search(bv_pattern, url)
                if bv_match:
                    bv_id = bv_match.group(0)
                    bv_id = "BV" + bv_id[2:]
                    LOG.info(f"解析 {url} 成功，返回标准BV号: {bv_id}")
                    return bv_id
                # 正则匹配av号
                av_pattern = r"[aA][vV](\d+)"
                av_match = re.search(av_pattern, url)
                if av_match:
                    av_id = av_match.group(1) 
                    LOG.info(f"解析 {url} 成功，返回AV号: av{av_id}")
                    return f"av{av_id}"
                return None
            except Exception as e:
                error_traceback = traceback.format_exc()
                LOG.error(f"解析链接 {url} 时发生错误: {e}")
                LOG.error(error_traceback)
                return None
        return None

    async def get_b23_url(self, msg: BaseMessageEvent):
        """从消息中提取并清理特殊格式的b23.tv链接"""
        try:
            for segment in msg.message.filter(Json):
                # 解析 JSON 数据
                json_data = json.loads(segment.data)
                # 提取 qqdocurl
                qqdocurl = json_data.get("meta", {}).get("detail_1", {}).get("qqdocurl")
                if not qqdocurl:
                    continue
                # 如果 qqdocurl 看起来已经是一个 URL，直接返回
                if isinstance(qqdocurl, str) and qqdocurl.startswith(('http://', 'https://', 'b23.tv')):
                    cleaned_url = qqdocurl.replace('\\', '')
                    LOG.info(f"解析特殊消息格式成功，返回结果为 {cleaned_url}")
                    return cleaned_url
                # 否则，尝试解析嵌套的 JSON
                if isinstance(qqdocurl, str):
                    cleaned_first = qqdocurl.replace('\\"', '"').replace('\\\\', '\\')
                    try:
                        inner_data = json.loads(cleaned_first)
                        final_url = inner_data.get('meta', {}).get('detail_1', {}).get('qqdocurl')
                        if final_url:
                            cleaned_url = final_url.replace('\\', '')
                            LOG.info(f"解析特殊消息格式成功，返回结果为 {cleaned_url}")
                            return cleaned_url
                    except json.JSONDecodeError:
                        # 如果解析失败，可能是已经是 URL 但不符合上面的检查条件
                        LOG.warning(f"无法解析为 JSON，尝试直接使用: {cleaned_first}")
                        cleaned_url = cleaned_first.replace('\\', '')
                        return cleaned_url
                else:
                    # 如果 qqdocurl 不是字符串，直接从中提取
                    final_url = qqdocurl.get('meta', {}).get('detail_1', {}).get('qqdocurl')
                    if final_url:
                        cleaned_url = final_url.replace('\\', '')
                        LOG.info(f"解析特殊消息格式成功，返回结果为 {cleaned_url}")
                        return cleaned_url
        except Exception as e:
            error_traceback = traceback.format_exc()
            LOG.error(f"解析特殊消息格式时出错: {e}")
            LOG.error(error_traceback)
        return None

    async def process_bili_url(self, msg: BaseMessageEvent):
        """统一处理B站链接，包括普通链接和特殊格式链接"""
        bvid = await self.get_bili_vid("".join(seg.text for seg in msg.message.filter_text()))
        if bvid is not None:
            LOG.info(f"解析普通链接成功，返回结果为 {bvid}")
            return bvid
        cleaned_url = await self.get_b23_url(msg)
        if cleaned_url:
            bvid = await self.get_bili_vid(cleaned_url)
            LOG.info(f"解析特殊消息格式成功，返回结果为 {bvid}")
            return bvid
        return None

    async def get_bv_info(self, video_id):
        """解析b站视频信息"""
        try:
            if video_id[0:2] == "av":
                v = video.Video(aid=int(video_id[2:]))
            else:
                v = video.Video(bvid=video_id)
            info = await v.get_info()
            title = info.get('title', 'N/A')
            bvid = info.get('bvid', 'N/A')
            aid = info.get('aid', 'N/A')
            # pic = info.get('pic', 'N/A')
            # pubdate = datetime.fromtimestamp(info.get('pubdate', 0)).strftime('%Y-%m-%d %H:%M:%S') if info.get('pubdate') else 'N/A'
            owner = info.get('owner', {})
            up_name = owner.get('name', 'N/A')
            up_mid = owner.get('mid', 'N/A')
            # stat = info.get('stat', {})
            # view = stat.get('view', 'N/A')
            # danmaku = stat.get('danmaku', 'N/A')
            # reply = stat.get('reply', 'N/A')
            # favorite = stat.get('favorite', 'N/A')
            # coin = stat.get('coin', 'N/A')
            # share = stat.get('share', 'N/A')
            # like = stat.get('like', 'N/A')
            # duration = info.get('duration', 0)
            # minutes, seconds = divmod(duration, 60)
            # hours, minutes = divmod(minutes, 60)
            # if hours > 0:
            #     duration_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
            # else:
            #     duration_str = f"{minutes:02d}:{seconds:02d}"
            # tname = info.get('tname_v2', 'N/A')
            # desc = info.get('desc', 'N/A')[:100] + '...' if len(info.get('desc', '')) > 100 else info.get('desc', 'N/A')
            output = f"""标题: {title}
UP主: {up_name} (UID: {up_mid})
BV号: {bvid}
av号: av{aid}
快捷链接: https://www.bilibili.com/video/{bvid}"""
            # 原详细输出保留备用：
            # output = f"""标题: {title}
            # BV号: {bvid}
            # av号: av{aid}
            # 分区: {tname}
            # 发布时间: {pubdate}
            # UP主: {up_name} (UID: {up_mid})
            # 时长: {duration_str}
            # {"-" * 20}
            # 播放量: {view}
            # 弹幕数: {danmaku}   评论数: {reply}
            # 收藏数: {favorite}   分享数: {share}
            # 点赞数: {like}   投币数: {coin}
            # {"-" * 20}
            # 简介: {desc}
            # {"-" * 20}
            # 快捷链接: https://www.bilibili.com/video/{bvid}"""
            message = MessageArray([
                    PlainText(text=output),
                    # Image(file=pic, url=pic)
                    ])
            LOG.info(f"获取视频信息成功，返回结果为 {message}")
            return message
        except Exception as e:
            error_traceback = traceback.format_exc()
            LOG.error(f"获取视频信息出现错误{e}")
            LOG.error(error_traceback)
            message = MessageArray([
                    PlainText(text=f"获取视频信息出现错误喵~\n{e}")
                    ])
            return message

    @registrar.qq.on_message()
    async def group_message(self, msg: BaseMessageEvent):
        bvid = await self.process_bili_url(msg)
        if bvid is not None:
            info = await self.get_bv_info(bvid)
            if isinstance(msg, GroupMessageEvent):
                await msg.reply(rtf=info, at_sender=False)
            else:
                await msg.reply(rtf=info)

