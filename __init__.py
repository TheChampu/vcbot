import asyncio
import os
import re
import traceback
import wave
from enum import Enum
from time import time
from traceback import format_exc
from urllib.parse import quote_plus

import aiohttp

try:
    from pytgcalls import GroupCallFactory
    from pytgcalls.exceptions import GroupCallNotFoundError
    NotInGroupCallError = GroupCallNotFoundError
except ImportError:
    from pathlib import Path

    from pytgcalls import PyTgCalls
    from pytgcalls.exceptions import NotInGroupCallError
    from pytgcalls.types.input_stream import AudioPiped, AudioVideoPiped

    class GroupCallFactory:
        class MTPROTO_CLIENT_TYPE(Enum):
            TELETHON = "telethon"

        def __init__(self, client, client_type):
            self.client = client
            self.client_type = client_type
            if getattr(client.__class__, "__module__", "") != "telethon.client.telegramclient":
                try:
                    client.__class__.__module__ = "telethon.client.telegramclient"
                except Exception:
                    pass
            self._app = PyTgCalls(client)

        def get_group_call(self):
            return _CompatGroupCall(self._app)

    class GroupCallNotFoundError(Exception):
        pass


    class _CompatGroupCall:
        def __init__(self, app):
            self._app = app
            self._chat = None
            self._current_stream = None
            self._current_source = None
            self._network_callback = None
            self._playout_callback = None
            self._stream_end_hooked = False
            self._started = False

        @property
        def is_connected(self):
            return bool(self._chat) and self._app.is_connected

        async def _ensure_started(self):
            if not self._started:
                await self._app.start()
                self._started = True

        async def _get_active_call(self):
            active = self._app.get_active_call(self._chat)
            if asyncio.iscoroutine(active):
                return await active
            return active

        @staticmethod
        def _ensure_valid_source(source):
            if source is None:
                raise ValueError("Empty audio source")
            source = str(source).strip()
            if not source:
                raise ValueError("Empty audio source")
            return source

        def on_network_status_changed(self, callback):
            self._network_callback = callback

        def on_playout_ended(self, callback):
            self._playout_callback = callback
            if not self._stream_end_hooked:
                self._hook_stream_end()

        def _hook_stream_end(self):
            self._stream_end_hooked = True

            @self._app.on_stream_end()
            async def _handler(client, update):
                if self._playout_callback and self._chat is not None:
                    await self._playout_callback(
                        self,
                        self._current_source,
                        "video" if isinstance(self._current_stream, AudioVideoPiped) else "audio",
                    )

        def _silence_file(self):
            silence_path = Path("resources/startup/vc_silence.wav")
            silence_path.parent.mkdir(parents=True, exist_ok=True)
            if not silence_path.exists():
                with wave.open(str(silence_path), "wb") as wav_file:
                    wav_file.setnchannels(1)
                    wav_file.setsampwidth(2)
                    wav_file.setframerate(48000)
                    wav_file.writeframes(b"\x00\x00" * 4800)
            return str(silence_path)

        async def join(self, chat_id):
            await self._ensure_started()
            self._chat = chat_id
            if self._current_stream is None:
                self._current_stream = AudioPiped(self._silence_file())
                self._current_source = self._current_stream._path
            await self._app.join_group_call(chat_id, self._current_stream)
            if self._network_callback:
                await self._network_callback(self, True)

        async def start_audio(self, source):
            await self._ensure_started()
            source = self._ensure_valid_source(source)
            self._current_source = source
            self._current_stream = AudioPiped(source)
            if self._chat is None:
                raise GroupCallNotFoundError()
            try:
                if await self._get_active_call() is None:
                    await self._app.join_group_call(self._chat, self._current_stream)
                else:
                    await self._app.change_stream(self._chat, self._current_stream)
            except NotInGroupCallError:
                # Call exists but user is not joined yet; join directly with stream.
                await self._app.join_group_call(self._chat, self._current_stream)
            if self._network_callback:
                await self._network_callback(self, True)

        async def start_video(self, source, with_audio=True):
            await self._ensure_started()
            source = self._ensure_valid_source(source)
            self._current_source = source
            self._current_stream = AudioVideoPiped(source)
            if self._chat is None:
                raise GroupCallNotFoundError()
            try:
                if await self._get_active_call() is None:
                    await self._app.join_group_call(self._chat, self._current_stream)
                else:
                    await self._app.change_stream(self._chat, self._current_stream)
            except NotInGroupCallError:
                await self._app.join_group_call(self._chat, self._current_stream)
            if self._network_callback:
                await self._network_callback(self, True)

        async def stop_video(self):
            return None

        async def stop(self):
            if self._chat is None:
                return
            try:
                await self._app.leave_group_call(self._chat)
            finally:
                if self._network_callback:
                    await self._network_callback(self, False)
                self._chat = None
                self._current_stream = None
                self._current_source = None

        async def reconnect(self):
            await self._ensure_started()
            if self._chat is None:
                raise GroupCallNotFoundError()
            chat_id = self._chat
            current_stream = self._current_stream
            await self.stop()
            self._chat = chat_id
            self._current_stream = current_stream
            if current_stream is None:
                self._current_stream = AudioPiped(self._silence_file())
                self._current_source = self._current_stream._path
            await self.join(chat_id)

        async def set_my_volume(self, volume):
            await self._ensure_started()
            if self._chat is None:
                raise GroupCallNotFoundError()
            await self._app.change_volume_call(self._chat, volume)

        async def change_stream(self, source):
            await self._ensure_started()
            if self._chat is None:
                raise GroupCallNotFoundError()
            source = self._ensure_valid_source(source)
            self._current_source = source
            self._current_stream = AudioPiped(source)
            try:
                await self._app.change_stream(self._chat, self._current_stream)
            except NotInGroupCallError:
                await self._app.join_group_call(self._chat, self._current_stream)

        async def join_group_call(self, *args, **kwargs):
            return await self.join(*args, **kwargs)

        async def leave_group_call(self, *args, **kwargs):
            return await self.stop(*args, **kwargs)

from telethon.errors.rpcerrorlist import (
    ParticipantJoinMissingError,
    ChatSendMediaForbiddenError,
)
from telethon.errors.rpcbaseerrors import ForbiddenError
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
from pyChampu.fns.ytdl import get_videos_link, get_yt_link
from pyChampu._misc import owner_and_sudos, sudoers
from pyChampu._misc._assistant import in_pattern
from pyChampu._misc._wrappers import eod, eor
from pyChampu.version import __version__ as UltVer
from telethon import events
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
API_URL = udB.get_key("YT_API_URL") or "https://shrutibots.site"
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
        if CLIENTS.get(chat):
            self.group_call = CLIENTS[chat]
        else:
            _client = GroupCallFactory(
                vcClient, GroupCallFactory.MTPROTO_CLIENT_TYPE.TELETHON,
            )
            self.group_call = _client.get_group_call()
            CLIENTS.update({chat: self.group_call})
        # Guard against duplicate on_stream_end callbacks racing each other.
        if not hasattr(self.group_call, "_queue_lock"):
            self.group_call._queue_lock = asyncio.Lock()
        if not hasattr(self.group_call, "_ending_in_progress"):
            self.group_call._ending_in_progress = False

    async def make_vc_active(self):
        try:
            await vcClient(
                functions.phone.CreateGroupCallRequest(
                    self._chat, title="🎧 Music 🎶"
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
                # Group call was created now; retry joining to avoid a later
                # NotInGroupCallError when .play tries change_stream.
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


# --------------------------------------------------


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
        song = await get_stream_link(link)
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
    dl = await get_stream_link(link)
    return dl, thumb, title, link, duration


async def get_stream_link(ytlink):
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

    # Prefer yt-dlp Python API first to avoid shell parsing issues on Windows.
    direct = await _extract_stream_with_ytdlp(ytlink)
    if direct:
        return direct

    # Shell fallback with explicit URL quoting for cmd/powershell safety.
    safe_link = ytlink.replace('"', '\\"')
    quoted = '"' + safe_link + '"'
    out, _ = await bash(
        f'yt-dlp -g -f "best[height<=?720][width<=?1280]" {quoted}'
    )
    primary = (out or "").strip().splitlines()
    if primary and primary[0].strip():
        return primary[0].strip()

    out, _ = await bash(f"yt-dlp -g {quoted}")
    primary = (out or "").strip().splitlines()
    if primary and primary[0].strip():
        return primary[0].strip()

    raise ValueError("Could not resolve a playable stream URL")


async def _extract_stream_with_ytdlp(ytlink):
    if not YoutubeDL:
        return None

    def _pick_stream_url():
        opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "extract_flat": False,
        }
        info = YoutubeDL(opts).extract_info(ytlink, download=False)
        if not isinstance(info, dict):
            return None

        direct = info.get("url")
        if isinstance(direct, str) and direct.strip():
            return direct.strip()

        formats = info.get("formats") or []
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
            if has_video and has_audio:
                if width and height and width <= 1280 and height <= 720:
                    best_av = url.strip()
        return best_av or best_any

    try:
        return await asyncio.to_thread(_pick_stream_url)
    except Exception as ex:
        LOGS.warning("yt-dlp API stream extraction failed: %s", str(ex)[:180])
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
            opts = {
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "extract_flat": False,
            }
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
                    try:
                        async with session.get(api) as response:
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
    video = await get_stream_link(link)
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
        song = await get_stream_link(first["link"])
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


# --------------------------------------------------
