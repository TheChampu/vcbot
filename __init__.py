
import asyncio
import os
import re
import traceback
from time import time
from traceback import format_exc

from pytgcalls import PyTgCalls
from pytgcalls.types import MediaStream, AudioQuality, VideoQuality, StreamEnded
from pytgcalls.exceptions import NoActiveGroupCall, NotInCallError
from telethon.errors.rpcerrorlist import (
    ParticipantJoinMissingError,
    ChatSendMediaForbiddenError,
)
from pyChampu import HNDLR, LOGS, asst, udB, vcClient
from pyChampu._misc._decorators import compile_pattern
from pyChampu.fns.helper import (
    bash,
    downloader,
    inline_mention,
    mediainfo,
    time_formatter,
)
from pyChampu.fns.admins import admin_check
from pyChampu.fns.tools import is_url_ok
from pyChampu.fns.ytdl import get_videos_link
from pyChampu._misc import owner_and_sudos, sudoers
from pyChampu._misc._assistant import in_pattern
from pyChampu._misc._wrappers import eod, eor
from pyChampu.version import __version__ as UltVer
from telethon import events
from telethon.tl import functions, types
from telethon.utils import get_display_name

# Backward-compatibility alias so existing exception-handlers keep working
GroupCallNotFoundError = NoActiveGroupCall

try:
    from yt_dlp import YoutubeDL
except ImportError:
    YoutubeDL = None
    LOGS.error("'yt-dlp' not found!")

try:
    from youtubesearchpython import VideosSearch
except ImportError:
    VideosSearch = None

from strings import get_string

# ------------------------------------------------------------------
# YouTube API constants (used by vcbot search helpers)
# ------------------------------------------------------------------
API_URL = (
    os.environ.get("SHRUTI_API_URL")
    or os.environ.get("YT_API_URL")
    or udB.get_key("YT_API_URL")
    or "http://api01.shrutibots.site"
)
API_KEY = os.environ.get("SHRUTI_API_KEY", "ShrutiBotsP3A8xKwYFafG6SuSLTIM")

# ------------------------------------------------------------------
# Global state
# ------------------------------------------------------------------
asstUserName = asst.me.username
LOG_CHANNEL = udB.get_key("LOG_CHANNEL")
ACTIVE_CALLS, VC_QUEUE = [], {}
MSGID_CACHE, VIDEO_ON = {}, {}
CLIENTS = {}            # {chat_id: GroupCallWrapper}

# Internal tracking for PyTgCalls 2.x integration
VC_PLAYOUT_CALLBACKS = {}   # {chat_id: async callback(call, source, mtype)}
VC_STREAM_FILES = {}        # {chat_id: current_file_path}

# ------------------------------------------------------------------
# Initialize the single global PyTgCalls 2.x client
# ------------------------------------------------------------------
pytgcalls_client = None
if vcClient:
    try:
        pytgcalls_client = PyTgCalls(vcClient)
        pytgcalls_client.start()
        LOGS.info("PyTgCalls 2.x client started successfully.")
    except Exception as _pytgcalls_err:
        LOGS.exception(_pytgcalls_err)


# ------------------------------------------------------------------
# GroupCallWrapper — adapts PyTgCalls 2.x to the legacy GroupCall API
# ------------------------------------------------------------------

class GroupCallWrapper:
    """
    Wraps the global PyTgCalls 2.x client and exposes the legacy
    pytgcalls 0.x GroupCall interface that existing VCBot command
    files (play.py, controls.py, vctools.py, videoplay.py, etc.)
    depend on — so none of those files need modification.
    """

    def __init__(self, chat_id: int):
        self._chat_id = chat_id

    # ── State ──────────────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        return self._chat_id in ACTIVE_CALLS

    # ── Legacy callback registration ───────────────────────────────

    def on_network_status_changed(self, callback):
        """No-op: connectivity is tracked via ACTIVE_CALLS."""
        pass

    def on_playout_ended(self, callback):
        """Register the playout-ended callback for this chat."""
        VC_PLAYOUT_CALLBACKS[self._chat_id] = callback

    # ── Call control ───────────────────────────────────────────────

    async def join(self, chat_id: int):
        """Join the voice chat without starting any media stream."""
        if pytgcalls_client is None:
            raise RuntimeError("PyTgCalls client not initialized (no VC session).")
        await pytgcalls_client.play(chat_id, None)
        if chat_id not in ACTIVE_CALLS:
            ACTIVE_CALLS.append(chat_id)

    async def start_audio(self, path: str):
        """Start streaming audio-only from *path* (file or URL)."""
        if pytgcalls_client is None:
            return
        VC_STREAM_FILES[self._chat_id] = path
        await pytgcalls_client.play(
            self._chat_id,
            MediaStream(path, video_flags=MediaStream.Flags.IGNORE),
        )
        if self._chat_id not in ACTIVE_CALLS:
            ACTIVE_CALLS.append(self._chat_id)

    async def start_video(self, path: str, with_audio: bool = True):
        """Start streaming video (and optionally audio) from *path*."""
        if pytgcalls_client is None:
            return
        VC_STREAM_FILES[self._chat_id] = path
        audio_flags = (
            MediaStream.Flags.AUTO_DETECT if with_audio else MediaStream.Flags.IGNORE
        )
        await pytgcalls_client.play(
            self._chat_id,
            MediaStream(
                path,
                audio_parameters=AudioQuality.HIGH,
                video_parameters=VideoQuality.HD_720p,
                audio_flags=audio_flags,
            ),
        )
        if self._chat_id not in ACTIVE_CALLS:
            ACTIVE_CALLS.append(self._chat_id)

    async def stop(self):
        """Leave the voice chat and clean up all state for this chat."""
        if pytgcalls_client is None:
            return
        try:
            await pytgcalls_client.leave_call(self._chat_id)
        except Exception:
            pass
        if self._chat_id in ACTIVE_CALLS:
            ACTIVE_CALLS.remove(self._chat_id)
        VC_PLAYOUT_CALLBACKS.pop(self._chat_id, None)
        VC_STREAM_FILES.pop(self._chat_id, None)

    async def stop_video(self):
        """Stop the video portion (audio-only continues)."""
        # pytgcalls 2.x has no separate stop_video; just update local state.
        VIDEO_ON.pop(self._chat_id, None)

    async def set_my_volume(self, volume: int):
        if pytgcalls_client is None:
            return
        volume = max(1, min(200, volume))
        await pytgcalls_client.change_volume_call(self._chat_id, volume)

    async def set_is_mute(self, muted: bool):
        if pytgcalls_client is None:
            return
        if muted:
            await pytgcalls_client.mute(self._chat_id)
        else:
            await pytgcalls_client.unmute(self._chat_id)

    async def set_pause(self, paused: bool):
        if pytgcalls_client is None:
            return
        if paused:
            await pytgcalls_client.pause(self._chat_id)
        else:
            await pytgcalls_client.resume(self._chat_id)

    async def reconnect(self):
        """Reconnect to the voice chat (used by the .rejoin command)."""
        if pytgcalls_client is None:
            raise NotInCallError("PyTgCalls client not initialized.")
        if self._chat_id in ACTIVE_CALLS:
            ACTIVE_CALLS.remove(self._chat_id)
        await pytgcalls_client.play(self._chat_id, None)
        if self._chat_id not in ACTIVE_CALLS:
            ACTIVE_CALLS.append(self._chat_id)

    def restart_playout(self):
        """Re-play the current song from the beginning."""
        path = VC_STREAM_FILES.get(self._chat_id)
        if path and pytgcalls_client:
            asyncio.create_task(
                pytgcalls_client.play(
                    self._chat_id,
                    MediaStream(path, video_flags=MediaStream.Flags.IGNORE),
                )
            )


# ------------------------------------------------------------------
# Global StreamEnded handler — automatically drives the VC queue
# ------------------------------------------------------------------

if pytgcalls_client is not None:

    @pytgcalls_client.on_update()
    async def _on_stream_ended(update):
        if not isinstance(update, StreamEnded):
            return
        chat_id = update.chat_id
        source = VC_STREAM_FILES.get(chat_id, "")
        callback = VC_PLAYOUT_CALLBACKS.get(chat_id)
        if callback:
            try:
                # Legacy callback signature: (call, source, mtype)
                await callback(None, source, None)
            except Exception as _cb_err:
                LOGS.exception(_cb_err)


# ------------------------------------------------------------------
# VC_AUTHS
# ------------------------------------------------------------------


def VC_AUTHS():
    _vcsudos = udB.get_key("VC_SUDOS") or []
    return [int(a) for a in [*owner_and_sudos(), *_vcsudos]]


# ------------------------------------------------------------------
# Player
# ------------------------------------------------------------------


class Player:
    def __init__(self, chat, event=None, video=False):
        self._chat = chat
        self._current_chat = event.chat_id if event else LOG_CHANNEL
        self._video = video
        if CLIENTS.get(chat):
            self.group_call = CLIENTS[chat]
        else:
            self.group_call = GroupCallWrapper(chat)
            CLIENTS[chat] = self.group_call

    async def make_vc_active(self):
        try:
            await vcClient(
                functions.phone.CreateGroupCallRequest(
                    self._chat, title="🎧 Champu Music 🎶"
                )
            )
        except Exception as e:
            LOGS.exception(e)
            return False, e
        return True, None

    async def startCall(self):
        if VIDEO_ON:
            for chats in VIDEO_ON:
                await VIDEO_ON[chats].stop()
            VIDEO_ON.clear()
            await asyncio.sleep(3)
        if self._video:
            for chats in list(CLIENTS):
                if chats != self._chat:
                    await CLIENTS[chats].stop()
                    del CLIENTS[chats]
            VIDEO_ON.update({self._chat: self.group_call})
        if self._chat not in ACTIVE_CALLS:
            try:
                self.group_call.on_network_status_changed(self.on_network_changed)
                self.group_call.on_playout_ended(self.playout_ended_handler)
                await self.group_call.join(self._chat)
            except GroupCallNotFoundError as er:
                LOGS.info(er)
                dn, err = await self.make_vc_active()
                if err:
                    return False, err
            except Exception as e:
                LOGS.exception(e)
                return False, e
        return True, None

    async def on_network_changed(self, call, is_connected):
        chat = self._chat
        if is_connected:
            if chat not in ACTIVE_CALLS:
                ACTIVE_CALLS.append(chat)
        elif chat in ACTIVE_CALLS:
            ACTIVE_CALLS.remove(chat)

    async def playout_ended_handler(self, call, source, mtype):
        if source and os.path.exists(source):
            os.remove(source)
        await self.play_from_queue()

    async def play_from_queue(self):
        chat_id = self._chat
        if chat_id in VIDEO_ON:
            await self.group_call.stop_video()
            VIDEO_ON.pop(chat_id, None)
        try:
            song, title, link, thumb, from_user, pos, dur = await get_from_queue(
                chat_id
            )
            try:
                await self.group_call.start_audio(song)
            except ParticipantJoinMissingError:
                await self.vc_joiner()
                await self.group_call.start_audio(song)
            if MSGID_CACHE.get(chat_id):
                await MSGID_CACHE[chat_id].delete()
                del MSGID_CACHE[chat_id]
            text = (
                f"<strong>🎧 Now playing #{pos}: <a href={link}>{title}</a>"
                f"\n⏰ Duration:</strong> <code>{dur}</code>"
                f"\n👤 <strong>Requested by:</strong> {from_user}"
            )
            try:
                xx = await vcClient.send_message(
                    self._current_chat,
                    text,
                    file=thumb,
                    link_preview=False,
                    parse_mode="html",
                )
            except ChatSendMediaForbiddenError:
                xx = await vcClient.send_message(
                    self._current_chat, text, link_preview=False, parse_mode="html"
                )
            MSGID_CACHE.update({chat_id: xx})
            VC_QUEUE[chat_id].pop(pos)
            if not VC_QUEUE[chat_id]:
                VC_QUEUE.pop(chat_id)

        except (IndexError, KeyError):
            await self.group_call.stop()
            CLIENTS.pop(self._chat, None)
            await vcClient.send_message(
                self._current_chat,
                f"• Successfully Left Vc : <code>{chat_id}</code> •",
                parse_mode="html",
            )
        except Exception as er:
            LOGS.exception(er)
            await vcClient.send_message(
                self._current_chat,
                f"<strong>ERROR:</strong> <code>{format_exc()}</code>",
                parse_mode="html",
            )

    async def vc_joiner(self):
        chat_id = self._chat
        done, err = await self.startCall()

        if done:
            await vcClient.send_message(
                self._current_chat,
                f"• Joined VC in <code>{chat_id}</code>",
                parse_mode="html",
            )
            return True
        await vcClient.send_message(
            self._current_chat,
            f"<strong>ERROR while Joining Vc -</strong> <code>{chat_id}</code> :\n<code>{err}</code>",
            parse_mode="html",
        )
        return False


# ------------------------------------------------------------------


def vc_asst(dec, **kwargs):
    def ult(func):
        kwargs["func"] = (
            lambda e: not e.is_private and not e.via_bot_id and not e.fwd_from
        )
        handler = udB.get_key("VC_HNDLR") or HNDLR
        kwargs["pattern"] = compile_pattern(dec, handler)
        vc_auth = kwargs.get("vc_auth", True)
        key = udB.get_key("VC_AUTH_GROUPS") or {}
        if "vc_auth" in kwargs:
            del kwargs["vc_auth"]

        async def vc_handler(e):
            VCAUTH = list(key.keys())
            if not (
                (e.out)
                or (e.sender_id in VC_AUTHS())
                or (vc_auth and e.chat_id in VCAUTH)
            ):
                return
            elif vc_auth and key.get(e.chat_id):
                cha, adm = key.get(e.chat_id), key[e.chat_id]["admins"]
                if adm and not (await admin_check(e)):
                    return
            try:
                await func(e)
            except Exception:
                LOGS.exception(Exception)
                await asst.send_message(
                    LOG_CHANNEL,
                    f"VC Error - <code>{UltVer}</code>\n\n<code>{e.text}</code>\n\n<code>{format_exc()}</code>",
                    parse_mode="html",
                )

        vcClient.add_event_handler(
            vc_handler,
            events.NewMessage(**kwargs),
        )

    return ult


# ------------------------------------------------------------------


def add_to_queue(chat_id, song, song_name, link, thumb, from_user, duration):
    try:
        n = sorted(list(VC_QUEUE[chat_id].keys()))
        play_at = n[-1] + 1
    except BaseException:
        play_at = 1
    stuff = {
        play_at: {
            "song": song,
            "title": song_name,
            "link": link,
            "thumb": thumb,
            "from_user": from_user,
            "duration": duration,
        }
    }
    if VC_QUEUE.get(chat_id):
        VC_QUEUE[int(chat_id)].update(stuff)
    else:
        VC_QUEUE.update({chat_id: stuff})
    return VC_QUEUE[chat_id]


def list_queue(chat):
    if VC_QUEUE.get(chat):
        txt, n = "", 0
        for x in list(VC_QUEUE[chat].keys())[:18]:
            n += 1
            data = VC_QUEUE[chat][x]
            txt += f'<strong>{n}. <a href={data["link"]}>{data["title"]}</a> :</strong> <i>By: {data["from_user"]}</i>\n'
        txt += "\n\n....."
        return txt


async def get_from_queue(chat_id):
    play_this = list(VC_QUEUE[int(chat_id)].keys())[0]
    info = VC_QUEUE[int(chat_id)][play_this]
    song = info.get("song")
    title = info["title"]
    link = info["link"]
    thumb = info["thumb"]
    from_user = info["from_user"]
    duration = info["duration"]
    if not song:
        song = await get_stream_link(link)
    return song, title, link, thumb, from_user, play_this, duration


# ------------------------------------------------------------------


async def download(query):
    if query.startswith("https://") and "youtube" not in query.lower():
        thumb, duration = None, "Unknown"
        title = link = query
    else:
        search = VideosSearch(query, limit=1).result()
        data = search["result"][0]
        link = data["link"]
        title = data["title"]
        duration = data.get("duration") or "♾"
        thumb = f"https://i.ytimg.com/vi/{data['id']}/hqdefault.jpg"
    dl = await get_stream_link(link)
    return dl, thumb, title, link, duration


async def get_stream_link(ytlink):
    stream = await bash(f'yt-dlp -g -f "best[height<=?720][width<=?1280]" {ytlink}')
    return stream[0]


async def vid_download(query):
    search = VideosSearch(query, limit=1).result()
    data = search["result"][0]
    link = data["link"]
    video = await get_stream_link(link)
    title = data["title"]
    thumb = f"https://i.ytimg.com/vi/{data['id']}/hqdefault.jpg"
    duration = data.get("duration") or "♾"
    return video, thumb, title, link, duration


async def dl_playlist(chat, from_user, link):
    # untill issue get fix
    # https://github.com/alexmercerind/youtube-search-python/issues/107
    links = await get_videos_link(link)
    try:
        search = VideosSearch(links[0], limit=1).result()
        vid1 = search["result"][0]
        duration = vid1.get("duration") or "♾"
        title = vid1["title"]
        song = await get_stream_link(vid1["link"])
        thumb = f"https://i.ytimg.com/vi/{vid1['id']}/hqdefault.jpg"
        return song, thumb, title, vid1["link"], duration
    finally:
        for z in links[1:]:
            try:
                search = VideosSearch(z, limit=1).result()
                vid = search["result"][0]
                duration = vid.get("duration") or "♾"
                title = vid["title"]
                thumb = f"https://i.ytimg.com/vi/{vid['id']}/hqdefault.jpg"
                add_to_queue(chat, None, title, vid["link"], thumb, from_user, duration)
            except Exception as er:
                LOGS.exception(er)


async def file_download(event, reply, fast_download=True):
    thumb = "https://telegra.ph/file/22bb2349da20c7524e4db.mp4"
    title = reply.file.title or reply.file.name or f"{str(time())}.mp4"
    file = reply.file.name or f"{str(time())}.mp4"
    if fast_download:
        dl = await downloader(
            f"vcbot/downloads/{file}",
            reply.media.document,
            event,
            time(),
            f"Downloading {title}...",
        )
        dl = dl.name
    else:
        dl = await reply.download_media()
    duration = (
        time_formatter(reply.file.duration * 1000) if reply.file.duration else "🤷‍♂️"
    )
    if reply.document.thumbs:
        thumb = await reply.download_media("vcbot/downloads/", thumb=-1)
    return dl, thumb, title, reply.message_link, duration


# ------------------------------------------------------------------
