
import asyncio
import os
import re
import traceback
from contextlib import suppress
from time import time
from traceback import format_exc

# ──────────────────────────────────────────────────────────────────────────────
# PyTgCalls v3 — (py-tgcalls >= 2.0 ships v3 API)
# ──────────────────────────────────────────────────────────────────────────────
from pyChampu import HNDLR, LOGS, asst, udB, vcClient
from telethon.errors.rpcerrorlist import (
    ParticipantJoinMissingError,
    ChatSendMediaForbiddenError,
)

PyTgCalls    = None
MediaStream  = None
AudioQuality = None
VideoQuality = None
StreamEnded  = None

try:
    from pytgcalls import PyTgCalls
    from pytgcalls.types import MediaStream, AudioQuality, VideoQuality, StreamEnded
    LOGS.info("PyTgCalls v3 API loaded successfully.")
except ImportError as _imp_err:
    LOGS.error(f"Failed to import PyTgCalls: {_imp_err}")

NoActiveGroupCall = Exception
NotInCallError    = Exception
try:
    from pytgcalls.exceptions import NoActiveGroupCall, NotInCallError
except ImportError:
    try:
        from pytgcalls.exceptions import NoActiveGroupCall
        NotInCallError = NoActiveGroupCall
    except ImportError:
        pass

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

# Backward-compat alias
GroupCallNotFoundError = NoActiveGroupCall

try:
    from yt_dlp import YoutubeDL
except ImportError:
    YoutubeDL = None
    LOGS.warning("'yt-dlp' not found — YouTube downloads will fail.")

try:
    from youtubesearchpython import VideosSearch
except ImportError:
    VideosSearch = None

from strings import get_string

# ──────────────────────────────────────────────────────────────────────────────
# YouTube API constants
# ──────────────────────────────────────────────────────────────────────────────
API_URL = (
    os.environ.get("SHRUTI_API_URL")
    or os.environ.get("YT_API_URL")
    or udB.get_key("YT_API_URL")
    or "http://api01.shrutibots.site"
)
API_KEY = os.environ.get("SHRUTI_API_KEY", "ShrutiBotsP3A8xKwYFafG6SuSLTIM")

# ──────────────────────────────────────────────────────────────────────────────
# Global state
# ──────────────────────────────────────────────────────────────────────────────
asstUserName = asst.me.username
LOG_CHANNEL  = udB.get_key("LOG_CHANNEL")

ACTIVE_CALLS: list = []           # chat_ids currently in a voice call
VC_QUEUE:     dict = {}           # {chat_id: {pos: {song, title, link, …}}}
MSGID_CACHE:  dict = {}           # {chat_id: Message} — current "now playing" msg
VIDEO_ON:     dict = {}           # {chat_id: GroupCallWrapper} — active video chats
CLIENTS:      dict = {}           # {chat_id: GroupCallWrapper}

VC_PLAYOUT_CALLBACKS: dict = {}   # {chat_id: async callback(call, source, mtype)}
VC_STREAM_FILES:      dict = {}   # {chat_id: current_file_path}

# ──────────────────────────────────────────────────────────────────────────────
# Initialise PyTgCalls client
# ──────────────────────────────────────────────────────────────────────────────
pytgcalls_client = None

if vcClient is not None and PyTgCalls is not None:
    try:
        pytgcalls_client = PyTgCalls(vcClient)
        pytgcalls_client.start()
        LOGS.info("PyTgCalls client started successfully.")
    except RuntimeError as _rterr:
        # Already-started warning from internal thread — safe to ignore
        LOGS.warning(f"PyTgCalls start note (ignored): {_rterr}")
    except Exception as _pytgcalls_err:
        LOGS.exception(f"PyTgCalls client start failed: {_pytgcalls_err}")
        pytgcalls_client = None


# ──────────────────────────────────────────────────────────────────────────────
# StreamEnded handler
# ──────────────────────────────────────────────────────────────────────────────

def _register_stream_handler() -> None:
    """Attach the StreamEnded handler to the global pytgcalls client."""
    if pytgcalls_client is None or StreamEnded is None:
        return

    @pytgcalls_client.on_update()
    async def _on_stream_ended(update):
        if not isinstance(update, StreamEnded):
            return
        chat_id = update.chat_id
        source   = VC_STREAM_FILES.get(chat_id, "")
        callback = VC_PLAYOUT_CALLBACKS.get(chat_id)
        if callback:
            try:
                await callback(None, source, None)
            except Exception as _cb_err:
                LOGS.exception(f"Playout callback error (chat {chat_id}): {_cb_err}")

_register_stream_handler()


# ──────────────────────────────────────────────────────────────────────────────
# GroupCallWrapper  —  thin v3 wrapper exposing the legacy GroupCall interface
# ──────────────────────────────────────────────────────────────────────────────

class GroupCallWrapper:
    """
    Wraps the global PyTgCalls v3 client and presents the legacy
    GroupCall API that play.py, controls.py, vctools.py etc. rely on.
    """

    def __init__(self, chat_id: int) -> None:
        self._chat_id = chat_id

    # ── State ──────────────────────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        return self._chat_id in ACTIVE_CALLS

    # ── Callback registration ──────────────────────────────────────────────

    def on_network_status_changed(self, callback) -> None:
        """No-op — connectivity is tracked via ACTIVE_CALLS."""
        pass

    def on_playout_ended(self, callback) -> None:
        VC_PLAYOUT_CALLBACKS[self._chat_id] = callback

    # ── Stream builders ────────────────────────────────────────────────────

    def _audio_stream(self, path: str) -> "MediaStream":
        return MediaStream(
            path,
            audio_parameters=AudioQuality.HIGH,
            video_flags=MediaStream.Flags.IGNORE,
        )

    def _video_stream(self, path: str, with_audio: bool = True) -> "MediaStream":
        return MediaStream(
            path,
            audio_parameters=AudioQuality.HIGH,
            video_parameters=VideoQuality.HD_720p,
            audio_flags=(
                MediaStream.Flags.AUTO_DETECT if with_audio
                else MediaStream.Flags.IGNORE
            ),
        )

    # ── Call control ────────────────────────────────────────────────────────

    async def join(self, chat_id: int) -> None:
        """Join the voice chat (silent — no user-facing audio)."""
        if pytgcalls_client is None:
            raise RuntimeError("PyTgCalls client not initialised (no VC session).")
        # pytgcalls v3 requires an active stream to join the call.
        # We play a very short public silence MP3; the StreamEnded event fires
        # immediately and play_from_queue() takes over.
        silence = "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3"
        try:
            await pytgcalls_client.play(
                chat_id,
                MediaStream(
                    silence,
                    video_flags=MediaStream.Flags.IGNORE,
                ),
            )
        except Exception as e:
            LOGS.warning(f"GroupCallWrapper.join({chat_id}) error: {e}")
            raise
        if chat_id not in ACTIVE_CALLS:
            ACTIVE_CALLS.append(chat_id)

    async def start_audio(self, path: str) -> None:
        """Start (or switch to) streaming audio from *path* (file or URL)."""
        if pytgcalls_client is None:
            raise RuntimeError("PyTgCalls client not initialised.")
        VC_STREAM_FILES[self._chat_id] = path
        stream = self._audio_stream(path)
        try:
            await pytgcalls_client.play(self._chat_id, stream)
            if self._chat_id not in ACTIVE_CALLS:
                ACTIVE_CALLS.append(self._chat_id)
        except Exception as e:
            LOGS.exception(f"start_audio({self._chat_id}): {e}")
            raise

    async def start_video(self, path: str, with_audio: bool = True) -> None:
        """Start (or switch to) streaming video from *path*."""
        if pytgcalls_client is None:
            raise RuntimeError("PyTgCalls client not initialised.")
        VC_STREAM_FILES[self._chat_id] = path
        stream = self._video_stream(path, with_audio=with_audio)
        try:
            await pytgcalls_client.play(self._chat_id, stream)
            if self._chat_id not in ACTIVE_CALLS:
                ACTIVE_CALLS.append(self._chat_id)
        except Exception as e:
            LOGS.exception(f"start_video({self._chat_id}): {e}")
            raise

    async def stop(self) -> None:
        """Leave voice chat and clean up all state for this chat."""
        if pytgcalls_client is not None:
            with suppress(Exception):
                await pytgcalls_client.leave_call(self._chat_id)
        if self._chat_id in ACTIVE_CALLS:
            ACTIVE_CALLS.remove(self._chat_id)
        VC_PLAYOUT_CALLBACKS.pop(self._chat_id, None)
        VC_STREAM_FILES.pop(self._chat_id, None)

    async def stop_video(self) -> None:
        """Stop the video portion; audio continues."""
        VIDEO_ON.pop(self._chat_id, None)

    async def set_my_volume(self, volume: int) -> None:
        if pytgcalls_client is None:
            return
        volume = max(1, min(200, volume))
        with suppress(Exception):
            await pytgcalls_client.change_volume_call(self._chat_id, volume)

    async def set_is_mute(self, muted: bool) -> None:
        if pytgcalls_client is None:
            return
        with suppress(Exception):
            if muted:
                await pytgcalls_client.mute(self._chat_id)
            else:
                await pytgcalls_client.unmute(self._chat_id)

    async def set_pause(self, paused: bool) -> None:
        if pytgcalls_client is None:
            return
        with suppress(Exception):
            if paused:
                await pytgcalls_client.pause(self._chat_id)
            else:
                await pytgcalls_client.resume(self._chat_id)

    async def reconnect(self) -> None:
        """Re-join the voice chat (used by .rejoin)."""
        if pytgcalls_client is None:
            raise NotInCallError("PyTgCalls client not initialised.")
        with suppress(Exception):
            await pytgcalls_client.leave_call(self._chat_id)
        if self._chat_id in ACTIVE_CALLS:
            ACTIVE_CALLS.remove(self._chat_id)
        path = VC_STREAM_FILES.get(self._chat_id)
        if path:
            await self.start_audio(path)
        else:
            await self.join(self._chat_id)

    def restart_playout(self) -> None:
        """Re-play the current song from the beginning."""
        path = VC_STREAM_FILES.get(self._chat_id)
        if path and pytgcalls_client:
            asyncio.create_task(self.start_audio(path))


# ──────────────────────────────────────────────────────────────────────────────
# VC_AUTHS
# ──────────────────────────────────────────────────────────────────────────────

def VC_AUTHS() -> list:
    _vcsudos = udB.get_key("VC_SUDOS") or []
    return [int(a) for a in [*owner_and_sudos(), *_vcsudos]]


# ──────────────────────────────────────────────────────────────────────────────
# Player
# ──────────────────────────────────────────────────────────────────────────────

class Player:
    def __init__(self, chat: int, event=None, video: bool = False) -> None:
        self._chat         = chat
        self._current_chat = event.chat_id if event else LOG_CHANNEL
        self._video        = video
        if CLIENTS.get(chat):
            self.group_call = CLIENTS[chat]
        else:
            self.group_call = GroupCallWrapper(chat)
            CLIENTS[chat]   = self.group_call

    async def make_vc_active(self):
        """Create a voice chat in the group (owner permission required)."""
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
        # Stop any existing video streams first
        if VIDEO_ON:
            for cid in list(VIDEO_ON):
                with suppress(Exception):
                    await VIDEO_ON[cid].stop()
            VIDEO_ON.clear()
            await asyncio.sleep(1)

        if self._video:
            # Video mode: leave all other chats
            for cid in list(CLIENTS):
                if cid != self._chat:
                    with suppress(Exception):
                        await CLIENTS[cid].stop()
                    CLIENTS.pop(cid, None)
            VIDEO_ON[self._chat] = self.group_call

        if self._chat not in ACTIVE_CALLS:
            self.group_call.on_network_status_changed(self.on_network_changed)
            self.group_call.on_playout_ended(self.playout_ended_handler)
            try:
                await self.group_call.join(self._chat)
            except GroupCallNotFoundError as er:
                LOGS.info(f"No active group call, creating one: {er}")
                ok, err = await self.make_vc_active()
                if err:
                    return False, err
                await asyncio.sleep(2)
                try:
                    await self.group_call.join(self._chat)
                except Exception as e:
                    LOGS.exception(e)
                    return False, e
            except Exception as e:
                LOGS.exception(e)
                return False, e
        return True, None

    async def on_network_changed(self, call, is_connected: bool) -> None:
        chat = self._chat
        if is_connected:
            if chat not in ACTIVE_CALLS:
                ACTIVE_CALLS.append(chat)
        elif chat in ACTIVE_CALLS:
            ACTIVE_CALLS.remove(chat)

    async def playout_ended_handler(self, call, source, mtype) -> None:
        """Called by StreamEnded event — delete downloaded file, advance queue."""
        if source:
            with suppress(Exception):
                if os.path.exists(source):
                    os.remove(source)
        await self.play_from_queue()

    async def play_from_queue(self) -> None:
        chat_id = self._chat
        # Stop video mode if active
        if chat_id in VIDEO_ON:
            with suppress(Exception):
                await self.group_call.stop_video()
            VIDEO_ON.pop(chat_id, None)

        try:
            song, title, link, thumb, from_user, pos, dur = await get_from_queue(chat_id)
        except (IndexError, KeyError):
            # Queue exhausted — leave the voice chat cleanly
            with suppress(Exception):
                await self.group_call.stop()
            CLIENTS.pop(self._chat, None)
            with suppress(Exception):
                await vcClient.send_message(
                    self._current_chat,
                    f"🎵 Queue finished. Left VC: <code>{chat_id}</code>",
                    parse_mode="html",
                )
            return
        except Exception as er:
            LOGS.exception(er)
            with suppress(Exception):
                await vcClient.send_message(
                    self._current_chat,
                    f"<strong>VC Queue Error:</strong> <code>{format_exc()}</code>",
                    parse_mode="html",
                )
            return

        # Start the next song
        try:
            await self.group_call.start_audio(song)
        except ParticipantJoinMissingError:
            await self.vc_joiner()
            with suppress(Exception):
                await self.group_call.start_audio(song)
        except Exception as er:
            LOGS.exception(er)
            return

        # Delete previous "now playing" message
        if chat_id in MSGID_CACHE:
            with suppress(Exception):
                await MSGID_CACHE[chat_id].delete()
            del MSGID_CACHE[chat_id]

        # Send new "now playing" message
        text = (
            f"<strong>🎧 Now playing #{pos}: <a href={link}>{title}</a>"
            f"\n⏰ Duration:</strong> <code>{dur}</code>"
            f"\n👤 <strong>Requested by:</strong> {from_user}"
        )
        try:
            msg = await vcClient.send_message(
                self._current_chat,
                text,
                file=thumb,
                link_preview=False,
                parse_mode="html",
            )
        except ChatSendMediaForbiddenError:
            msg = await vcClient.send_message(
                self._current_chat,
                text,
                link_preview=False,
                parse_mode="html",
            )
        except Exception:
            msg = None

        if msg:
            MSGID_CACHE[chat_id] = msg

        # Remove played item from queue
        with suppress(Exception):
            VC_QUEUE[chat_id].pop(pos)
            if not VC_QUEUE[chat_id]:
                VC_QUEUE.pop(chat_id)

    async def vc_joiner(self) -> bool:
        chat_id = self._chat
        done, err = await self.startCall()
        if done:
            with suppress(Exception):
                await vcClient.send_message(
                    self._current_chat,
                    f"• Joined VC in <code>{chat_id}</code>",
                    parse_mode="html",
                )
            return True
        with suppress(Exception):
            await vcClient.send_message(
                self._current_chat,
                f"<strong>ERROR joining VC</strong> <code>{chat_id}</code>:\n<code>{err}</code>",
                parse_mode="html",
            )
        return False


# ──────────────────────────────────────────────────────────────────────────────
# vc_asst  —  decorator for vcbot command handlers
# ──────────────────────────────────────────────────────────────────────────────

def vc_asst(dec, **kwargs):
    def ult(func):
        kwargs["func"] = (
            lambda e: not e.is_private and not e.via_bot_id and not e.fwd_from
        )
        handler = udB.get_key("VC_HNDLR") or HNDLR
        kwargs["pattern"] = compile_pattern(dec, handler)
        vc_auth = kwargs.pop("vc_auth", True)

        async def vc_handler(e):
            key    = udB.get_key("VC_AUTH_GROUPS") or {}
            VCAUTH = list(key.keys())
            if not (
                e.out
                or (e.sender_id in VC_AUTHS())
                or (vc_auth and e.chat_id in VCAUTH)
            ):
                return
            if vc_auth and key.get(e.chat_id):
                if key[e.chat_id].get("admins") and not (await admin_check(e)):
                    return
            try:
                await func(e)
            except Exception as _vc_err:
                LOGS.exception(_vc_err)
                with suppress(Exception):
                    await asst.send_message(
                        LOG_CHANNEL,
                        f"VC Error — <code>{UltVer}</code>\n\n"
                        f"<code>{e.text}</code>\n\n"
                        f"<code>{format_exc()}</code>",
                        parse_mode="html",
                    )

        vcClient.add_event_handler(vc_handler, events.NewMessage(**kwargs))

    return ult


# ──────────────────────────────────────────────────────────────────────────────
# Queue helpers
# ──────────────────────────────────────────────────────────────────────────────

def add_to_queue(
    chat_id: int,
    song,
    song_name: str,
    link: str,
    thumb,
    from_user: str,
    duration: str,
) -> dict:
    try:
        play_at = sorted(VC_QUEUE[chat_id].keys())[-1] + 1
    except (KeyError, IndexError):
        play_at = 1
    entry = {
        play_at: {
            "song":      song,
            "title":     song_name,
            "link":      link,
            "thumb":     thumb,
            "from_user": from_user,
            "duration":  duration,
        }
    }
    if VC_QUEUE.get(chat_id):
        VC_QUEUE[int(chat_id)].update(entry)
    else:
        VC_QUEUE[chat_id] = entry
    return VC_QUEUE[chat_id]


def list_queue(chat: int) -> str:
    if not VC_QUEUE.get(chat):
        return ""
    lines = []
    for n, x in enumerate(list(VC_QUEUE[chat].keys())[:18], start=1):
        d = VC_QUEUE[chat][x]
        lines.append(
            f'<strong>{n}. <a href={d["link"]}>{d["title"]}</a>:</strong> '
            f'<i>By: {d["from_user"]}</i>'
        )
    return "\n".join(lines) + "\n\n....."


async def get_from_queue(chat_id: int):
    pos  = list(VC_QUEUE[int(chat_id)].keys())[0]
    info = VC_QUEUE[int(chat_id)][pos]
    song = info.get("song")
    if not song:
        song = await get_stream_link(info["link"])
    return (
        song,
        info["title"],
        info["link"],
        info["thumb"],
        info["from_user"],
        pos,
        info["duration"],
    )


# ──────────────────────────────────────────────────────────────────────────────
# Download / stream helpers
# ──────────────────────────────────────────────────────────────────────────────

async def get_stream_link(ytlink: str) -> str:
    """Return the best-audio direct stream URL via yt-dlp."""
    out, _ = await bash(
        f'yt-dlp -g -f "bestaudio[ext=m4a]/bestaudio/best" -- "{ytlink}"'
    )
    # yt-dlp may output multiple lines (e.g. for HLS); take the first non-empty one
    lines = [l.strip() for l in (out or "").splitlines() if l.strip()]
    if not lines:
        raise RuntimeError(f"yt-dlp returned no stream URL for: {ytlink}")
    return lines[0]


async def download(query: str):
    """Search YouTube (or use direct URL) and return stream info."""
    if query.startswith("https://") and "youtube" not in query.lower() and "youtu.be" not in query.lower():
        return query, None, query, query, "Unknown"

    if VideosSearch is None:
        raise ImportError("'youtube-search-python' not installed.")
    search = VideosSearch(query, limit=1).result()
    data   = search["result"][0]
    link   = data["link"]
    title  = data["title"]
    dur    = data.get("duration") or "♾"
    thumb  = f"https://i.ytimg.com/vi/{data['id']}/hqdefault.jpg"
    dl     = await get_stream_link(link)
    return dl, thumb, title, link, dur


async def vid_download(query: str):
    """Search YouTube for a video and return stream info."""
    if VideosSearch is None:
        raise ImportError("'youtube-search-python' not installed.")
    search = VideosSearch(query, limit=1).result()
    data   = search["result"][0]
    link   = data["link"]
    video  = await get_stream_link(link)
    title  = data["title"]
    thumb  = f"https://i.ytimg.com/vi/{data['id']}/hqdefault.jpg"
    dur    = data.get("duration") or "♾"
    return video, thumb, title, link, dur


async def dl_playlist(chat: int, from_user: str, link: str):
    """Enqueue all videos from a YouTube playlist; return first item info."""
    links = await get_videos_link(link)
    if not links:
        raise ValueError("No links found in playlist.")

    if VideosSearch is None:
        raise ImportError("'youtube-search-python' not installed.")

    # First item — returned to caller for immediate play
    search = VideosSearch(links[0], limit=1).result()
    vid1   = search["result"][0]
    song   = await get_stream_link(vid1["link"])
    thumb  = f"https://i.ytimg.com/vi/{vid1['id']}/hqdefault.jpg"
    dur    = vid1.get("duration") or "♾"
    title  = vid1["title"]

    # Remaining items — enqueue in background
    async def _enqueue_rest():
        for z in links[1:]:
            with suppress(Exception):
                s = VideosSearch(z, limit=1).result()
                v = s["result"][0]
                add_to_queue(
                    chat, None,
                    v["title"], v["link"],
                    f"https://i.ytimg.com/vi/{v['id']}/hqdefault.jpg",
                    from_user,
                    v.get("duration") or "♾",
                )
    asyncio.create_task(_enqueue_rest())

    return song, thumb, title, vid1["link"], dur


async def file_download(event, reply, fast_download: bool = True):
    """Download a media file from a replied message and return info."""
    thumb = "https://telegra.ph/file/22bb2349da20c7524e4db.mp4"
    title = (
        getattr(reply.file, "title", None)
        or getattr(reply.file, "name", None)
        or f"{str(time())}.mp4"
    )
    fname = getattr(reply.file, "name", None) or f"{str(time())}.mp4"

    if fast_download:
        dl_obj = await downloader(
            f"vcbot/downloads/{fname}",
            reply.media.document,
            event,
            time(),
            f"Downloading {title}...",
        )
        dl = dl_obj.name
    else:
        dl = await reply.download_media()

    dur = (
        time_formatter(reply.file.duration * 1000)
        if getattr(reply.file, "duration", None)
        else "🤷‍♂️"
    )

    # Thumbnail
    doc = getattr(reply, "document", None)
    if doc and getattr(doc, "thumbs", None):
        with suppress(Exception):
            thumb = await reply.download_media("vcbot/downloads/", thumb=-1)

    link = getattr(reply, "message_link", None) or ""
    return dl, thumb, title, link, dur


# ──────────────────────────────────────────────────────────────────────────────
