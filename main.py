import os
import asyncio
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from discord.abc import Messageable
import wavelink
from dotenv import load_dotenv

load_dotenv()


def load_tokens() -> list[str]:
    tokens: list[str] = []
    raw = os.getenv("DISCORD_BOT_TOKENS", "")
    if raw:
        raw = raw.replace("\n", ",")
        tokens.extend([t.strip() for t in raw.split(",") if t.strip()])
    for key, val in os.environ.items():
        if key.startswith("DISCORD_TOKEN"):
            val = val.strip()
            if val:
                tokens.append(val)
    seen = set()
    unique_tokens: list[str] = []
    for token in tokens:
        if token not in seen:
            unique_tokens.append(token)
            seen.add(token)
    return unique_tokens


def get_env_emoji(key: str, default: str) -> str:
    val = os.getenv(key, default).strip()
    return val or default


def parse_emoji(val: str | None) -> Optional[str]:
    if not val:
        return None
    val = val.strip()
    if not val:
        return None
    if val.isdigit():
        return f"<:custom:{val}>"
    return val


TOKENS = load_tokens()
PREFIX = os.getenv("DISCORD_PREFIX", "!")
FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg")
VOICE_RECONNECT_INTERVAL = max(5.0, float(os.getenv("VOICE_RECONNECT_INTERVAL", "20")))
MAX_MESSAGE_CACHE = max(100, int(os.getenv("DISCORD_MAX_MESSAGES", "200")))
TRACK_RETRY_LIMIT = max(0, int(os.getenv("TRACK_RETRY_LIMIT", "3")))
MANUAL_LEAVE_GRACE = max(0.0, float(os.getenv("MANUAL_LEAVE_GRACE", "15")))

LAVA_HOST = os.getenv("LAVA_HOST", "127.0.0.1")
LAVA_PORT = int(os.getenv("LAVA_PORT", "2333"))
LAVA_PASSWORD = os.getenv("LAVA_PASSWORD", "youshallnotpass")
LAVA_SSL = os.getenv("LAVA_SSL", "false").lower() in {"true", "1", "yes"}

DEFAULT_EMOJIS = {
    "pause": get_env_emoji("EMOJI_PAUSE", "⏸️"),
    "resume": get_env_emoji("EMOJI_RESUME", "▶️"),
    "stop": get_env_emoji("EMOJI_STOP", "⏹️"),
    "skip": get_env_emoji("EMOJI_SKIP", "⏭️"),
    "restart": get_env_emoji("EMOJI_RESTART", "🔁"),
    "vol_up": get_env_emoji("EMOJI_VOL_UP", "🔼"),
    "vol_down": get_env_emoji("EMOJI_VOL_DOWN", "🔽"),
}

GLOBAL_EMOJI_OVERRIDES: dict[int, dict[str, str]] = {}


def get_guild_emojis(guild_id: int | None) -> dict[str, str]:
    base = DEFAULT_EMOJIS.copy()
    if guild_id and guild_id in GLOBAL_EMOJI_OVERRIDES:
        base.update(GLOBAL_EMOJI_OVERRIDES[guild_id])
    return base


class ControlView(discord.ui.View):
    def __init__(
        self,
        owner: discord.Member,
        emojis: dict[str, str],
        bot: "MusicBot",
        guild: discord.Guild,
    ):
        super().__init__(timeout=180)
        self.owner_id = owner.id
        self.emojis = emojis
        self.bot_ref = bot
        self.guild = guild
        self._apply_emojis()

    def _apply_emojis(self):
        for item in self.children:
            if not isinstance(item, discord.ui.Button):
                continue
            cid = getattr(item, "custom_id", "")
            if cid == "control_play":
                item.emoji = self.emojis.get("resume")
            elif cid == "control_stop":
                item.emoji = self.emojis.get("stop")
            elif cid == "control_skip":
                item.emoji = self.emojis.get("skip")
            elif cid == "control_restart":
                item.emoji = self.emojis.get("restart")
            elif cid == "control_vol_up":
                item.emoji = self.emojis.get("vol_up")
            elif cid == "control_vol_down":
                item.emoji = self.emojis.get("vol_down")

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "هذا البانل مخصص لمن أنشأه فقط.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="\u200b", style=discord.ButtonStyle.secondary, custom_id="control_play")
    async def play_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.bot_ref.resume_track(interaction.guild, interaction.response)

    @discord.ui.button(label="\u200b", style=discord.ButtonStyle.secondary, custom_id="control_stop")
    async def stop_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.bot_ref.stop_track(interaction.guild, interaction.response)

    @discord.ui.button(label="\u200b", style=discord.ButtonStyle.secondary, custom_id="control_skip")
    async def skip_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.bot_ref.skip_track(interaction.guild, interaction.response)

    @discord.ui.button(label="\u200b", style=discord.ButtonStyle.secondary, custom_id="control_restart")
    async def restart_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.bot_ref.restart_track(interaction.guild, interaction.response)

    @discord.ui.button(label="\u200b", style=discord.ButtonStyle.secondary, custom_id="control_vol_up")
    async def volume_up_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.bot_ref.change_volume(interaction.guild, 10, interaction.response)

    @discord.ui.button(label="\u200b", style=discord.ButtonStyle.secondary, custom_id="control_vol_down")
    async def volume_down_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.bot_ref.change_volume(interaction.guild, -10, interaction.response)


class MusicBot(commands.Cog):
    def __init__(self, bot: commands.Bot, storage_dir: Path):
        self.bot = bot
        self.storage_dir = storage_dir
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.text_channels: dict[int, int] = {}
        self.last_tracks: dict[int, wavelink.Playable | None] = {}
        self.volumes: dict[int, int] = {}
        self.manual_disconnects: dict[int, float] = {}
        self.rejoin_tasks: dict[int, asyncio.Task] = {}
        self.bot.loop.create_task(self._connect_lavalink())

    async def _connect_lavalink(self):
        await self.bot.wait_until_ready()
        uri = f"{'https' if LAVA_SSL else 'http'}://{LAVA_HOST}:{LAVA_PORT}"
        node = wavelink.Node(
            identifier="MAIN",
            uri=uri,
            password=LAVA_PASSWORD,
            secure=LAVA_SSL,
        )
        await wavelink.NodePool.connect(client=self.bot, nodes=[node])
        print(f"✅ Lavalink node ready at {uri}")

    def _player(self, guild: discord.Guild) -> Optional[wavelink.Player]:
        vc = guild.voice_client
        if vc and isinstance(vc, wavelink.Player):
            return vc
        return None

    async def _connect_player(
        self, ctx: commands.Context, channel: Optional[discord.VoiceChannel] = None
    ) -> Optional[wavelink.Player]:
        if not ctx.guild:
            return None
        player = self._player(ctx.guild)
        if player:
            return player
        target = channel or (ctx.author.voice.channel if ctx.author.voice else None)
        if not target:
            await ctx.send("⚠️ ادخل روم صوتي أولاً.")
            return None
        player = await target.connect(cls=wavelink.Player)
        player.autoplay = wavelink.AutoPlayMode.disabled
        player.queue = wavelink.Queue()
        self.volumes[ctx.guild.id] = 100
        await player.set_volume(100)
        return player

    def _record_text_channel(
        self, guild: discord.Guild | None, channel: Optional[discord.abc.GuildChannel]
    ):
        if not guild or not channel:
            return
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            self.text_channels[guild.id] = channel.id

    async def _maybe_send_panel(self, guild: discord.Guild, track: wavelink.Playable):
        channel_id = self.text_channels.get(guild.id)
        channel = guild.get_channel(channel_id) if channel_id else None
        if isinstance(channel, discord.Thread):
            panel_channel: Optional[Messageable] = channel
        elif isinstance(channel, discord.TextChannel):
            panel_channel = channel
        else:
            panel_channel = None
        if not panel_channel:
            return
        member = guild.me
        if not member:
            return
        await self.send_panel(panel_channel, member, guild, track_title=track.title)

    @commands.Cog.listener()
    async def on_wavelink_track_start(self, player: wavelink.Player, track: wavelink.Playable):
        self.last_tracks[player.guild.id] = track
        await self._maybe_send_panel(player.guild, track)

    @commands.Cog.listener()
    async def on_wavelink_track_end(self, player: wavelink.Player, track: wavelink.Playable, reason):
        if not player.queue.is_empty:
            next_track = player.queue.get()
            await player.play(next_track)
        else:
            self.last_tracks[player.guild.id] = track

    async def prepare_track(self, query: str) -> Optional[wavelink.Playable]:
        try:
            track = await wavelink.YouTubeTrack.search(query=query, return_first=True)
        except Exception:
            return None
        return track

    async def add_to_queue(
        self, ctx: commands.Context, player: wavelink.Player, track: wavelink.Playable
    ):
        track.extra = {
            "requester_id": ctx.author.id,
            "text_channel_id": ctx.channel.id if isinstance(ctx.channel, (discord.TextChannel, discord.Thread)) else None,
        }
        if not player.is_playing():
            await player.play(track)
            await ctx.send(f"▶️ يتم الآن تشغيل **{track.title}**.")
        else:
            player.queue.put(track)
            await ctx.send(f"✅ أضيف **{track.title}** إلى قائمة الانتظار.")

    async def skip_track(
        self,
        guild: discord.Guild | None,
        response: Optional[discord.InteractionResponse] = None,
    ) -> Optional[str]:
        if not guild:
            return "⚠️ لا يوجد سيرفر."
        player = self._player(guild)
        if not player or not player.is_playing():
            msg = "⚠️ لا يوجد تشغيل حالي."
            if response:
                await response.send_message(msg, ephemeral=True)
                return None
            return msg
        if not player.queue.is_empty:
            next_track = player.queue.get()
            await player.play(next_track)
        else:
            await player.stop()
        msg = "⏭️ تم التخطي."
        if response:
            await response.send_message(msg, ephemeral=True)
            return None
        return msg

    async def stop_track(
        self,
        guild: discord.Guild | None,
        response: Optional[discord.InteractionResponse] = None,
    ) -> Optional[str]:
        if not guild:
            return "⚠️ لا يوجد سيرفر."
        player = self._player(guild)
        if not player:
            msg = "⚠️ غير متصل بقناة صوتية."
            if response:
                await response.send_message(msg, ephemeral=True)
                return None
            return msg
        player.queue.clear()
        await player.stop()
        msg = "⏹️ تم الإيقاف."
        if response:
            await response.send_message(msg, ephemeral=True)
            return None
        return msg

    async def restart_track(
        self,
        guild: discord.Guild | None,
        response: Optional[discord.InteractionResponse] = None,
    ) -> Optional[str]:
        if not guild:
            return "⚠️ لا يوجد سيرفر."
        player = self._player(guild)
        if not player:
            msg = "⚠️ غير متصل بقناة صوتية."
            if response:
                await response.send_message(msg, ephemeral=True)
                return None
            return msg
        last = self.last_tracks.get(guild.id)
        if not last:
            msg = "⚠️ لا يوجد مسار لإعادته."
            if response:
                await response.send_message(msg, ephemeral=True)
                return None
            return msg
        await player.play(last)
        msg = "🔁 تمت إعادة تشغيل المسار."
        if response:
            await response.send_message(msg, ephemeral=True)
            return None
        return msg

    async def resume_track(
        self,
        guild: discord.Guild | None,
        response: Optional[discord.InteractionResponse] = None,
    ) -> Optional[str]:
        if not guild:
            return "⚠️ لا يوجد سيرفر."
        player = self._player(guild)
        if not player:
            msg = "⚠️ غير متصل بقناة صوتية."
            if response:
                await response.send_message(msg, ephemeral=True)
                return None
            return msg
        if player.is_paused():
            await player.resume()
            msg = "▶️ تم الاستئناف."
        elif not player.is_playing() and not player.queue.is_empty:
            next_track = player.queue.get()
            await player.play(next_track)
            msg = "▶️ يتم الآن التشغيل."
        else:
            msg = "⚠️ لا يوجد شيء للتشغيل."
        if response:
            await response.send_message(msg, ephemeral=True)
            return None
        return msg

    async def change_volume(
        self,
        guild: discord.Guild | None,
        delta: int,
        response: Optional[discord.InteractionResponse] = None,
    ) -> Optional[str]:
        if not guild:
            return "⚠️ لا يوجد سيرفر."
        player = self._player(guild)
        if not player:
            msg = "⚠️ غير متصل بقناة صوتية."
            if response:
                await response.send_message(msg, ephemeral=True)
                return None
            return msg
        current = self.volumes.get(guild.id, 100)
        new = max(10, min(200, current + delta))
        self.volumes[guild.id] = new
        await player.set_volume(new)
        msg = f"🔊 مستوى الصوت الآن {new}%."
        if response:
            await response.send_message(msg, ephemeral=True)
            return None
        return msg

    async def send_panel(
        self,
        channel: Messageable,
        member: discord.Member,
        guild: Optional[discord.Guild] = None,
        track_title: Optional[str] = None,
    ):
        target_guild = guild or getattr(channel, "guild", None)
        if not target_guild:
            return
        panel_text = "\u200b"
        if track_title:
            panel_text = f"🎶 **{track_title.strip() or 'غير معروف'}**"
        emojis = get_guild_emojis(target_guild.id)
        view = ControlView(member, emojis, self, target_guild)
        try:
            await channel.send(panel_text, view=view)
        except Exception as exc:
            print(f"[panel] failed to send in {target_guild.name}: {exc}")

    @commands.command(name="join")
    async def join(self, ctx, channel: discord.VoiceChannel | None = None):
        player = await self._connect_player(ctx, channel)
        if player and ctx.guild:
            self._record_text_channel(ctx.guild, ctx.channel)
            await ctx.send(f"✅ انضممت إلى: {player.channel.name}")

    @commands.command(name="شغل")
    async def play_ar(self, ctx, *, query: str):
        if not ctx.guild:
            return
        player = await self._connect_player(ctx)
        if not player:
            return
        self._record_text_channel(ctx.guild, ctx.channel)
        track = await self.prepare_track(query)
        if not track:
            await ctx.send("⚠️ لم أستطع جلب الصوت.")
            return
        await self.add_to_queue(ctx, player, track)

    @commands.command()
    async def stop(self, ctx):
        msg = await self.stop_track(ctx.guild)
        if msg:
            await ctx.send(msg)

    @commands.command()
    async def pause(self, ctx):
        player = self._player(ctx.guild) if ctx.guild else None
        if not player or not player.is_playing():
            await ctx.send("⚠️ لا يوجد تشغيل.")
            return
        await player.pause()
        await ctx.send("⏸️ تم الإيقاف.")

    @commands.command()
    async def resume(self, ctx):
        msg = await self.resume_track(ctx.guild)
        if msg:
            await ctx.send(msg)

    @commands.command()
    async def skip(self, ctx):
        msg = await self.skip_track(ctx.guild)
        if msg:
            await ctx.send(msg)

    @commands.command()
    async def leave(self, ctx):
        if not ctx.guild:
            return
        player = self._player(ctx.guild)
        if player:
            self.manual_disconnects[ctx.guild.id] = asyncio.get_event_loop().time()
            await player.disconnect()
            await ctx.send("👋 تم قطع الاتصال.")

    @commands.command()
    async def panel(self, ctx):
        if not ctx.guild:
            return
        self._record_text_channel(ctx.guild, ctx.channel)
        player = self._player(ctx.guild)
        title = player.current.title if player and player.current else None
        await self.send_panel(ctx.channel, ctx.author, ctx.guild, track_title=title)

    @commands.command()
    async def setemojis(
        self,
        ctx,
        pause: str | None = None,
        resume: str | None = None,
        stop: str | None = None,
        skip: str | None = None,
        restart: str | None = None,
        volup: str | None = None,
        voldown: str | None = None,
    ):
        if not ctx.guild:
            await ctx.send("⚠️ داخل سيرفر فقط.")
            return
        overrides = GLOBAL_EMOJI_OVERRIDES.setdefault(ctx.guild.id, {})
        if (em := parse_emoji(pause)):
            overrides["pause"] = em
        if (em := parse_emoji(resume)):
            overrides["resume"] = em
        if (em := parse_emoji(stop)):
            overrides["stop"] = em
        if (em := parse_emoji(skip)):
            overrides["skip"] = em
        if (em := parse_emoji(restart)):
            overrides["restart"] = em
        if (em := parse_emoji(volup)):
            overrides["vol_up"] = em
        if (em := parse_emoji(voldown)):
            overrides["vol_down"] = em
        await ctx.send("✅ الإيموجيات تم تحديثها لجميع البوتات.")

    @app_commands.command(name="join", description="إدخال البوت قناة صوتية محددة")
    @app_commands.describe(channel="القناة الصوتية (اختياري)")
    async def slash_join(self, interaction: discord.Interaction, channel: discord.VoiceChannel | None = None):
        if not interaction.guild:
            return
        ctx = await commands.Context.from_interaction(interaction)  # type: ignore
        await self.join(ctx, channel=channel)
        await interaction.response.send_message("✅ تمت المعالجة.", ephemeral=True)

    @app_commands.command(name="panel", description="إظهار لوحة التحكم الخاصة بالبث")
    async def slash_panel(self, interaction: discord.Interaction):
        if not interaction.guild or not interaction.channel:
            return
        self._record_text_channel(interaction.guild, interaction.channel)
        player = self._player(interaction.guild)
        title = player.current.title if player and player.current else None
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.send_panel(
            interaction.channel,
            interaction.user,
            interaction.guild,
            track_title=title,
        )
        await interaction.followup.send("✅ أرسلت اللوحة.", ephemeral=True)

    @app_commands.command(name="setemojis", description="تغيير إيموجيات البانال للجميع")
    async def slash_setemojis(
        self,
        interaction: discord.Interaction,
        pause: str | None = None,
        resume: str | None = None,
        stop: str | None = None,
        skip: str | None = None,
        restart: str | None = None,
        volup: str | None = None,
        voldown: str | None = None,
    ):
        if not interaction.guild:
            return
        overrides = GLOBAL_EMOJI_OVERRIDES.setdefault(interaction.guild.id, {})
        if (em := parse_emoji(pause)):
            overrides["pause"] = em
        if (em := parse_emoji(resume)):
            overrides["resume"] = em
        if (em := parse_emoji(stop)):
            overrides["stop"] = em
        if (em := parse_emoji(skip)):
            overrides["skip"] = em
        if (em := parse_emoji(restart)):
            overrides["restart"] = em
        if (em := parse_emoji(volup)):
            overrides["vol_up"] = em
        if (em := parse_emoji(voldown)):
            overrides["vol_down"] = em
        await interaction.response.send_message("✅ تم تحديث الإيموجيات.", ephemeral=True)


def build_bot() -> commands.Bot:
    storage_dir = Path("bot_data")
    intents = discord.Intents.default()
    intents.message_content = True
    intents.voice_states = True
    bot = commands.Bot(
        command_prefix=commands.when_mentioned_or(PREFIX),
        intents=intents,
        max_messages=MAX_MESSAGE_CACHE,
    )

    @bot.event
    async def on_ready():
        print(f"✅ {bot.user} جاهز.")

    async def setup_hook():
        await bot.add_cog(MusicBot(bot, storage_dir))
        await bot.tree.sync()

    bot.setup_hook = setup_hook  # type: ignore
    return bot


async def launch_bot(token: str, index: int, restart_delay: float):
    attempt = 0
    while True:
        bot = build_bot()
        try:
            await bot.start(token)
        except discord.LoginFailure as exc:
            print(f"❌ فشل تسجيل الدخول للبوت رقم {index + 1}: {exc}")
            await bot.close()
            return
        except asyncio.CancelledError:
            await bot.close()
            raise
        except Exception as exc:
            attempt += 1
            await bot.close()
            wait_time = max(1.0, restart_delay)
            print(f"⚠️ تعطل البوت رقم {index + 1} (محاولة {attempt}): {exc}. إعادة المحاولة بعد {wait_time}ث.")
            await asyncio.sleep(wait_time)
            continue
        else:
            await bot.close()
            break


async def run_all_bots(tokens: list[str], start_delay: float, restart_delay: float):
    tasks: list[asyncio.Task] = []
    for idx, token in enumerate(tokens):
        task = asyncio.create_task(launch_bot(token, idx, restart_delay), name=f"bot-{idx + 1}")
        tasks.append(task)
        if start_delay > 0 and idx < len(tokens) - 1:
            await asyncio.sleep(start_delay)
    await asyncio.gather(*tasks)


def main():
    if not TOKENS:
        print("❔ ما في توكنات متوفرة لتشغيل البوت")
        return
    try:
        start_delay = float(os.getenv("BOT_START_DELAY", "1.0"))
    except ValueError:
        start_delay = 1.0
    try:
        restart_delay = float(os.getenv("BOT_RESTART_DELAY", "5.0"))
    except ValueError:
        restart_delay = 5.0
    try:
        asyncio.run(run_all_bots(TOKENS, start_delay, restart_delay))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
