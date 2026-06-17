"""
✘ Commands Available -

`{i}ytplaylist <playlist link>`
  play whole playlist in voice chat

"""

import re
from . import vc_asst, get_string, inline_mention, Player, dl_playlist, add_to_queue, is_url_ok, VC_QUEUE


@vc_asst("ytplaylist(?: |$)")
async def live_stream(e):
    xx = await e.eor(get_string("com_1"))
    chat = e.chat_id
    song = None
    if len(e.text.split()) > 1:
        input_str = e.text.split(maxsplit=1)[1]
        tiny_input = input_str.split()[0]
        if tiny_input[0] in ["@", "-"]:
            try:
                chat = await e.client.parse_id(tiny_input)
            except Exception as er:
                return await xx.edit(str(er))
            try:
                song = input_str.split(maxsplit=1)[1]
            except IndexError:
                pass
        else:
            song = input_str
    if not song:
        return await xx.eor("Are You Kidding Me?\nWhat to Play?")
    if not (re.search("youtu", song) and re.search("playlist\\?list", song)):
        return await xx.eor(get_string("vcbot_8"))
    if not is_url_ok(song):
        return await xx.eor("`Only Youtube Playlist please.`")
    await xx.edit(get_string("vcbot_7"))
    ultSongs = Player(chat, e)
    was_connected = ultSongs.group_call.is_connected
    if not was_connected and not (await ultSongs.vc_joiner(announce=False)):
        return
    file, thumb, title, link, duration = await dl_playlist(
        chat, inline_mention(e), song
    )
    if not was_connected:
        from_user = inline_mention(e.sender)
        await xx.reply(
            "🎸 **Now playing:** [{}]({})\n⏰ **Duration:** `{}`\n👥 **Chat:** `{}`\n🙋‍♂ **Requested by:** {}".format(
                f"{title[:30]}...", link, duration, chat, from_user
            ),
            file=thumb,
            link_preview=False,
        )

        await xx.delete()
        await ultSongs.group_call.start_audio(file)
    else:
        from_user = inline_mention(e)
        add_to_queue(chat, file, title, link, thumb, from_user, duration)
        return await xx.eor(
            f"▶ Added 🎵 **[{title}]({link})** to queue at #{list(VC_QUEUE[chat].keys())[-1]}.",
        )
