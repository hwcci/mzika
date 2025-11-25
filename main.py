import asyncio
import json
import os
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from discord.abc import Messageable
import wavelink
from dotenv import load_dotenv

load_dotenv()


# ----------------- إعداد التوكنات -----------------
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
    unique: list[str] = []
    for t in tokens:
        if t not in seen:
            unique.append(t)
            seen.add(t)
    return unique


# ----------------- إعداد الإيموجيات -----------------
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


# ----------------- إعدادات عامة -----------------
TOKENS = load_tokens()
PREFIX = os.getenv("DISCORD_PREFIX", "!")
MAX_MESSAGE_CACHE = max(100, int(os.getenv("DISCORD_MAX_MESSAGES", "200")))

LAVA_HOST = os.getenv("LAVA_HOST", "127.0.0.1")
LAVA_PORT = int(os.getenv("LAVA_PORT", "2333"))
LAVA_PASSWORD = os.getenv("LAVA_PASSWORD", "youshallnotpass")
LAVA_SSL = os.getenv("LAVA_SSL", "false").lower() in {"true", "1", "yes"}


# ----------------- لوحة التحكم -----------------
class ControlView(discord.ui.View):
    def __init__(self, owner: discord.Member, emojis: dict[str, str], cog: "MusicCog", guild: discord.Guild):
        super().__init__(timeout=180)
        self.owner_id = owner.id
        self.emojis = emojis
        self.cog = cog
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
            await interaction.response.send_message("هذا البانل مخصص لك فقط.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="\u200b", style=discord.ButtonStyle.secondary, custom_id="control_play")
    async def play_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cog.resume_track(self.guild, interaction.response)

    @discord.ui.button(label="\u200b", style=discord.ButtonStyle.secondary, custom_id="control_stop")
    async def stop_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cog.stop_track(self.guild, interaction.response)

    @discord.ui.button(label="\u200b", style=discord.ButtonStyle.secondary, custom_id="control_skip")
    async def skip_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cog.skip_track(self.guild, interaction.response)

    @discord.ui.button(label="\u200b", style=discord.ButtonStyle.secondary, custom_id="control_restart")
    async def restart_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cog.restart_track(self.guild, interaction.response)

    @discord.ui.button(label="\u200b", style=discord.ButtonStyle.secondary, custom_id="control_vol_up")
    async def volume_up_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cog.change_volume(self.guild, 10, interaction.response)

    @discord.ui.button(label="\u200b", style=discord.ButtonStyle.secondary, custom_id="control_vol_down")
    async def volume_down_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cog.change_volume(self.guild, -10, interaction.response)


# ----------------- الكوج الرئيسي -----------------
class MusicCog(commands.Cog):
    def __init__(self, bot: commands.Bot, storage_dir: Path):
        self.bot = bot
        self.storage_dir = storage_dir
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.text_channels: dict[int, int] = {}
        self.last_tracks: dict[int, wavelink.Playable | None] = {}
        self.volumes: dict[int, int] = {}
        self.node_ready = asyncio.Event()
        self.bot.loop.create_task(self._connect_node_loop())

    async def _connect_node_loop(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                scheme = "https" if LAVA_SSL else "http"
                node = wavelink.Node(uri=f"{scheme}://{LAVA_HOST}:{LAVA_PORT}", password=LAVA_PASSWORD, secure=LAVA_SSL)
                await wavelink.NodePool.connect(client=self.bot, nodes=[node])
                print(f"✅ Lavalink node ready at {scheme}://{LAVA_HOST}:{LAVA_PORT}")
                self.node_ready.set()
                return
            except Exception as exc:
                print(f"[lavalink] failed to connect, retrying in 5s: {exc}")
                await asyncio.sleep(5)

    def _player(self, guild: discord.Guild | None) -> Optional[wavelink.Player]:
        if not guild:
            return None
        vc = guild.voice_client
        if vc and isinstance(vc, wavelink.Player):
            return vc
        return None

    async def _connect_player(self, ctx: commands.Context, channel: Optional[discord.VoiceChannel] = None) -> Optional[wavelink.Player]:
        if not ctx.guild:
            return None
        await self.node_ready.wait()
        player = self._player(ctx.guild)
        if player:
            return player
        target = channel or (ctx.author.voice.channel if ctx.author.voice else None)
        if not target:
            await ctx.send("⚠️ ادخل قناة صوتية أولاً.")
            return None
        player = await target.connect(cls=wavelink.Player)
        player.queue = wavelink.Queue()
        player.autoplay = wavelink.AutoPlayMode.disabled
        self.volumes[ctx.guild.id] = 100
        await player.set_volume(100)
        return player

    async def _send_panel(self, channel: Messageable, member: discord.Member, guild: discord.Guild, track_title: Optional[str]):
        emojis = get_guild_emojis(guild.id)
        view = ControlView(member, emojis, self, guild)
        text = f"🎶 **{(track_title or 'غير معروف').strip()}**"
        try:
            await channel.send(text, view=view)
        except Exception as exc:
            print(f"[panel] send failed in {guild.name}: {exc}")

    async def _record_text_channel(self, guild: discord.Guild, channel: discord.abc.GuildChannel | discord.Thread):
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            self.text_channels[guild.id] = channel.id
            path = self.storage_dir / "text_channels.json"
            try:
                path.write_text(json.dumps({str(k): v for k, v in self.text_channels.items()}), encoding="utf-8")
            except Exception:
                pass

    @commands.Cog.listener()
    async def on_ready(self):
        print(f"✅ {self.bot.user} جاهز.")
        # تحميل قنوات النص المخزنة
        path = self.storage_dir / "text_channels.json"
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self.text_channels = {int(k): int(v) for k, v in data.items()}
            except Exception:
                self.text_channels = {}

    @commands.Cog.listener()
    async def on_wavelink_track_start(self, player: wavelink.Player, track: wavelink.Playable):
        self.last_tracks[player.guild.id] = track
        channel_id = self.text_channels.get(player.guild.id)
        channel = player.guild.get_channel(channel_id) if channel_id else None
        member = player.guild.me
        if channel and member:
            await self._send_panel(channel, member, player.guild, track.title)

    @commands.Cog.listener()
    async def on_wavelink_track_end(self, player: wavelink.Player, track: wavelink.Playable, reason):
        if not player.queue.is_empty:
            nxt = player.queue.get()
            await player.play(nxt)
        else:
            self.last_tracks[player.guild.id] = track

    async def prepare_track(self, query: str) -> Optional[wavelink.Playable]:
        try:
            node = wavelink.NodePool.get_node()
            results = await wavelink.Playable.search(query, node=node)
        except Exception as exc:
            print(f"[wavelink] search error for '{query}': {exc}")
            return None
        if not results:
            return None
        if isinstance(results, wavelink.Playlist):
            return results.tracks[0] if results.tracks else None
        if isinstance(results, list):
            return results[0]
        return results

    async def add_to_queue(self, ctx: commands.Context, player: wavelink.Player, track: wavelink.Playable):
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

    async def skip_track(self, guild: discord.Guild | None, response: Optional[discord.InteractionResponse] = None) -> Optional[str]:
        if not guild:
            return "⚠️ لا يوجد سيرفر."
        player = self._player(guild)
        if not player or not player.is_playing():
            msg = "⚠️ لا يوجد تشغيل."
            if response:
                await response.send_message(msg, ephemeral=True)
                return None
            return msg
        if not player.queue.is_empty:
            nxt = player.queue.get()
            await player.play(nxt)
        else:
            await player.stop()
        msg = "⏭️ تم التخطي."
        if response:
            await response.send_message(msg, ephemeral=True)
            return None
        return msg

    async def stop_track(self, guild: discord.Guild | None, response: Optional[discord.InteractionResponse] = None) -> Optional[str]:
        if not guild:
            return "⚠️ لا يوجد سيرفر."
        player = self._player(guild)
        if not player:
            msg = "⚠️ غير متصل."
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

    async def restart_track(self, guild: discord.Guild | None, response: Optional[discord.InteractionResponse] = None) -> Optional[str]:
        if not guild:
            return "⚠️ لا يوجد سيرفر."
        player = self._player(guild)
        if not player:
            msg = "⚠️ غير متصل."
            if response:
                await response.send_message(msg, ephemeral=True)
                return None
            return msg
        last = self.last_tracks.get(guild.id)
        if not last:
            msg = "⚠️ لا يوجد مسار سابق."
            if response:
                await response.send_message(msg, ephemeral=True)
                return None
            return msg
        await player.play(last)
        msg = "🔁 تم إعادة التشغيل."
        if response:
            await response.send_message(msg, ephemeral=True)
            return None
        return msg

    async def resume_track(self, guild: discord.Guild | None, response: Optional[discord.InteractionResponse] = None) -> Optional[str]:
        if not guild:
            return "⚠️ لا يوجد سيرفر."
        player = self._player(guild)
        if not player:
            msg = "⚠️ غير متصل."
            if response:
                await response.send_message(msg, ephemeral=True)
                return None
            return msg
        if player.is_paused():
            await player.resume()
            msg = "▶️ تم الاستئناف."
        elif not player.is_playing() and not player.queue.is_empty:
            nxt = player.queue.get()
            await player.play(nxt)
            msg = "▶️ يتم الآن التشغيل."
        else:
            msg = "⚠️ لا يوجد تشغيل."
        if response:
            await response.send_message(msg, ephemeral=True)
            return None
        return msg

    async def change_volume(self, guild: discord.Guild | None, delta: int, response: Optional[discord.InteractionResponse] = None) -> Optional[str]:
        if not guild:
            return "⚠️ لا يوجد سيرفر."
        player = self._player(guild)
        if not player:
            msg = "⚠️ غير متصل."
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

    # ----------------- الأوامر النصية -----------------
    @commands.command(name="join")
    async def join(self, ctx, channel: discord.VoiceChannel | None = None):
        player = await self._connect_player(ctx, channel)
        if player:
            await self._record_text_channel(ctx.guild, ctx.channel)  # type: ignore
            await ctx.send(f"✅ انضممت إلى: {player.channel.name}")

    @commands.command(name="شغل")
    async def play_ar(self, ctx, *, query: str):
        if not ctx.guild:
            return
        player = await self._connect_player(ctx)
        if not player:
            return
        await self._record_text_channel(ctx.guild, ctx.channel)  # type: ignore
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
        msg = await self.resume_track(ctx.guild)  # reuse resume/ pause toggling
        if msg:
            await ctx.send(msg)

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
        player = self._player(ctx.guild) if ctx.guild else None
        if player:
            await player.disconnect()
            await ctx.send("👋 تم قطع الاتصال.")

    @commands.command()
    async def panel(self, ctx):
        if not ctx.guild:
            return
        await self._record_text_channel(ctx.guild, ctx.channel)  # type: ignore
        player = self._player(ctx.guild)
        title = player.current.title if player and player.current else None
        await self._send_panel(ctx.channel, ctx.author, ctx.guild, track_title=title)  # type: ignore

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
        await ctx.send("✅ الإيموجيات تم تحديثها.")

    # ----------------- السلاش -----------------
    @app_commands.command(name="join", description="إدخال البوت قناة صوتية محددة")
    async def slash_join(self, interaction: discord.Interaction, channel: discord.VoiceChannel | None = None):
        if not interaction.guild:
            return
        ctx = await commands.Context.from_interaction(interaction)  # type: ignore
        player = await self._connect_player(ctx, channel)
        if player and interaction.guild:
            await self._record_text_channel(interaction.guild, interaction.channel)  # type: ignore
            await interaction.response.send_message(f"✅ انضممت إلى: {player.channel.name}", ephemeral=True)
        else:
            await interaction.response.send_message("⚠️ تعذر الاتصال بالقناة.", ephemeral=True)

    @app_commands.command(name="panel", description="إظهار لوحة التحكم الخاصة بالبث")
    async def slash_panel(self, interaction: discord.Interaction):
        if not interaction.guild or not interaction.channel:
            return
        await self._record_text_channel(interaction.guild, interaction.channel)
        player = self._player(interaction.guild)
        title = player.current.title if player and player.current else None
        await interaction.response.defer(thinking=True, ephemeral=True)
        await self._send_panel(interaction.channel, interaction.user, interaction.guild, track_title=title)  # type: ignore
        await interaction.followup.send("✅ أرسلت اللوحة.", ephemeral=True)

    @app_commands.command(name="setemojis", description="تغيير إيموجيات البانل")
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


# ----------------- بناء البوت -----------------
def build_bot() -> commands.Bot:
    intents = discord.Intents.default()
    intents.message_content = True
    intents.voice_states = True

    bot = commands.Bot(
        command_prefix=commands.when_mentioned_or(PREFIX),
        intents=intents,
        max_messages=MAX_MESSAGE_CACHE,
    )

    storage_dir = Path("bot_data")

    @bot.event
    async def on_ready():
        print(f"✅ {bot.user} جاهز.")

    async def setup_hook():
        await bot.add_cog(MusicCog(bot, storage_dir))
        await bot.tree.sync()

    bot.setup_hook = setup_hook  # type: ignore
    return bot


# ----------------- تشغيل عدة بوتات -----------------
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
