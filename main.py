import asyncio
import json
import os
from pathlib import Path
from typing import Optional, List

import discord
from discord.ext import commands
from discord import app_commands
from discord.abc import Messageable
import yt_dlp
from dotenv import load_dotenv

load_dotenv()

# ---------------- إعداد التوكنات ----------------
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
    uniq: list[str] = []
    for t in tokens:
        if t not in seen:
            uniq.append(t)
            seen.add(t)
    return uniq


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
BOT_START_DELAY = float(os.getenv("BOT_START_DELAY", "1.0"))
MAX_MESSAGE_CACHE = max(100, int(os.getenv("DISCORD_MAX_MESSAGES", "200")))

# ---------------- إعداد الإيموجيات ----------------
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


# ---------------- yt-dlp & ffmpeg ----------------
ytdl_format_options = {
    "format": "bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "default_search": "auto",
    "source_address": "0.0.0.0",
}
ffmpeg_options = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}
ytdl = yt_dlp.YoutubeDL(ytdl_format_options)


class ControlView(discord.ui.View):
    def __init__(self, owner: discord.Member, emojis: dict[str, str], bot: "MusicBot", guild: discord.Guild):
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
            await interaction.response.send_message("هذا البانل مخصص لك فقط.", ephemeral=True)
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
        await self.bot_ref.change_volume(interaction.guild, 0.1, interaction.response)

    @discord.ui.button(label="\u200b", style=discord.ButtonStyle.secondary, custom_id="control_vol_down")
    async def volume_down_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.bot_ref.change_volume(interaction.guild, -0.1, interaction.response)


class MusicBot(commands.Cog):
    def __init__(self, bot: commands.Bot, storage_dir: Path):
        self.bot = bot
        self.storage_dir = storage_dir
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.queues: dict[int, List[dict[str, str]]] = {}
        self.current_track: dict[int, dict[str, str]] = {}
        self.last_tracks: dict[int, dict[str, str]] = {}
        self.text_channels: dict[int, int] = {}
        self.volumes: dict[int, float] = {}
        self._load_text_channels()

    # ---------- تخزين قناة اللوحة ----------
    def _text_storage_file(self) -> Optional[Path]:
        if not self.bot.user:
            return None
        return self.storage_dir / f"text_channels_{self.bot.user.id}.json"

    def _load_text_channels(self):
        path = self._text_storage_file()
        if not path or not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.text_channels = {int(k): int(v) for k, v in data.items()}
        except Exception:
            self.text_channels = {}

    def _persist_text_channels(self):
        path = self._text_storage_file()
        if not path:
            return
        try:
            path.write_text(json.dumps({str(k): v for k, v in self.text_channels.items()}), encoding="utf-8")
        except Exception:
            pass

    def _record_text_channel(self, guild: discord.Guild | None, channel: Optional[discord.abc.GuildChannel]):
        if guild and isinstance(channel, (discord.TextChannel, discord.Thread)):
            self.text_channels[guild.id] = channel.id
            self._persist_text_channels()

    # ---------- أدوات الصوت ----------
    def _player(self, guild: discord.Guild | None) -> Optional[discord.VoiceClient]:
        return guild.voice_client if guild else None

    async def _ensure_voice(self, ctx: commands.Context, channel: Optional[discord.VoiceChannel] = None) -> Optional[discord.VoiceClient]:
        if not ctx.guild:
            return None
        if ctx.guild.voice_client:
            return ctx.guild.voice_client
        target = channel or (ctx.author.voice.channel if ctx.author.voice else None)
        if not target:
            await ctx.send("⚠️ ادخل قناة صوتية أولاً.")
            return None
        try:
            vc = await target.connect()
            return vc
        except Exception as exc:
            await ctx.send(f"⚠️ تعذر الاتصال: {exc}")
            return None

    async def prepare_track(self, query: str) -> Optional[dict[str, str]]:
        loop = asyncio.get_running_loop()
        try:
            data = await loop.run_in_executor(None, lambda: ytdl.extract_info(query, download=False))
        except Exception as exc:
            print(f"[ytdl] failed for {query}: {exc}")
            return None
        if not data:
            return None
        if "entries" in data:
            entries = data.get("entries") or []
            if not entries:
                return None
            data = entries[0]
        url = data.get("url")
        if not url:
            return None
        track = {
            "title": data.get("title", "غير معروف"),
            "url": url,
            "original_query": query,
        }
        return track

    async def _start_next(self, guild: discord.Guild):
        queue = self.queues.get(guild.id, [])
        if not queue:
            self.current_track.pop(guild.id, None)
            return
        track = queue.pop(0)
        vc = guild.voice_client
        if not vc:
            self.current_track.pop(guild.id, None)
            return
        self.current_track[guild.id] = track
        target_volume = self.volumes.setdefault(guild.id, 1.0)
        try:
            source = discord.PCMVolumeTransformer(
                discord.FFmpegPCMAudio(track["url"], executable=FFMPEG_BIN, **ffmpeg_options),
                volume=target_volume,
            )
        except Exception as exc:
            print(f"[voice] failed to prepare audio in {guild.name}: {exc}")
            self.current_track.pop(guild.id, None)
            return await self._start_next(guild)

        def after(exc):
            asyncio.run_coroutine_threadsafe(self._track_end(guild, track, exc), self.bot.loop)

        vc.play(source, after=after)
        await self._send_panel_auto(guild, track)

    async def _track_end(self, guild: discord.Guild, track: dict[str, str], error: Optional[Exception]):
        if error:
            print(f"[voice] error in {guild.name}: {error}")
        self.last_tracks[guild.id] = track
        await self._start_next(guild)

    # ---------- أوامر داخلية ----------
    async def add_to_queue(self, guild: discord.Guild, track: dict[str, str]):
        queue = self.queues.setdefault(guild.id, [])
        queue.append(track)
        if guild.id not in self.current_track:
            await self._start_next(guild)

    async def skip_track(self, guild: discord.Guild | None, response: Optional[discord.InteractionResponse] = None) -> Optional[str]:
        if not guild or not guild.voice_client:
            msg = "⚠️ لا يوجد تشغيل حالي."
            if response:
                await response.send_message(msg, ephemeral=True)
                return None
            return msg
        guild.voice_client.stop()
        msg = "⏭️ تم التخطي."
        if response:
            await response.send_message(msg, ephemeral=True)
            return None
        return msg

    async def stop_track(self, guild: discord.Guild | None, response: Optional[discord.InteractionResponse] = None) -> Optional[str]:
        if not guild or not guild.voice_client:
            msg = "⚠️ لا يوجد تشغيل."
            if response:
                await response.send_message(msg, ephemeral=True)
                return None
            return msg
        guild.voice_client.stop()
        self.queues[guild.id] = []
        self.current_track.pop(guild.id, None)
        msg = "⏹️ تم الإيقاف."
        if response:
            await response.send_message(msg, ephemeral=True)
            return None
        return msg

    async def restart_track(self, guild: discord.Guild | None, response: Optional[discord.InteractionResponse] = None) -> Optional[str]:
        if not guild:
            return None
        last = self.last_tracks.get(guild.id)
        if not last:
            msg = "⚠️ لا يوجد مسار سابق."
            if response:
                await response.send_message(msg, ephemeral=True)
            return None
        queue = self.queues.setdefault(guild.id, [])
        queue.insert(0, last.copy())
        if guild.id not in self.current_track or not guild.voice_client or not guild.voice_client.is_playing():
            await self._start_next(guild)
        msg = "🔁 سيتم تشغيل المسار مرة أخرى."
        if response:
            await response.send_message(msg, ephemeral=True)
            return None
        return msg

    async def resume_track(self, guild: discord.Guild | None, response: Optional[discord.InteractionResponse] = None) -> Optional[str]:
        if not guild or not guild.voice_client:
            msg = "⚠️ غير متصل."
            if response:
                await response.send_message(msg, ephemeral=True)
            return None
        vc = guild.voice_client
        if vc.is_paused():
            vc.resume()
            msg = "▶️ تم الاستئناف."
        elif not vc.is_playing() and guild.id in self.current_track:
            await self._start_next(guild)
            msg = "▶️ يتم الآن التشغيل."
        else:
            msg = "⚠️ لا يوجد تشغيل."
        if response:
            await response.send_message(msg, ephemeral=True)
            return None
        return msg

    async def change_volume(self, guild: discord.Guild | None, delta: float, response: Optional[discord.InteractionResponse] = None) -> Optional[str]:
        if not guild:
            return None
        new = min(max(self.volumes.get(guild.id, 1.0) + delta, 0.1), 2.0)
        self.volumes[guild.id] = new
        vc = guild.voice_client
        if vc and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = new
        msg = f"🔊 مستوى الصوت الآن {int(new * 100)}%."
        if response:
            await response.send_message(msg, ephemeral=True)
            return None
        return msg

    # ---------- إرسال لوحة ----------
    async def _send_panel_auto(self, guild: discord.Guild, track: dict[str, str]):
        channel_id = self.text_channels.get(guild.id)
        if not channel_id:
            return
        channel = guild.get_channel(channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return
        member = guild.me
        if not member:
            return
        await self.send_panel(channel, member, guild, track_title=track.get("title"))

    async def send_panel(self, channel: Messageable, member: discord.Member, guild: Optional[discord.Guild] = None, track_title: Optional[str] = None):
        target_guild = guild or getattr(channel, "guild", None)
        if not target_guild:
            return
        self._record_text_channel(target_guild, channel)  # type: ignore
        panel_text = f"🎶 **{(track_title or 'غير معروف').strip()}**"
        view = ControlView(member, get_guild_emojis(target_guild.id), self, target_guild)
        try:
            await channel.send(panel_text, view=view)
        except Exception as exc:
            print(f"[panel] failed in {target_guild.name}: {exc}")

    # ---------- الأوامر النصية ----------
    @commands.command(name="join")
    async def join(self, ctx, channel: discord.VoiceChannel | None = None):
        vc = await self._ensure_voice(ctx, channel)
        if vc:
            self._record_text_channel(ctx.guild, ctx.channel)  # type: ignore
            await ctx.send(f"✅ انضممت إلى: {vc.channel.name}")

    @commands.command(name="شغل")
    async def play_ar(self, ctx, *, query: str):
        vc = await self._ensure_voice(ctx)
        if not vc or not ctx.guild:
            return
        self._record_text_channel(ctx.guild, ctx.channel)  # type: ignore
        track = await self.prepare_track(query)
        if not track:
            await ctx.send("⚠️ لم أستطع جلب الصوت.")
            return
        await self.add_to_queue(ctx.guild, track)
        queue = self.queues.get(ctx.guild.id, [])
        if len(queue) == 0 or self.current_track.get(ctx.guild.id) == track:
            await ctx.send(f"▶️ يتم الآن تشغيل **{track['title']}**.")
        else:
            await ctx.send(f"✅ أضيف **{track['title']}** إلى قائمة الانتظار.")

    @commands.command()
    async def stop(self, ctx):
        msg = await self.stop_track(ctx.guild)
        if msg:
            await ctx.send(msg)

    @commands.command()
    async def pause(self, ctx):
        if not ctx.guild or not ctx.guild.voice_client or not ctx.guild.voice_client.is_playing():
            await ctx.send("⚠️ لا يوجد تشغيل.")
            return
        ctx.guild.voice_client.pause()
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
        if ctx.guild and ctx.guild.voice_client:
            await ctx.guild.voice_client.disconnect()
            await ctx.send("👋 تم قطع الاتصال.")

    @commands.command()
    async def panel(self, ctx):
        if not ctx.guild:
            return
        self._record_text_channel(ctx.guild, ctx.channel)  # type: ignore
        title = None
        current = self.current_track.get(ctx.guild.id)
        if current:
            title = current.get("title")
        await self.send_panel(ctx.channel, ctx.author, ctx.guild, track_title=title)  # type: ignore

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

    # ---------- أوامر السلاش ----------
    @app_commands.command(name="join", description="إدخال البوت قناة صوتية محددة")
    async def slash_join(self, interaction: discord.Interaction, channel: discord.VoiceChannel | None = None):
        if not interaction.guild:
            return
        ctx = await commands.Context.from_interaction(interaction)  # type: ignore
        vc = await self._ensure_voice(ctx, channel)
        if vc:
            await self._record_text_channel(interaction.guild, interaction.channel)  # type: ignore
            await interaction.response.send_message(f"✅ انضممت إلى: {vc.channel.name}", ephemeral=True)
        else:
            await interaction.response.send_message("⚠️ تعذر الاتصال بالقناة.", ephemeral=True)

    @app_commands.command(name="panel", description="إظهار لوحة التحكم الخاصة بالبث")
    async def slash_panel(self, interaction: discord.Interaction):
        if not interaction.guild or not interaction.channel:
            return
        await self._record_text_channel(interaction.guild, interaction.channel)
        current = self.current_track.get(interaction.guild.id, {})
        title = current.get("title")
        await interaction.response.defer(thinking=True, ephemeral=True)
        await self.send_panel(interaction.channel, interaction.user, interaction.guild, track_title=title)  # type: ignore
        await interaction.followup.send("✅ أرسلت اللوحة.", ephemeral=True)

    @app_commands.command(name="setemojis", description="تغيير إيموجيات البانال")
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
    intents = discord.Intents.default()
    intents.message_content = True
    intents.voice_states = True
    bot = commands.Bot(command_prefix=commands.when_mentioned_or(PREFIX), intents=intents, max_messages=MAX_MESSAGE_CACHE)
    storage_dir = Path("bot_data")

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
        restart_delay = float(os.getenv("BOT_RESTART_DELAY", "5.0"))
    except ValueError:
        restart_delay = 5.0
    try:
        asyncio.run(run_all_bots(TOKENS, BOT_START_DELAY, restart_delay))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
