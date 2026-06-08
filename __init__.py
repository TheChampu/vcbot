import asyncio
import os
import re
import traceback
from enum import Enum
from time import time
from traceback import format_exc
from urllib.parse import quote_plus

import aiohttp

from pytgcalls import PyTgCalls
from pytgcalls.types import MediaStream, ChatUpdate, StreamEnded
from pytgcalls.exceptions import (
    NoActiveGroupCall,
    NotInCallError as NotInGroupCallError,
)

# Compat aliases so the rest of the code can still raise/catch these names
GroupCallNotFoundError = NoActiveGroupCall


def AudioPiped(path):
    """Audio-only stream (video suppressed)."""
    return MediaStream(path, video_flags=MediaStream.Flags.IGNORE)


def AudioVideoPiped(path, with_audio=True):
    """Combined audio+video stream."""
    return MediaStream(path)


class GroupCallFactory:
    class MTPROTO_CLIENT_TYPE(Enum):
        TELETHON = "telethon"

    def __init__(self, client, client_type):
        self._app = PyTgCalls(client)

    def get_group_call(self):
        return _CompatGroupCall(self._app)


class _CompatGroupCall:
    """Adapter between the old group-call API used by vcbot and pytgcalls v2.x."""

    def __init__(self, app: PyTgCalls):
        self._app = app
        self._chat = None
        self._current_source = None
        self._network_callback = None
        self._playout_callback = None
        self._started = False

        # Wire stream-end and chat-state events once at construction time.
        @self._app.on_update()
        async def _on_update(client, update):
            if isinstance(update, StreamEnded):
                if self._playout_callback and self._chat == update.chat_id:
                    await self._playout_callback(self, self._current_source, "audio")
            elif isinstance(update, ChatUpdate):
                left = bool(update.status & ChatUpdate.Status.LEFT_CALL)
                if self._network_callback:
                    await self._network_callback(self, not left)
                if left and self._chat == update.chat_id:
                    self._chat = None

    # ------------------------------------------------------------------ #
    # Public helpers                                                       #
    # ------------------------------------------------------------------ #

    @property
    def is_connected(self):
        return self._chat is not None

    async def _ensure_started(self):
        if not self._started:
            await self._app.start()
            self._started = True

    def on_network_status_changed(self, callback):
        self._network_callback = callback

    def on_playout_ended(self, callback):
        self._playout_callback = callback

    async def join(self, chat_id):
        await self._ensure_started()
        self._chat = chat_id
        # Play a short silence to hold the VC slot while the real track is fetched.
        silence = MediaStream(
            "resources/startup/vc_silence.wav",
            video_flags=MediaStream.Flags.IGNORE,
        )
        try:
            await self._app.play(chat_id, silence)
        except NoActiveGroupCall:
            raise GroupCallNotFoundError()
        if self._network_callback:
            await self._network_callback(self, True)

    async def start_audio(self, source):
        await self._ensure_started()
        source = self._ensure_valid_source(source)
        self._current_source = source
        if self._chat is None:
            raise GroupCallNotFoundError()
        stream = MediaStream(source, video_flags=MediaStream.Flags.IGNORE)
        try:
            await self._app.play(self._chat, stream)
        except NoActiveGroupCall:
            raise GroupCallNotFoundError()
        if self._network_callback:
            await self._network_callback(self, True)

    async def start_video(self, source, with_audio=True):
        await self._ensure_started()
        source = self._ensure_valid_source(source)
        self._current_source = source
        if self._chat is None:
            raise GroupCallNotFoundError()
        try:
            await self._app.play(self._chat, MediaStream(source))
        except NoActiveGroupCall:
            raise GroupCallNotFoundError()
        if self._network_callback:
            await self._network_callback(self, True)

    async def stop_video(self):
        """No-op: video is part of the regular stream in v2.x."""
        return None

    async def stop(self):
        if self._chat is None:
            return
        chat_id = self._chat
        self._chat = None
        self._current_source = None
        try:
            await self._app.leave_call(chat_id)
        except Exception:
            pass
        finally:
            if self._network_callback:
                await self._network_callback(self, False)

    async def reconnect(self):
        await self._ensure_started()
        if self._chat is None:
            raise GroupCallNotFoundError()
        chat_id = self._chat
        source = self._current_source
        await self.stop()
        self._chat = chat_id
        await self.join(chat_id)
        if source:
            await self.start_audio(source)

    async def change_stream(self, source):
        await self._ensure_started()
        if self._chat is None:
            raise GroupCallNotFoundError()
        source = self._ensure_valid_source(source)
        self._current_source = source
        stream = MediaStream(source, video_flags=MediaStream.Flags.IGNORE)
        try:
            await self._app.play(self._chat, stream)
        except NoActiveGroupCall:
            raise GroupCallNotFoundError()

    async def set_my_volume(self, volume):
        await self._ensure_started()
        if self._chat is None:
            raise GroupCallNotFoundError()
        try:
            await self._app.change_volume_call(self._chat, volume)
        except Exception:
            pass

    async def set_is_mute(self, mute: bool):
        await self._ensure_started()
        if self._chat is None:
            raise GroupCallNotFoundError()
        try:
            if mute:
                await self._app.mute(self._chat)
            else:
                await self._app.unmute(self._chat)
        except Exception:
            pass

    async def set_pause(self, pause: bool):
        await self._ensure_started()
        if self._chat is None:
            raise GroupCallNotFoundError()
        try:
            if pause:
                await self._app.pause(self._chat)
            else:
                await self._app.resume(self._chat)
        except Exception:
            pass

    def restart_playout(self):
        """Re-play current source from beginning (fire-and-forget)."""
        if self._chat is not None and self._current_source:
            asyncio.ensure_future(self._restart_playout_async())

    async def _restart_playout_async(self):
        try:
            source = self._current_source
            if source and self._chat:
                stream = MediaStream(source, video_flags=MediaStream.Flags.IGNORE)
                await self._app.play(self._chat, stream)
        except Exception:
            pass

    # Legacy aliases kept for compatibility with other vcbot files
    async def join_group_call(self, *args, **kwargs):
        return await self.join(*args, **kwargs)

    async def leave_group_call(self, *args, **kwargs):
        return await self.stop()

from telethon.errors.rpcerrorlist import (
    ParticipantJoinMissingError,
    ChatSendMediaForbiddenError,
)
from telethon.errors.rpcbaseerrors import ForbiddenError
from pyChampu import HNDLR, LOGS, asst, champu_bot, udB, vcClient
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
from pyChampu.fns.ytdl import get_videos_link, get_yt_link
from pyChampu._misc import owner_and_sudos, sudoers
from pyChampu._misc._assistant import in_pattern
from pyChampu._misc._wrappers import eod, eor
from pyChampu.version import __version__ as UltVer
from telethon import events
from telethon.errors.rpcerrorlist import UserNotParticipantError
from telethon.tl import functions, types
from telethon.utils import get_display_name

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

asstUserName = asst.me.username
LOG_CHANNEL = udB.get_key("LOG_CHANNEL")
ACTIVE_CALLS, VC_QUEUE = [], {}
MSGID_CACHE, VIDEO_ON = {}, {}
CLIENTS = {}
STREAM_CACHE = {}
STREAM_CACHE_TTL = 900
LAST_WORKING_COOKIE_FILE = None
API_URL = os.environ.get("SHRUTI_API_URL") or udB.get_key("YT_API_URL") or "https://api.shrutibots.site"
API_KEY = os.environ.get("SHRUTI_API_KEY", "ShrutiBotsP3A8xKwYFafG6SuSLTIM")
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
YT_COOKIES_DIR = "/home/ubuntu/ProjectRoot/resources/cookies"
SEARCH_ENDPOINTS = (
    "song/search",
    "songs/search",
    "ytsearch",
    "youtube/search",
    "search",
    "song",
    "songs",
    "api/search",
)


def VC_AUTHS():
    _vcsudos = udB.get_key("VC_SUDOS") or []
    return [int(a) for a in [*owner_and_sudos(), *_vcsudos]]


class Player:
    def __init__(self, chat, event=None, video=False):
        self._chat = chat
        self._current_chat = event.chat_id if event else LOG_CHANNEL
        self._video = video
        self._voice_client = vcClient
        if CLIENTS.get(chat):
            self.group_call = CLIENTS[chat]
        else:
            self.group_call = self._create_group_call(self._voice_client)
            CLIENTS.update({chat: self.group_call})
        # Guard against duplicate on_stream_end callbacks racing each other.
        if not hasattr(self.group_call, "_queue_lock"):
            self.group_call._queue_lock = asyncio.Lock()
        if not hasattr(self.group_call, "_ending_in_progress"):
            self.group_call._ending_in_progress = False

    @staticmethod
    def _create_group_call(client):
        return GroupCallFactory(
            client,
            GroupCallFactory.MTPROTO_CLIENT_TYPE.TELETHON,
        ).get_group_call()

    async def _use_userbot_fallback_if_needed(self):
        if self._voice_client is champu_bot:
            return
        if not champu_bot or getattr(champu_bot.me, "bot", True):
            return
        if not getattr(asst, "me", None) or not getattr(asst.me, "bot", True):
            return
        try:
            await asst.get_permissions(self._chat, asst.me.id)
            return
        except UserNotParticipantError:
            LOGS.info(
                "Assistant bot is not in chat %s, falling back to userbot VC client.",
                self._chat,
            )
        except Exception as er:
            LOGS.debug(f"Assistant membership check failed for {self._chat}: {er}")
            return

        self._voice_client = champu_bot
        self.group_call = self._create_group_call(champu_bot)
        CLIENTS.update({self._chat: self.group_call})

    async def make_vc_active(self):
        try:
            await self._voice_client(
                functions.phone.CreateGroupCallRequest(
                    self._chat, title="🎧 Music 🎶"
                )
            )
        except Exception as e:
            LOGS.exception(e)
            return False, e
        return True, None

    async def startCall(self, allow_create=False):
        await self._use_userbot_fallback_if_needed()
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
                if not allow_create:
                    return False, "Voice chat is off. Please turn it on first, then use play again."
                dn, err = await self.make_vc_active()
                if err:
                    return False, err
                await asyncio.sleep(1.5)
                try:
                    await self.group_call.join(self._chat)
                except Exception as e:
                    LOGS.exception(e)
                    return False, e
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
        """Handle stream-end safely without double-processing queue events."""
        lock = self.group_call._queue_lock
        async with lock:
            current_source = getattr(self.group_call, "_current_source", None)
            if current_source and source and current_source != source:
                LOGS.debug(
                    "Ignoring stale stream-end event for %s (current: %s)",
                    source,
                    current_source,
                )
                return

            try:
                if source and os.path.exists(source):
                    os.remove(source)
            except Exception as e:
                LOGS.debug(f"Error removing temp file: {e}")

            try:
                await self.play_from_queue()
            except Exception as e:
                LOGS.exception(f"Error in playout_ended_handler: {e}")

    async def _notify_and_leave_after_queue_end(self):
        if self.group_call._ending_in_progress:
            return
        self.group_call._ending_in_progress = True
        try:
            await vcClient.send_message(
                self._current_chat,
                "🎵 <strong>Song was finished.</strong> If you want to play more songs, give me commands.",
                parse_mode="html",
            )
            # Give Telegram a moment to deliver the message before leaving VC.
            await asyncio.sleep(1.5)
            await self.group_call.stop()
        finally:
            if CLIENTS.get(self._chat):
                del CLIENTS[self._chat]
            self.group_call._ending_in_progress = False

    async def play_from_queue(self):
        chat_id = self._chat
        if chat_id in VIDEO_ON:
            try:
                await self.group_call.stop_video()
                VIDEO_ON.pop(chat_id)
            except Exception as e:
                LOGS.debug(f"Error stopping video: {e}")
        
        # Queue finished: notify and leave VC cleanly.
        if not VC_QUEUE.get(chat_id) or not VC_QUEUE[chat_id]:
            await self._notify_and_leave_after_queue_end()
            return
        
        try:
            song, title, link, thumb, from_user, pos, dur = await get_from_queue(
                chat_id
            )
            try:
                await self.group_call.start_audio(song)
            except ParticipantJoinMissingError:
                LOGS.info("ParticipantJoinMissingError, attempting rejoin")
                if not (await self.vc_joiner()):
                    return
                await self.group_call.start_audio(song)
            except GroupCallNotFoundError:
                LOGS.info("GroupCallNotFoundError, attempting rejoin")
                if not (await self.vc_joiner(announce=False)):
                    return
                await self.group_call.start_audio(song)
            except NotInGroupCallError:
                LOGS.info("NotInGroupCallError, attempting rejoin")
                if not (await self.vc_joiner()):
                    return
                await self.group_call.start_audio(song)
            
            if MSGID_CACHE.get(chat_id):
                try:
                    await MSGID_CACHE[chat_id].delete()
                except Exception:
                    pass
                del MSGID_CACHE[chat_id]
            
            text = f"<strong>🎧 Now playing #{pos}: <a href={link}>{title}</a>\n⏰ Duration:</strong> <code>{dur}</code>\n👤 <strong>Requested by:</strong> {from_user}"
            title_only_text = f"<strong>🎧 Now playing #{pos}:</strong> <code>{title}</code>"

            try:
                xx = await vcClient.send_message(
                    self._current_chat,
                    text,
                    file=thumb,
                    link_preview=False,
                    parse_mode="html",
                )

            except (ChatSendMediaForbiddenError, ForbiddenError):
                xx = await vcClient.send_message(
                    self._current_chat,
                    title_only_text,
                    link_preview=False,
                    parse_mode="html",
                )
            except Exception as msg_err:
                LOGS.exception(f"Error sending now playing message: {msg_err}")
                xx = None
            
            if xx:
                MSGID_CACHE.update({chat_id: xx})
            
            # Remove now-playing song from queue only after start succeeds.
            try:
                VC_QUEUE[chat_id].pop(pos)
                if not VC_QUEUE[chat_id]:
                    VC_QUEUE.pop(chat_id)
            except (KeyError, IndexError):
                pass

        except (IndexError, KeyError) as e:
            # Queue is empty at this point; notify and leave VC.
            LOGS.info(f"Queue empty in chat {chat_id}: {e}")
            await self._notify_and_leave_after_queue_end()
        except Exception as er:
            # For transient errors, keep VC connected and notify.
            LOGS.exception(f"Error playing next song: {er}")
            await vcClient.send_message(
                self._current_chat,
                f"⚠️ <strong>Error playing next song:</strong> <code>{str(er)[:100]}</code>",
                parse_mode="html",
            )

    async def vc_joiner(self, announce=True, allow_create=False):
        chat_id = self._chat
        done, err = await self.startCall(allow_create=allow_create)

        if done:
            if announce:
                await vcClient.send_message(
                    self._current_chat,
                    f"• Joined VC in <code>{chat_id}</code>",
                    parse_mode="html",
                )

            return True
        if isinstance(err, str):
            await vcClient.send_message(self._current_chat, err, parse_mode="html")
            return False
        await vcClient.send_message(
            self._current_chat,
            f"<strong>ERROR while Joining Vc -</strong> <code>{chat_id}</code> :\n<code>{err}</code>",
            parse_mode="html",
        )
        return False


async def ensure_vc(chat, event=None, video=False, announce=False):
    player = Player(chat, event, video)
    if player.group_call.is_connected:
        return player
    if not await player.vc_joiner(announce=announce):
        return None
    return player


# --------------------------------------------------


def vc_asst(dec, **kwargs):
    def ult(func):
        kwargs["func"] = (
            lambda e: not e.is_private and not e.via_bot_id and not e.fwd_from
        )
        handler = udB.get_key("VC_HNDLR") or HNDLR
        kwargs["pattern"] = compile_pattern(dec, handler)
        vc_auth = kwargs.get("vc_auth", True)
        allow_all = kwargs.get("allow_all", True)
        key = udB.get_key("VC_AUTH_GROUPS") or {}
        if "vc_auth" in kwargs:
            del kwargs["vc_auth"]
        if "allow_all" in kwargs:
            del kwargs["allow_all"]

        async def vc_handler(e):
            VCAUTH = list(key.keys())
            if not (
                allow_all
                or (e.out)
                or (e.sender_id in VC_AUTHS())
                or (vc_auth and e.chat_id in VCAUTH)
            ):
                return
            elif not allow_all and vc_auth and key.get(e.chat_id):
                cha, adm = key.get(e.chat_id), key[e.chat_id]["admins"]
                if adm and not (await admin_check(e)):
                    return
            try:
                await func(e)
            except ValueError as er:
                msg = str(er).strip() or "Could not process this request."
                LOGS.warning("VC command failed: %s", msg[:220])
                try:
                    await e.reply(f"❌ {msg}")
                except Exception:
                    pass
            except Exception:
                LOGS.exception("VC handler error")
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


# --------------------------------------------------


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
        song = await get_stream_link(link, prefer_audio=True)
    return song, title, link, thumb, from_user, play_this, duration


# --------------------------------------------------


async def download(query):
    if query.startswith("https://") and "youtube" not in query.lower():
        thumb, duration = None, "Unknown"
        title = link = query
    else:
        data = None
        if VideosSearch:
            try:
                search = VideosSearch(query, limit=1).result()
                data = search["result"][0]
            except Exception as ex:
                LOGS.warning("VideosSearch failed, using fallback for query '%s': %s", query, str(ex)[:180])

        if data:
            link = data["link"]
            title = data["title"]
            duration = data.get("duration") or "♾"
            thumb = f"https://i.ytimg.com/vi/{data['id']}/hqdefault.jpg"
        else:
            link = await _api_search_youtube_link(query)
            if not link:
                link = get_yt_link(query)
            if not link:
                raise ValueError("No playable YouTube result found")
            title = link
            duration = "Unknown"
            thumb = None
    dl = await get_stream_link(link, prefer_audio=True)
    return dl, thumb, title, link, duration


async def get_stream_link(ytlink, prefer_audio=True):
    """
    info = YoutubeDL({}).extract_info(url=ytlink, download=False)
    k = ""
    for x in info["formats"]:
        h, w = ([x["height"], x["width"]])
        if h and w:
            if h <= 720 and w <= 1280:
                k = x["url"]
    return k
    """
    if isinstance(ytlink, (list, tuple)):
        ytlink = ytlink[0] if ytlink else ""
    ytlink = str(ytlink or "").strip()
    if not ytlink:
        raise ValueError("Could not resolve a playable stream URL")

    cached = _stream_cache_get(ytlink, prefer_audio)
    if cached:
        return cached

    # Priority: yt-dlp (no cookies) -> yt-dlp (all cookie files) -> API_URL.
    cookie_files = [None] + _ordered_cookie_files()
    last_error = ""
    selector = "bestaudio/best" if prefer_audio else "best[height<=?720][width<=?1280]/best"

    for cookie_file in cookie_files:
        direct, api_error = await _extract_stream_with_ytdlp(
            ytlink, cookie_file=cookie_file, prefer_audio=prefer_audio
        )
        if direct:
            global LAST_WORKING_COOKIE_FILE
            if cookie_file:
                LAST_WORKING_COOKIE_FILE = cookie_file
            _stream_cache_set(ytlink, prefer_audio, direct)
            return direct
        if api_error:
            last_error = str(api_error)

        safe_link = ytlink.replace('"', '\\"')
        quoted = '"' + safe_link + '"'
        cookie_arg = ""
        if cookie_file:
            safe_cookie = cookie_file.replace('"', '\\"')
            cookie_arg = f' --cookies "{safe_cookie}"'

        out, _ = await bash(
            f'yt-dlp -g -f "{selector}"{cookie_arg} {quoted}'
        )
        primary = (out or "").strip().splitlines()
        if primary and primary[0].strip():
            stream_url = primary[0].strip()
            if cookie_file:
                LAST_WORKING_COOKIE_FILE = cookie_file
            _stream_cache_set(ytlink, prefer_audio, stream_url)
            return stream_url

        out, err = await bash(f"yt-dlp -g{cookie_arg} {quoted}")
        primary = (out or "").strip().splitlines()
        if primary and primary[0].strip():
            stream_url = primary[0].strip()
            if cookie_file:
                LAST_WORKING_COOKIE_FILE = cookie_file
            _stream_cache_set(ytlink, prefer_audio, stream_url)
            return stream_url
        if err:
            last_error = str(err)

    api_stream = await _api_resolve_stream_url(ytlink, prefer_audio=prefer_audio)
    if api_stream:
        _stream_cache_set(ytlink, prefer_audio, api_stream)
        return api_stream

    if _is_youtube_bot_challenge(last_error):
        raise ValueError(
            "YouTube blocked playback. yt-dlp and cookies both failed. "
            "Put valid cookies .txt/.json files in /home/ubuntu/ProjectRoot/resources/cookies"
        )
    raise ValueError("Could not resolve a playable stream URL")


def _ytdlp_cookie_files():
    out = []
    seen = set()

    # Optional explicit cookie file via env/db.
    for key in ("YTDLP_COOKIES_FILE", "YT_COOKIES_FILE", "YTDLP_COOKIES", "YT_COOKIES"):
        value = os.getenv(key)
        if isinstance(value, str) and value.strip() and os.path.exists(value.strip()):
            p = value.strip()
            if p not in seen:
                seen.add(p)
                out.append(p)
    try:
        db_value = udB.get_key("YTDLP_COOKIES_FILE") or udB.get_key("YT_COOKIES_FILE")
        if isinstance(db_value, str) and db_value.strip() and os.path.exists(db_value.strip()):
            p = db_value.strip()
            if p not in seen:
                seen.add(p)
                out.append(p)
    except Exception:
        pass

    # Folder-based cookies requested by user.
    if os.path.isdir(YT_COOKIES_DIR):
        for name in sorted(os.listdir(YT_COOKIES_DIR)):
            path = os.path.join(YT_COOKIES_DIR, name)
            if not os.path.isfile(path):
                continue
            low = name.lower()
            if not (low.endswith(".txt") or low.endswith(".json")):
                continue
            if path not in seen:
                seen.add(path)
                out.append(path)
    return out


def _ordered_cookie_files():
    files = _ytdlp_cookie_files()
    global LAST_WORKING_COOKIE_FILE
    if LAST_WORKING_COOKIE_FILE and LAST_WORKING_COOKIE_FILE in files:
        files.remove(LAST_WORKING_COOKIE_FILE)
        files.insert(0, LAST_WORKING_COOKIE_FILE)
    return files


def _stream_cache_get(url, prefer_audio):
    key = (str(url).strip(), bool(prefer_audio))
    data = STREAM_CACHE.get(key)
    if not data:
        return None
    stream_url, ts = data
    if (time() - ts) > STREAM_CACHE_TTL:
        STREAM_CACHE.pop(key, None)
        return None
    return stream_url


def _stream_cache_set(url, prefer_audio, stream_url):
    key = (str(url).strip(), bool(prefer_audio))
    STREAM_CACHE[key] = (stream_url, time())


def _ytdlp_common_opts(cookie_file=None) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "extract_flat": False,
    }
    if not cookie_file:
        files = _ordered_cookie_files()
        cookie_file = files[0] if files else None
    if cookie_file:
        opts["cookiefile"] = cookie_file
    return opts


def _is_youtube_bot_challenge(error_text: str) -> bool:
    text = str(error_text or "").lower()
    checks = (
        "sign in to confirm you're not a bot",
        "sign in to confirm you’re not a bot",
        "use --cookies-from-browser",
        "use --cookies",
    )
    return any(c in text for c in checks)


async def _extract_stream_with_ytdlp(ytlink, cookie_file=None, prefer_audio=True):
    if not YoutubeDL:
        return None, "yt-dlp not installed"

    def _pick_stream_url():
        opts = _ytdlp_common_opts(cookie_file=cookie_file)
        info = YoutubeDL(opts).extract_info(ytlink, download=False)
        if not isinstance(info, dict):
            return None

        direct = info.get("url")
        if isinstance(direct, str) and direct.strip():
            return direct.strip()

        formats = info.get("formats") or []
        best_audio = None
        best_av = None
        best_any = None
        for fmt in formats:
            if not isinstance(fmt, dict):
                continue
            url = fmt.get("url")
            if not isinstance(url, str) or not url.strip():
                continue
            if best_any is None:
                best_any = url.strip()

            width = fmt.get("width")
            height = fmt.get("height")
            vcodec = str(fmt.get("vcodec") or "")
            acodec = str(fmt.get("acodec") or "")
            has_video = vcodec != "none"
            has_audio = acodec != "none"
            if has_audio and not has_video:
                abr = fmt.get("abr") or 0
                prev_abr = best_audio[1] if best_audio else 0
                if abr >= prev_abr:
                    best_audio = (url.strip(), abr)
            if has_video and has_audio:
                if width and height and width <= 1280 and height <= 720:
                    best_av = url.strip()
        if prefer_audio and best_audio:
            return best_audio[0]
        return best_av or (best_audio[0] if best_audio else None) or best_any

    try:
        return await asyncio.to_thread(_pick_stream_url), None
    except Exception as ex:
        msg = str(ex)[:220]
        if cookie_file:
            LOGS.warning("yt-dlp failed with cookie %s: %s", os.path.basename(cookie_file), msg)
        elif _is_youtube_bot_challenge(msg):
            LOGS.warning("yt-dlp blocked by YouTube bot challenge")
        else:
            LOGS.warning("yt-dlp API stream extraction failed: %s", msg)
        return None, msg


async def _api_resolve_stream_url(link, prefer_audio=True):
    link = str(link or "").strip()
    if not link:
        return None
    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            media_type = "audio" if prefer_audio else "video"
            params = {"url": link, "type": media_type}
            if API_KEY:
                params["api_key"] = API_KEY
            headers = {}
            if API_KEY:
                headers["x-api-key"] = API_KEY
                headers["Authorization"] = f"Bearer {API_KEY}"
            async with session.get(f"{API_URL}/download", params=params, headers=headers) as response:
                if response.status != 200:
                    return None
                data = await response.json(content_type=None)

            for key in ("stream_url", "download_url", "url"):
                val = data.get(key)
                if isinstance(val, str) and val.startswith("http"):
                    return val

            token = data.get("download_token")
            media_id = data.get("id") or data.get("video_id")
            if token and media_id:
                encoded = quote_plus(str(media_id))
                stream_link = f"{API_URL}/stream/{encoded}?type={media_type}&token={token}"
                if API_KEY:
                    stream_link += f"&api_key={API_KEY}"
                return stream_link
    except Exception as ex:
        LOGS.warning("API stream resolver failed: %s", str(ex)[:180])
    return None


def _extract_api_link(payload):
    if not isinstance(payload, dict):
        return None
    for key in ("link", "url", "video_url", "videoUrl", "webpage_url"):
        val = payload.get(key)
        if isinstance(val, str) and "youtube" in val:
            return val
    vid = payload.get("id") or payload.get("video_id") or payload.get("videoId")
    if isinstance(vid, str) and vid.strip():
        return f"https://youtube.com/watch?v={vid.strip()}"
    return None


def _extract_api_link_deep(data):
    if isinstance(data, list):
        for item in data:
            found = _extract_api_link_deep(item)
            if found:
                return found
        return None
    if isinstance(data, dict):
        direct = _extract_api_link(data)
        if direct:
            return direct
        for key in ("result", "results", "data", "song", "songs", "videos"):
            found = _extract_api_link_deep(data.get(key))
            if found:
                return found
    return None


def _videosearch_to_metadata(data):
    if not isinstance(data, dict):
        return None
    link = data.get("link")
    if not isinstance(link, str) or not link.strip():
        return None
    vid = data.get("id")
    thumb = None
    if isinstance(vid, str) and vid.strip():
        thumb = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
    return {
        "link": link.strip(),
        "title": str(data.get("title") or link).strip(),
        "duration": data.get("duration") or "♾",
        "thumb": thumb,
    }


async def _metadata_from_url(url, fallback_title=None):
    url = str(url or "").strip()
    if not url:
        return None

    title = fallback_title or url
    duration = "Unknown"
    thumb = None

    if YoutubeDL:
        def _extract():
            opts = _ytdlp_common_opts()
            return YoutubeDL(opts).extract_info(url, download=False)

        try:
            info = await asyncio.to_thread(_extract)
            if isinstance(info, dict):
                title = str(info.get("title") or title).strip()
                seconds = info.get("duration")
                if isinstance(seconds, (int, float)) and seconds > 0:
                    duration = time_formatter(int(seconds * 1000))
                elif info.get("is_live"):
                    duration = "♾"
                thumb = info.get("thumbnail") or thumb
                url = str(info.get("webpage_url") or info.get("original_url") or url).strip()
        except Exception as ex:
            LOGS.warning("yt-dlp metadata extraction failed: %s", str(ex)[:180])

    return {
        "link": url,
        "title": title,
        "duration": duration,
        "thumb": thumb,
    }


async def _resolve_video_metadata(query):
    query = str(query or "").strip()
    if not query:
        return None

    if VideosSearch:
        try:
            search = await asyncio.to_thread(lambda: VideosSearch(query, limit=1).result())
            results = (search or {}).get("result") or []
            if results:
                parsed = _videosearch_to_metadata(results[0])
                if parsed:
                    return parsed
        except Exception as ex:
            LOGS.warning("VideosSearch failed for '%s': %s", query, str(ex)[:180])

    link = query if is_url_ok(query) else None
    if not link:
        link = await _api_search_youtube_link(query)
    if not link:
        link = get_yt_link(query)
    if not link:
        return None

    return await _metadata_from_url(link, fallback_title=query)


async def _api_search_youtube_link(query):
    encoded_query = quote_plus(query)
    timeout = aiohttp.ClientTimeout(total=8)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for endpoint in SEARCH_ENDPOINTS:
                for param in ("query", "q", "search"):
                    api = f"{API_URL}/{endpoint}?{param}={encoded_query}"
                    if API_KEY:
                        api += f"&api_key={API_KEY}"
                    headers = {}
                    if API_KEY:
                        headers["x-api-key"] = API_KEY
                        headers["Authorization"] = f"Bearer {API_KEY}"
                    try:
                        async with session.get(api, headers=headers) as response:
                            if response.status != 200:
                                continue
                            data = await response.json(content_type=None)
                    except Exception:
                        continue

                    link = _extract_api_link_deep(data)
                    if link:
                        return link
    except Exception:
        pass
    return None


async def vid_download(query):
    info = await _resolve_video_metadata(query)
    if not info:
        raise ValueError("No playable video result found")
    link = info["link"]
    video = await get_stream_link(link, prefer_audio=False)
    return video, info.get("thumb"), info.get("title") or link, link, info.get("duration") or "Unknown"


async def dl_playlist(chat, from_user, link):
    # untill issue get fix
    # https://github.com/alexmercerind/youtube-search-python/issues/107
    """
    vids = Playlist.getVideos(link)
    try:
        vid1 = vids["videos"][0]
        duration = vid1["duration"] or "♾"
        title = vid1["title"]
        song = await get_stream_link(vid1['link'])
        thumb = f"https://i.ytimg.com/vi/{vid1['id']}/hqdefault.jpg"
        return song[0], thumb, title, vid1["link"], duration
    finally:
        vids = vids["videos"][1:]
        for z in vids:
            duration = z["duration"] or "♾"
            title = z["title"]
            thumb = f"https://i.ytimg.com/vi/{z['id']}/hqdefault.jpg"
            add_to_queue(chat, None, title, z["link"], thumb, from_user, duration)
    """
    links = await get_videos_link(link)
    if not links:
        raise ValueError("Could not read playlist items")

    async def _meta_for_playlist_item(item_link):
        if VideosSearch:
            try:
                search = await asyncio.to_thread(
                    lambda: VideosSearch(item_link, limit=1).result()
                )
                results = (search or {}).get("result") or []
                if results:
                    parsed = _videosearch_to_metadata(results[0])
                    if parsed:
                        return parsed
            except Exception as ex:
                LOGS.warning(
                    "VideosSearch playlist lookup failed for '%s': %s",
                    item_link,
                    str(ex)[:180],
                )

        return await _metadata_from_url(item_link, fallback_title=item_link)

    try:
        first = await _meta_for_playlist_item(links[0])
        if not first:
            raise ValueError("Could not resolve first playlist video")
        song = await get_stream_link(first["link"], prefer_audio=True)
        return (
            song,
            first.get("thumb"),
            first.get("title") or links[0],
            first["link"],
            first.get("duration") or "Unknown",
        )
    finally:
        for z in links[1:]:
            try:
                meta = await _meta_for_playlist_item(z)
                if not meta:
                    continue
                add_to_queue(
                    chat,
                    None,
                    meta.get("title") or z,
                    meta["link"],
                    meta.get("thumb"),
                    from_user,
                    meta.get("duration") or "Unknown",
                )
            except Exception as er:
                LOGS.exception(er)


async def file_download(event, reply, fast_download=True):
    thumb = "https://telegra.ph/file/abc578ecc222d28a861ba.mp4"
    title = reply.file.title or reply.file.name or f"{str(time())}.mp4"
    file = reply.file.name or f"{str(time())}.mp4"
    if fast_download:
        dl = await downloader(
            os.path.join(DOWNLOAD_DIR, file),
            reply.media.document,
            event,
            time(),
            f"Downloading {title}...",
        )

        dl = dl.name
    else:
        dl = await reply.download_media(os.path.join(DOWNLOAD_DIR, ""))
    duration = (
        time_formatter(reply.file.duration * 1000) if reply.file.duration else "🤷‍♂️"
    )
    if reply.document.thumbs:
        thumb = await reply.download_media(os.path.join(DOWNLOAD_DIR, ""), thumb=-1)
    return dl, thumb, title, reply.message_link, duration


# --------------------------------------------------
