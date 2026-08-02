# Ultroid - UserBot
# Copyright (C) 2021-2022 TeamUltroid
#
# This file is a part of < https://github.com/TeamUltroid/Ultroid/ >
# PLease read the GNU Affero General Public License in
# <https://www.github.com/TeamUltroid/Ultroid/blob/main/LICENSE/>.

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
from . import vc_asst, Player, get_string


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
    ultSongs.group_call.restart_playout()
    await event.eor("`Re-playing the current song.`")


from pyChampu import udB, HNDLR
from pyChampu._misc._decorators import champu_cmd


@champu_cmd(pattern="vcsession( (.*)|$)", fullsudo=True)
async def vcsession_cmd(event):
    match = event.pattern_match.group(1).strip()
    if not match:
        current = udB.get_key("VC_SESSION")
        if current:
            masked = str(current)[:10] + "..." + str(current)[-10:]
            return await event.eor(
                f"🎧 **VC_SESSION Status:**\n\n"
                f"• **Custom VC_SESSION:** Active (`{masked}`)\n\n"
                f"💡 **Usage:**\n"
                f"• `{HNDLR}vcsession set <session_string>` — Set/Update session\n"
                f"• `{HNDLR}vcsession del` — Remove custom session (Use Main Account)"
            )
        return await event.eor(
            f"🎧 **VC_SESSION Status:**\n\n"
            f"• **Custom VC_SESSION:** Not Set (Using **Main Userbot Account**)\n\n"
            f"💡 **Usage:**\n"
            f"• `{HNDLR}vcsession set <session_string>` — Set/Update custom session\n"
            f"• `{HNDLR}vcsession del` — Remove custom session"
        )

    parts = match.split(maxsplit=1)
    subcmd = parts[0].lower()

    if subcmd in ["set", "add"]:
        sess_str = parts[1].strip() if len(parts) > 1 else ""
        if not sess_str and event.reply_to_msg_id:
            reply = await event.get_reply_message()
            sess_str = reply.text.strip() if reply and reply.text else ""
        if not sess_str:
            return await event.eor("Please provide a session string or reply to a message containing it!")

        udB.set_key("VC_SESSION", sess_str)
        return await event.eor("✅ **VC_SESSION updated in database!**\nRestart userbot (`.restart`) to apply changes.")

    elif subcmd in ["del", "delete", "remove", "rem"]:
        udB.del_key("VC_SESSION")
        return await event.eor("✅ **VC_SESSION deleted from database!**\nUserbot will now use your **Main Account** for VC Bot.")

    return await event.eor(f"Unknown subcommand! Use `{HNDLR}vcsession` to view status & commands.")
