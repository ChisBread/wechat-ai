"""
微信消息工厂模块。
负责微信消息的解析、构建与分发。
"""

#!/usr/bin/env python
# -*- coding: utf-8 -*-

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, List, Optional, Union
from pathlib import Path
import xmltodict
import zstandard as zstd
from google.protobuf.json_format import MessageToDict
from wechat_ai_bot.models import UserInfo

from .message_classes import *
from .parser.audio_parser import parser_audio
from .parser.emoji_parser import parser_emoji
from .parser.file_parser import parse_video
from .parser.link_parser import (
    parser_applet,
    parser_business,
    parser_favorite_note,
    parser_file,
    parser_link,
    parser_merged_messages,
    parser_pat,
    parser_position,
    parser_red_envelop,
    parser_reply,
    parser_transfer,
    parser_voip,
    parser_wechat_video,
)
from .parser.util.common import decompress, get_md5_from_xml
from .parser.util.protocbuf import (
    packed_info_data_pb2,
    packed_info_data_img2_pb2,
    packed_info_data_img_pb2,
    packed_info_data_merged_pb2,
)

if TYPE_CHECKING:
    from wechat_ai_bot.plugins.interface import DatabaseService


# 定义抽象工厂基类
class MessageFactory(ABC):
    """
    消息工厂抽象基类。
    定义了创建消息实例的接口。
    """

    @abstractmethod
    def create(
        self,
        message,
        user_info: UserInfo,
        db: "DatabaseService",
        contact: dict,
        room: dict,
    ):
        """
        创建消息实例
        :param message: 消息数据
        :param user_info: 用户信息对象
        :param db: 数据库对象
        :param contact: 联系人对象字典
        :param room: 群聊对象字典
        :return: 消息实例
        """
        pass


class UnknownMessageFactory(MessageFactory):
    """
    未知消息工厂。
    处理无法识别的消息类型。
    """

    def create(
        self,
        message,
        user_info: UserInfo,
        db: "DatabaseService",
        contact: dict,
        room: dict,
    ):
        msg = Message(
            local_id=message[0],
            server_id=message[1],
            local_type=message[2],
            sort_seq=message[3],
            real_sender_id=message[4],
            create_time=message[5],
            status=message[6],
            upload_status=message[7],
            download_status=message[8],
            server_seq=message[9],
            origin_source=message[10],
            source=message[11],
            message_content=message[12],
            compress_content=message[13],
            packed_info_data=message[14],
            message_db_path=message[17],
            contact=contact,
            room=room,
            user_info=user_info,
        )
        return msg


class TextMessageFactory(MessageFactory):
    """
    文本消息工厂。
    处理文本消息的解析与构建。
    """

    def create(
        self,
        message,
        user_info: UserInfo,
        db: "DatabaseService",
        contact: dict,
        room: dict,
    ):
        msg = TextMessage(
            local_id=message[0],
            server_id=message[1],
            local_type=message[2],
            sort_seq=message[3],
            real_sender_id=message[4],
            create_time=message[5],
            status=message[6],
            upload_status=message[7],
            download_status=message[8],
            server_seq=message[9],
            origin_source=message[10],
            source=message[11],
            message_content=message[12],
            compress_content=message[13],
            packed_info_data=message[14],
            message_db_path=message[17],
            contact=contact,
            room=room,
            content=message[12],
            user_info=user_info,
        )
        return msg


class ImageMessageFactory(MessageFactory):
    """
    图片消息工厂。
    处理图片消息的解析与构建。
    """

    def create(
        self,
        message,
        user_info: UserInfo,
        db: "DatabaseService",
        contact: dict,
        room: dict,
    ):
        filename = ""
        try:
            # 兼容微信4.0.3+ 的图片命名方式
            packed_info_data_proto = packed_info_data_img2_pb2.PackedInfoDataImg2()
            packed_info_data_proto.ParseFromString(message[14])
            packed_info_data = MessageToDict(packed_info_data_proto)
            image_info = packed_info_data.get("imageInfo", {})
            filename = image_info.get("filename", "").strip().strip('"').strip()
        except Exception:
            try:
                # 兼容旧版
                packed_info_data_proto = packed_info_data_img_pb2.PackedInfoDataImg()
                packed_info_data_proto.ParseFromString(message[14])
                packed_info_data = MessageToDict(packed_info_data_proto)
                filename = (
                    packed_info_data.get("filename", "").strip().strip('"').strip()
                )
            except Exception:
                filename = ""

        msg = ImageMessage(
            local_id=message[0],
            server_id=message[1],
            local_type=message[2],
            sort_seq=message[3],
            real_sender_id=message[4],
            create_time=message[5],
            status=message[6],
            upload_status=message[7],
            download_status=message[8],
            server_seq=message[9],
            origin_source=message[10],
            source=message[11],
            message_content=message[12],
            compress_content=message[13],
            packed_info_data=message[14],
            message_db_path=message[17],
            contact=contact,
            room=room,
            md5="",
            path="",
            thumb_path="",
            file_size=0,
            file_name=filename,
            file_type="png",
            user_info=user_info,
        )

        sender_wxid = msg.room.username if msg.is_chatroom else msg.contact.username

        path = db.get_image(
            xml_content=msg.parsed_content,
            message=msg,
            up_dir="",
            thumb=False,
            sender_wxid=sender_wxid,
        )
        if path:
            msg.path = str(path)

        path_thumb = db.get_image(
            xml_content=msg.parsed_content,
            message=msg,
            up_dir="",
            thumb=True,
            sender_wxid=sender_wxid,
        )
        if path_thumb:
            msg.thumb_path = str(path_thumb)

        return msg


class AudioMessageFactory(MessageFactory):
    """
    音频消息工厂。
    处理音频消息的解析与构建。
    """

    def create(
        self,
        message,
        user_info: UserInfo,
        db: "DatabaseService",
        contact: dict,
        room: dict,
    ):
        msg = AudioMessage(
            local_id=message[0],
            server_id=message[1],
            local_type=message[2],
            sort_seq=message[3],
            real_sender_id=message[4],
            create_time=message[5],
            status=message[6],
            upload_status=message[7],
            download_status=message[8],
            server_seq=message[9],
            origin_source=message[10],
            source=message[11],
            message_content=message[12],
            compress_content=message[13],
            packed_info_data=message[14],
            message_db_path=message[17],
            contact=contact,
            room=room,
            md5="",
            path="",
            file_size=0,
            file_name="",
            file_type="mp3",
            audio_text="",
            duration=0,
            user_info=user_info,
        )
        audio_dic = parser_audio(msg.parsed_content)
        audio_length = audio_dic.get("audio_length", 0)
        audio_text = audio_dic.get("audio_text", "")
        if not audio_text:
            try:
                packed_info_data_proto = packed_info_data_pb2.PackedInfoData()
                packed_info_data_proto.ParseFromString(message[14])
                packed_info_data = MessageToDict(packed_info_data_proto)
                audio_text = packed_info_data.get("info", {}).get("audioTxt", "")
            except Exception:
                audio_text = ""

        if not audio_text:
            # TODO: 语音转文字逻辑可能需要延迟处理或异步获取
            # print(f"音频消息没有音频文字，需要延迟处理一下")
            pass

        msg.audio_text = audio_text
        msg.duration = audio_length
        msg.set_file_name()
        return msg


class VideoMessageFactory(MessageFactory):
    """
    视频消息工厂。
    处理视频消息的解析与构建。
    """

    def create(
        self,
        message,
        user_info: UserInfo,
        db: "DatabaseService",
        contact: dict,
        room: dict,
    ):
        filename = ""
        try:
            # 兼容微信4.0.3+ 的视频命名方式
            packed_info_data_proto = packed_info_data_img2_pb2.PackedInfoDataImg2()
            packed_info_data_proto.ParseFromString(message[14])
            packed_info_data = MessageToDict(packed_info_data_proto)
            video_info = packed_info_data.get("videoInfo", {})
            filename = video_info.get("filename", "").strip().strip('"').strip()
        except Exception:
            filename = ""

        msg = VideoMessage(
            local_id=message[0],
            server_id=message[1],
            local_type=message[2],
            sort_seq=message[3],
            real_sender_id=message[4],
            create_time=message[5],
            status=message[6],
            upload_status=message[7],
            download_status=message[8],
            server_seq=message[9],
            origin_source=message[10],
            source=message[11],
            message_content=message[12],
            compress_content=message[13],
            packed_info_data=message[14],
            message_db_path=message[17],
            contact=contact,
            room=room,
            md5="",
            path="",
            file_size=0,
            file_name=filename,
            file_type="mp4",
            thumb_path="",
            duration=0,
            raw_md5="",
            user_info=user_info,
        )

        video_dic = parse_video(msg.parsed_content)
        msg.duration = video_dic.get("length", 0)
        msg.file_size = video_dic.get("size", 0)
        msg.md5 = video_dic.get("md5", "")
        msg.raw_md5 = video_dic.get("rawmd5", "")

        # [REFACTORED] Simplified path assignment logic
        video_path: Optional[Union[Path, str]] = None
        thumb_path: Optional[Union[Path, str]] = None
        month = msg.str_time[:7]  # e.g., "2025-01"

        if filename:
            # 微信 4.0.3+ 新版路径逻辑
            video_base = Path("msg/video") / month
            if db.user_info.data_dir:
                real_video_dir = Path(db.user_info.data_dir) / video_base
                real_video_path_raw = real_video_dir.joinpath(f"{filename}_raw.mp4")
                if real_video_path_raw.exists():
                    video_path = video_base / f"{filename}_raw.mp4"
                else:
                    video_path = video_base / f"{filename}.mp4"
            else:
                video_path = video_base / f"{filename}.mp4"

            thumb_path = video_base / f"{filename}.jpg"
        else:
            # 旧版路径查找逻辑
            video_path = db.get_video(msg.raw_md5, False)
            thumb_path = db.get_video(msg.raw_md5, True)
            if not video_path:
                video_path = db.get_video(msg.md5, False)
                thumb_path = db.get_video(msg.md5, True)

        msg.path = str(video_path) if video_path else ""
        msg.thumb_path = str(thumb_path) if thumb_path else ""

        return msg


class EmojiMessageFactory(MessageFactory):
    """
    表情消息工厂。
    处理表情消息的解析与构建。
    """

    def create(
        self,
        message,
        user_info: UserInfo,
        db: "DatabaseService",
        contact: dict,
        room: dict,
    ):
        msg = EmojiMessage(
            local_id=message[0],
            server_id=message[1],
            local_type=message[2],
            sort_seq=message[3],
            real_sender_id=message[4],
            create_time=message[5],
            status=message[6],
            upload_status=message[7],
            download_status=message[8],
            server_seq=message[9],
            origin_source=message[10],
            source=message[11],
            message_content=message[12],
            compress_content=message[13],
            packed_info_data=message[14],
            message_db_path=message[17],
            contact=contact,
            room=room,
            md5="",
            path="",
            thumb_path="",
            file_size=0,
            file_name="",
            file_type="gif",  # Default, can be other types
            url="",
            thumb_url="",
            description="",
            user_info=user_info,
        )
        emoji_info = parser_emoji(msg.parsed_content)
        msg.md5 = emoji_info.get("md5", "")
        msg.url = emoji_info.get("url")
        if not msg.url and msg.md5:
            msg.url = db.get_emoji_url(msg.md5)
        msg.description = emoji_info.get("desc", "")
        return msg


class LinkMessageFactory(MessageFactory):
    """
    链接消息工厂。
    处理链接消息的解析与构建。
    """

    def create(
        self,
        message,
        user_info: UserInfo,
        db: "DatabaseService",
        contact: dict,
        room: dict,
    ):
        msg = LinkMessage(
            local_id=message[0],
            server_id=message[1],
            local_type=message[2],
            sort_seq=message[3],
            real_sender_id=message[4],
            create_time=message[5],
            status=message[6],
            upload_status=message[7],
            download_status=message[8],
            server_seq=message[9],
            origin_source=message[10],
            source=message[11],
            message_content=message[12],
            compress_content=message[13],
            packed_info_data=message[14],
            message_db_path=message[17],
            contact=contact,
            room=room,
            href="",
            title="",
            description="",
            cover_path="",
            cover_url="",
            app_name="",
            app_icon="",
            app_id="",
            user_info=user_info,
        )

        info = {}
        if message[2] in {
            MessageType.LinkMessage,
            MessageType.LinkMessage2,
            MessageType.Music,
            MessageType.LinkMessage4,
            MessageType.LinkMessage5,
            MessageType.LinkMessage6,
        }:
            info = parser_link(msg.parsed_content)
            if message[2] == MessageType.Music:
                msg.type = MessageType.Music
            source_username = info.get("sourceusername")
            if source_username:
                source_contact = db.get_contact_by_username(source_username)
                if source_contact:
                    msg.app_name = source_contact.display_name
                    msg.app_icon = source_contact.small_head_url
                msg.app_id = source_username
        elif message[2] in {MessageType.Applet, MessageType.Applet2}:
            info = parser_applet(msg.parsed_content)
            msg.type = MessageType.Applet
            msg.app_icon = info.get("app_icon", "")

        msg.title = info.get("title", "")
        msg.href = info.get("url", "")
        msg.app_name = info.get("appname", msg.app_name or "")
        msg.app_id = info.get("appid", msg.app_id or "")
        msg.description = info.get("desc", "")
        msg.cover_url = info.get("cover_url", "")

        return msg


class BusinessCardMessageFactory(MessageFactory):
    """
    名片消息工厂。
    处理名片消息的解析与构建。
    """

    def create(
        self,
        message,
        user_info: UserInfo,
        db: "DatabaseService",
        contact: dict,
        room: dict,
    ):
        msg = BusinessCardMessage(
            local_id=message[0],
            server_id=message[1],
            local_type=message[2],
            sort_seq=message[3],
            real_sender_id=message[4],
            create_time=message[5],
            status=message[6],
            upload_status=message[7],
            download_status=message[8],
            server_seq=message[9],
            origin_source=message[10],
            source=message[11],
            message_content=message[12],
            compress_content=message[13],
            packed_info_data=message[14],
            message_db_path=message[17],
            contact=contact,
            room=room,
            is_open_im=message[2] == MessageType.OpenIMBCard,
            username="",
            nickname="",
            alias="",
            province="",
            city="",
            sign="",
            sex=0,
            small_head_url="",
            big_head_url="",
            open_im_desc="",
            open_im_desc_icon="",
            user_info=user_info,
        )
        info = parser_business(msg.parsed_content)
        msg.username = info.get("username", "")
        msg.nickname = info.get("nickname", "")
        msg.alias = info.get("alias", "")
        msg.small_head_url = info.get("smallheadimgurl", "")
        msg.big_head_url = info.get("bigheadimgurl", "")
        msg.sex = int(info.get("sex", 0))
        msg.sign = info.get("sign", "")
        msg.province = info.get("province", "")
        msg.city = info.get("city", "")
        msg.open_im_desc = info.get("openimdesc", "")
        msg.open_im_desc_icon = info.get("openimdescicon", "")
        return msg


class VoipMessageFactory(MessageFactory):
    """
    语音/视频通话消息工厂。
    处理语音/视频通话消息的解析与构建。
    """

    def create(
        self,
        message,
        user_info: UserInfo,
        db: "DatabaseService",
        contact: dict,
        room: dict,
    ):
        msg = VoipMessage(
            local_id=message[0],
            server_id=message[1],
            local_type=message[2],
            sort_seq=message[3],
            real_sender_id=message[4],
            create_time=message[5],
            status=message[6],
            upload_status=message[7],
            download_status=message[8],
            server_seq=message[9],
            origin_source=message[10],
            source=message[11],
            message_content=message[12],
            compress_content=message[13],
            packed_info_data=message[14],
            message_db_path=message[17],
            contact=contact,
            room=room,
            invite_type=0,
            display_content="",
            duration=0,
            user_info=user_info,
        )
        info = parser_voip(msg.parsed_content)
        msg.invite_type = info.get("invite_type", 0)
        msg.display_content = info.get("display_content", "")
        msg.duration = info.get("duration", 0)
        return msg


class MergedMessageFactory(MessageFactory):
    """
    合并转发消息工厂。
    处理合并转发消息的解析与构建。
    """

    def create(
        self,
        message,
        user_info: UserInfo,
        db: "DatabaseService",
        contact: dict,
        room: dict,
    ):
        msg = MergedMessage(
            local_id=message[0],
            server_id=message[1],
            local_type=message[2],
            sort_seq=message[3],
            real_sender_id=message[4],
            create_time=message[5],
            status=message[6],
            upload_status=message[7],
            download_status=message[8],
            server_seq=message[9],
            origin_source=message[10],
            source=message[11],
            message_content=message[12],
            compress_content=message[13],
            packed_info_data=message[14],
            message_db_path=message[17],
            contact=contact,
            room=room,
            title="",
            description="",
            messages=[],
            level=0,
            user_info=user_info,
        )

        info = parser_merged_messages(
            user_info, msg.parsed_content, "", msg.contact.username, message[5]
        )

        dir0 = ""
        try:
            packed_info_data_proto = packed_info_data_merged_pb2.PackedInfoData()
            packed_info_data_proto.ParseFromString(message[14])
            packed_info_data = MessageToDict(packed_info_data_proto)
            dir0 = packed_info_data.get("info", {}).get("dir", "")
        except Exception:
            dir0 = ""

        month = msg.str_time[:7]

        if not dir0 and user_info.data_dir:
            rec_dir = (
                Path(user_info.data_dir)
                / "msg"
                / "attach"
                / hashlib.md5(msg.contact.username.encode("utf-8")).hexdigest()
                / month
                / "Rec"
            )
            if rec_dir.exists():
                for file in rec_dir.iterdir():
                    if file.is_dir() and file.name.startswith(f"{msg.local_id}_"):
                        dir0 = file.name
                        break

        msg.title = info.get("title", "")
        msg.description = info.get("desc", "")
        msg.messages = info.get("messages", [])

        def parser_merged(merged_messages, level_prefix):
            attach_base = Path("msg/attach")
            wxid_md5 = hashlib.md5(msg.contact.username.encode("utf-8")).hexdigest()

            for index, inner_msg in enumerate(merged_messages):
                inner_msg.room = msg.room
                current_level = f"{level_prefix}{'_' if level_prefix else ''}{index}"

                if inner_msg.local_type == MessageType.Image:
                    if dir0:
                        inner_msg.path = str(
                            attach_base
                            / wxid_md5
                            / month
                            / "Rec"
                            / dir0
                            / "Img"
                            / current_level
                        )
                        inner_msg.thumb_path = str(
                            attach_base
                            / wxid_md5
                            / month
                            / "Rec"
                            / dir0
                            / "Img"
                            / f"{current_level}_t"
                        )
                    else:
                        path = db.get_image(
                            "",
                            inner_msg.md5,
                            inner_msg,
                            "",
                            False,
                            msg.contact.username,
                        )
                        thumb_path = db.get_image(
                            "", inner_msg.md5, inner_msg, "", True, msg.contact.username
                        )
                        inner_msg.path = str(path) if path else ""
                        inner_msg.thumb_path = str(thumb_path) if thumb_path else ""

                elif inner_msg.local_type == MessageType.Video:
                    if dir0:
                        inner_msg.path = str(
                            attach_base
                            / wxid_md5
                            / month
                            / "Rec"
                            / dir0
                            / "V"
                            / f"{current_level}.mp4"
                        )
                        inner_msg.thumb_path = str(
                            attach_base
                            / wxid_md5
                            / month
                            / "Rec"
                            / dir0
                            / "Img"
                            / f"{current_level}_t"
                        )
                    else:
                        path = db.get_video(inner_msg.md5, False)
                        thumb_path = db.get_video(inner_msg.md5, True)
                        inner_msg.path = str(path) if path else ""
                        inner_msg.thumb_path = str(thumb_path) if thumb_path else ""

                elif inner_msg.local_type == MessageType.File:
                    if dir0:
                        inner_msg.path = str(
                            attach_base
                            / wxid_md5
                            / month
                            / "Rec"
                            / dir0
                            / "F"
                            / current_level
                            / inner_msg.file_name
                        )
                    else:
                        path = db.get_file(inner_msg.md5)
                        inner_msg.path = str(path) if path else ""

                elif inner_msg.local_type == MessageType.MergedMessages:
                    parser_merged(inner_msg.messages, current_level)

        parser_merged(msg.messages, "")
        return msg


class WeChatVideoMessageFactory(MessageFactory):
    """
    微信视频号消息工厂。
    """

    def create(
        self,
        message,
        user_info: UserInfo,
        db: "DatabaseService",
        contact: dict,
        room: dict,
    ):
        msg = WeChatVideoMessage(
            local_id=message[0],
            server_id=message[1],
            local_type=message[2],
            sort_seq=message[3],
            real_sender_id=message[4],
            create_time=message[5],
            status=message[6],
            upload_status=message[7],
            download_status=message[8],
            server_seq=message[9],
            origin_source=message[10],
            source=message[11],
            message_content=message[12],
            compress_content=message[13],
            packed_info_data=message[14],
            message_db_path=message[17],
            contact=contact,
            room=room,
            url="",
            publisher_nickname="",
            publisher_avatar="",
            description="",
            media_count=1,
            cover_url="",
            thumb_url="",
            cover_path="",
            width=0,
            height=0,
            duration=0,
            user_info=user_info,
        )
        info = parser_wechat_video(msg.parsed_content)
        msg.publisher_nickname = info.get("sourcedisplayname", "")
        msg.publisher_avatar = info.get("weappiconurl", "")
        msg.description = info.get("title", "")
        msg.cover_url = info.get("cover", "")
        return msg


class PositionMessageFactory(MessageFactory):
    """
    位置消息工厂。
    """

    def create(
        self,
        message,
        user_info: UserInfo,
        db: "DatabaseService",
        contact: dict,
        room: dict,
    ):
        msg = PositionMessage(
            local_id=message[0],
            server_id=message[1],
            local_type=message[2],
            sort_seq=message[3],
            real_sender_id=message[4],
            create_time=message[5],
            status=message[6],
            upload_status=message[7],
            download_status=message[8],
            server_seq=message[9],
            origin_source=message[10],
            source=message[11],
            message_content=message[12],
            compress_content=message[13],
            packed_info_data=message[14],
            message_db_path=message[17],
            contact=contact,
            room=room,
            x=0.0,
            y=0.0,
            poiname="",
            label="",
            scale=0,
            user_info=user_info,
        )
        info = parser_position(msg.parsed_content)
        try:
            msg.x = float(info.get("x", 0.0))
            msg.y = float(info.get("y", 0.0))
            msg.scale = int(info.get("scale", 0))
        except (ValueError, TypeError):
            pass  # Keep default values if conversion fails
        msg.poiname = info.get("poiname", "")
        msg.label = info.get("label", "")
        return msg


class QuoteMessageFactory(MessageFactory):
    """
    引用消息工厂。
    """

    def create(
        self,
        message,
        user_info: UserInfo,
        db: "DatabaseService",
        contact: dict,
        room: dict,
    ):
        msg = QuoteMessage(
            local_id=message[0],
            server_id=message[1],
            local_type=message[2],
            sort_seq=message[3],
            real_sender_id=message[4],
            create_time=message[5],
            status=message[6],
            upload_status=message[7],
            download_status=message[8],
            server_seq=message[9],
            origin_source=message[10],
            source=message[11],
            message_content=message[12],
            compress_content=message[13],
            packed_info_data=message[14],
            message_db_path=message[17],
            contact=contact,
            room=room,
            content="",
            quote_message=None,
            user_info=user_info,
        )
        info = parser_reply(msg.parsed_content)

        quote_svrid = info.get("svrid", "")
        chat_user = room.username if room else contact.username
        quote_message_data = db.get_message_by_server_id(
            quote_svrid, msg.message_db_path, chat_user
        )

        if quote_message_data:
            quote_sender_id = quote_message_data[4]
            quote_contact = db.get_contact_by_sender_id(
                quote_sender_id, msg.message_db_path
            )
            quote_factory = FACTORY_REGISTRY.get(
                quote_message_data[2], UnknownMessageFactory()
            )
            msg.quote_message = quote_factory.create(
                quote_message_data, user_info, db, quote_contact, room
            )
        else:
            # [REFACTORED] Changed print to a comment, suggesting logger usage
            # logger.warning(f"Quoted message not found. svrid: {quote_svrid}, xml: {msg.parsed_content}")
            msg.quote_message = None

        msg.content = info.get("text", "")
        return msg


class SystemMessageFactory(MessageFactory):
    """
    系统消息工厂。
    """

    def create(
        self,
        message,
        user_info: UserInfo,
        db: "DatabaseService",
        contact: dict,
        room: dict,
    ):
        msg = TextMessage(
            local_id=message[0],
            server_id=message[1],
            local_type=message[2],
            sort_seq=message[3],
            real_sender_id=message[4],
            create_time=message[5],
            status=message[6],
            upload_status=message[7],
            download_status=message[8],
            server_seq=message[9],
            origin_source=message[10],
            source=message[11],
            message_content=message[12],
            compress_content=message[13],
            packed_info_data=message[14],
            message_db_path=message[17],
            contact=contact,
            room=room,
            content="",
            user_info=user_info,
        )

        message_content = message[12]
        if isinstance(message[12], bytes):
            message_content = decompress(message[12])

        # [FIXED] Safely handle chatroom system messages and XML parsing
        try:
            # Strip chatroom prefix if it exists
            if "@chatroom:" in message_content:
                parts = message_content.split("@chatroom:", 1)
                if len(parts) > 1:
                    message_content = parts[1].strip()

            # Attempt to parse as XML and convert to JSON string for readability
            dic = xmltodict.parse(message_content)
            message_content = json.dumps(dic, ensure_ascii=False)
        except Exception:
            # If it's not valid XML or fails for any reason, use the content as is
            pass

        msg.content = message_content
        return msg


class TransferMessageFactory(MessageFactory):
    """
    转账消息工厂。
    """

    def create(
        self,
        message,
        user_info: UserInfo,
        db: "DatabaseService",
        contact: dict,
        room: dict,
    ):
        msg = TransferMessage(
            local_id=message[0],
            server_id=message[1],
            local_type=message[2],
            sort_seq=message[3],
            real_sender_id=message[4],
            create_time=message[5],
            status=message[6],
            upload_status=message[7],
            download_status=message[8],
            server_seq=message[9],
            origin_source=message[10],
            source=message[11],
            message_content=message[12],
            compress_content=message[13],
            packed_info_data=message[14],
            message_db_path=message[17],
            contact=contact,
            room=room,
            pay_subtype=0,
            fee_desc="",
            receiver_username="",
            pay_memo="",
            user_info=user_info,
        )
        info = parser_transfer(msg.parsed_content)
        msg.pay_subtype = info.get("pay_subtype", 0)
        msg.fee_desc = info.get("fee_desc", "")
        msg.receiver_username = info.get("receiver_username", "")
        msg.pay_memo = info.get("pay_memo", "")
        return msg


class RedEnvelopeMessageFactory(MessageFactory):
    """
    红包消息工厂。
    """

    def create(
        self,
        message,
        user_info: UserInfo,
        db: "DatabaseService",
        contact: dict,
        room: dict,
    ):
        msg = RedEnvelopeMessage(
            local_id=message[0],
            server_id=message[1],
            local_type=message[2],
            sort_seq=message[3],
            real_sender_id=message[4],
            create_time=message[5],
            status=message[6],
            upload_status=message[7],
            download_status=message[8],
            server_seq=message[9],
            origin_source=message[10],
            source=message[11],
            message_content=message[12],
            compress_content=message[13],
            packed_info_data=message[14],
            message_db_path=message[17],
            contact=contact,
            room=room,
            title="",
            icon_url="",
            inner_type=0,
            user_info=user_info,
        )
        info = parser_red_envelop(msg.parsed_content)
        msg.title = info.get("title", "")
        msg.icon_url = info.get("icon_url", "")
        msg.inner_type = info.get("inner_type", 0)
        return msg


class FileMessageFactory(MessageFactory):
    """
    文件消息工厂。
    """

    def create(
        self,
        message,
        user_info: UserInfo,
        db: "DatabaseService",
        contact: dict,
        room: dict,
    ):
        msg = FileMessage(
            local_id=message[0],
            server_id=message[1],
            local_type=message[2],
            sort_seq=message[3],
            real_sender_id=message[4],
            create_time=message[5],
            status=message[6],
            upload_status=message[7],
            download_status=message[8],
            server_seq=message[9],
            origin_source=message[10],
            source=message[11],
            message_content=message[12],
            compress_content=message[13],
            packed_info_data=message[14],
            message_db_path=message[17],
            contact=contact,
            room=room,
            path="",
            md5="",
            file_type="",
            file_name="",
            file_size="",
            user_info=user_info,
        )
        info = parser_file(msg.parsed_content)
        md5 = info.get("md5", "")
        filename = info.get("file_name", "")

        if not filename:
            try:
                # Try parsing packed info for filename as a fallback
                packed_info_data_proto = packed_info_data_img2_pb2.PackedInfoDataImg2()
                packed_info_data_proto.ParseFromString(message[14])
                packed_info_data = MessageToDict(packed_info_data_proto)
                file_info = packed_info_data.get("fileInfo", {}).get("fileInfo", {})
                filename = file_info.get("filename", "").strip()
            except Exception:
                pass

        file_path: Optional[Union[Path, str]] = None
        if filename:
            month = msg.str_time[:7]
            file_path = Path("msg/file") / month / filename
        elif md5:
            file_path = db.get_file(md5)

        msg.path = str(file_path) if file_path else ""
        msg.file_name = filename
        msg.file_size = info.get("file_size", "0")
        msg.file_type = info.get("file_type", "")
        msg.md5 = md5
        return msg


class FavNoteMessageFactory(MessageFactory):
    """
    收藏笔记消息工厂。
    """

    def create(
        self,
        message,
        user_info: UserInfo,
        db: "DatabaseService",
        contact: dict,
        room: dict,
    ):
        msg = FavNoteMessage(
            local_id=message[0],
            server_id=message[1],
            local_type=message[2],
            sort_seq=message[3],
            real_sender_id=message[4],
            create_time=message[5],
            status=message[6],
            upload_status=message[7],
            download_status=message[8],
            server_seq=message[9],
            origin_source=message[10],
            source=message[11],
            message_content=message[12],
            compress_content=message[13],
            packed_info_data=message[14],
            message_db_path=message[17],
            contact=contact,
            room=room,
            title="",
            description="",
            record_item="",
            user_info=user_info,
        )
        info = parser_favorite_note(msg.parsed_content)
        msg.title = info.get("title", "")
        msg.description = info.get("desc", "")
        msg.record_item = info.get("recorditem", "")
        return msg


class PatMessageFactory(MessageFactory):
    """
    拍一拍消息工厂。
    """

    def create(
        self,
        message,
        user_info: UserInfo,
        db: "DatabaseService",
        contact: dict,
        room: dict,
    ):
        msg = PatMessage(
            local_id=message[0],
            server_id=message[1],
            local_type=message[2],
            sort_seq=message[3],
            real_sender_id=message[4],
            create_time=message[5],
            status=message[6],
            upload_status=message[7],
            download_status=message[8],
            server_seq=message[9],
            origin_source=message[10],
            source=message[11],
            message_content=message[12],
            compress_content=message[13],
            packed_info_data=message[14],
            message_db_path=message[17],
            contact=contact,  # Initial contact (system)
            room=room,
            title="",
            from_username="",
            patted_username="",
            chat_username="",
            template="",
            user_info=user_info,
        )
        info = parser_pat(msg.parsed_content)
        msg.title = info.get("title", "")
        msg.from_username = info.get("fromusername", "")
        msg.patted_username = info.get("pattedusername", "")
        msg.chat_username = info.get("chatusername", "")
        msg.template = info.get("template", "")

        # '拍一拍' 消息的发送者是实际拍人的人, 而不是'系统消息'
        # 需要根据 from_username 重新获取正确的联系人信息
        if msg.from_username:
            pat_contact = db.get_contact_by_username(msg.from_username)
            if pat_contact:
                msg.contact = pat_contact

        return msg


# 工厂注册表
FACTORY_REGISTRY = {
    -1: UnknownMessageFactory(),
    MessageType.Text: TextMessageFactory(),
    MessageType.Image: ImageMessageFactory(),
    MessageType.Audio: AudioMessageFactory(),
    MessageType.Video: VideoMessageFactory(),
    MessageType.Emoji: EmojiMessageFactory(),
    MessageType.System: SystemMessageFactory(),
    MessageType.LinkMessage: LinkMessageFactory(),
    MessageType.LinkMessage2: LinkMessageFactory(),
    MessageType.Music: LinkMessageFactory(),
    MessageType.LinkMessage4: LinkMessageFactory(),
    MessageType.LinkMessage5: LinkMessageFactory(),
    MessageType.LinkMessage6: LinkMessageFactory(),
    MessageType.Applet: LinkMessageFactory(),
    MessageType.Applet2: LinkMessageFactory(),
    MessageType.File: FileMessageFactory(),
    MessageType.FileWait: FileMessageFactory(),
    MessageType.Position: PositionMessageFactory(),
    MessageType.Quote: QuoteMessageFactory(),
    MessageType.Pat: PatMessageFactory(),
    MessageType.RedEnvelope: RedEnvelopeMessageFactory(),
    MessageType.Transfer: TransferMessageFactory(),
    MessageType.Voip: VoipMessageFactory(),
    MessageType.FavNote: FavNoteMessageFactory(),
    MessageType.WeChatVideo: WeChatVideoMessageFactory(),
    MessageType.BusinessCard: BusinessCardMessageFactory(),
    MessageType.OpenIMBCard: BusinessCardMessageFactory(),
    MessageType.MergedMessages: MergedMessageFactory(),
    MessageType.PublicAnnouncement: SystemMessageFactory(),
}

if __name__ == "__main__":
    # 创建 TextMessage 实例
    msg_instance = TextMessage(
        local_id=107,
        server_id=7733522398990171519,
        local_type=MessageType.Text,
        sort_seq=1740373617000,
        real_sender_id=1235,
        create_time=1740373617,
        status=3,
        upload_status=0,
        download_status=0,
        server_seq=842928924,
        origin_source=2,
        source="",
        message_content="wxid_4431474314712:\n我不在",
        compress_content="",
        packed_info_data=None,
        content="我不在",
        message_db_path="message/message_0.db",
        room=None,
        contact=None,
        user_info=None,  # For testing purpose
    )
    print(msg_instance.to_json())
