"""
✘ Commands Available -

• `{i}mutevc`
   Mute playback.

• `{i}unmutevc`
   UnMute playback.

• `{i}pausevc`
   Pause playback.

• `{i}resumevc`
   Resume playback.

• `{i}replay`
   Re-play the current song from the beginning.
"""
from . import vc_asst, Player, get_string, udB, YT_COOKIES_DIR, _ytdlp_cookie_files, LAST_WORKING_COOKIE_FILE, _ordered_cookie_files, _extract_stream_with_ytdlp
import os


@vc_asst("mutevc")
async def mute(event):
    if len(event.text.split()) > 1:
        chat = event.text.split()[1]
        try:
            chat = await event.client.parse_id(chat)
        except Exception as e:
            return await event.eor(f"**ERROR:**\n{str(e)}")
    else:
        chat = event.chat_id
    ultSongs = Player(chat)
    if not ultSongs.group_call.is_connected:
        return await event.eor(get_string("vcbot_6"))
    await ultSongs.group_call.set_is_mute(True)
    await event.eor(get_string("vcbot_12"))


@vc_asst("unmutevc")
async def unmute(event):
    if len(event.text.split()) > 1:
        chat = event.text.split()[1]
        try:
            chat = await event.client.parse_id(chat)
        except Exception as e:
            return await event.eor(f"**ERROR:**\n{str(e)}")
    else:
        chat = event.chat_id
    ultSongs = Player(chat)
    if not ultSongs.group_call.is_connected:
        return await event.eor(get_string("vcbot_6"))
    await ultSongs.group_call.set_is_mute(False)
    await event.eor("`UnMuted playback in this chat.`")


@vc_asst("pausevc")
async def pauser(event):
    if len(event.text.split()) > 1:
        chat = event.text.split()[1]
        try:
            chat = await event.client.parse_id(chat)
        except Exception as e:
            return await event.eor(f"**ERROR:**\n{str(e)}")
    else:
        chat = event.chat_id
    ultSongs = Player(chat)
    if not ultSongs.group_call.is_connected:
        return await event.eor(get_string("vcbot_6"))
    await ultSongs.group_call.set_pause(True)
    await event.eor(get_string("vcbot_14"))


@vc_asst("resumevc")
async def resumer(event):
    if len(event.text.split()) > 1:
        chat = event.text.split()[1]
        try:
            chat = await event.client.parse_id(chat)
        except Exception as e:
            return await event.eor(f"**ERROR:**\n{str(e)}")
    else:
        chat = event.chat_id
    ultSongs = Player(chat)
    if not ultSongs.group_call.is_connected:
        return await event.eor(get_string("vcbot_6"))
    await ultSongs.group_call.set_pause(False)
    await event.eor(get_string("vcbot_13"))


@vc_asst("replay")
async def replayer(event):
    if len(event.text.split()) > 1:
        chat = event.text.split()[1]
        try:
            chat = await event.client.parse_id(chat)
        except Exception as e:
            return await event.eor(f"**ERROR:**\n{str(e)}")
    else:
        chat = event.chat_id
    ultSongs = Player(chat)
    if not ultSongs.group_call.is_connected:
        return await event.eor(get_string("vcbot_6"))
    ultSongs.group_call.restart_playout()
    await event.eor("`Re-playing the current song.`")


@vc_asst("checkcookies")
async def check_cookies(event):
    """Report available cookie files, env/db cookie settings, and last-working cookie."""
    files = _ytdlp_cookie_files()
    env_vals = []
    for key in ("YTDLP_COOKIES_FILE", "YT_COOKIES_FILE", "YTDLP_COOKIES", "YT_COOKIES"):
        v = os.getenv(key)
        if v:
            env_vals.append(f"{key}={v}")
    try:
        db_cookie = udB.get_key("YTDLP_COOKIES_FILE") or udB.get_key("YT_COOKIES_FILE")
    except Exception:
        db_cookie = None

    lines = []
    lines.append(f"Cookies dir: {YT_COOKIES_DIR}")
    lines.append(f"Cookie files found: {len(files)}")
    for p in files:
        mark = " (last OK)" if LAST_WORKING_COOKIE_FILE and p == LAST_WORKING_COOKIE_FILE else ""
        lines.append(f" - {os.path.basename(p)}{mark}")
    lines.append(f"Env cookie vars: {', '.join(env_vals) if env_vals else 'None'}")
    lines.append(f"DB cookie entry: {db_cookie or 'None'}")
    if LAST_WORKING_COOKIE_FILE:
        lines.append(f"Remembered cookie: {os.path.basename(LAST_WORKING_COOKIE_FILE)}")
    else:
        lines.append("Remembered cookie: None (using no-cookie mode first)")

    lines.append("")
    lines.append("To enable cookies: place .txt/.json cookie files in the resources/cookies folder or set YTDLP_COOKIES_FILE env/db key.")

    await event.eor("\n".join(lines))


@vc_asst("checkcookies probe")
async def probe_cookies(event):
    """Probe cookie files against a provided YouTube link to see which allow extraction.

    Usage: `checkcookies probe <youtube_url>`
    """
    parts = event.text.split(maxsplit=2)
    if len(parts) < 3:
        return await event.eor("Usage: checkcookies probe <youtube_url>")
    url = parts[2].strip()
    msg = await event.eor("`Probing yt-dlp with available cookie modes...`")
    results = []

    modes = [None] + _ordered_cookie_files()
    for cookie in modes:
        label = "no-cookie" if not cookie else os.path.basename(cookie)
        start = time()
        try:
            direct, err = await _extract_stream_with_ytdlp(url, cookie_file=cookie, prefer_audio=True)
            took = round(time() - start, 2)
            if direct:
                results.append(f"✅ {label} — OK ({took}s): {direct[:200]}")
                # remember successful cookie
                if cookie:
                    global LAST_WORKING_COOKIE_FILE
                    LAST_WORKING_COOKIE_FILE = cookie
                break
            else:
                results.append(f"❌ {label} — failed ({took}s): {str(err)[:200]}")
        except Exception as e:
            took = round(time() - start, 2)
            results.append(f"❌ {label} — exception ({took}s): {str(e)[:200]}")

    await msg.edit("\n".join(results))
