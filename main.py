import os
import asyncio
import json
import time
from pathlib import Path
from typing import List, Optional

import discord
from discord import app_commands
from discord.ext import commands
from discord.abc import Messageable
import yt_dlp
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
    unique_tokens = []
    for tok in tokens:
        if tok not in seen:
            unique_tokens.append(tok)
            seen.add(tok)
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

CACHE_DIR = Path(os.getenv("AUDIO_CACHE_DIR", "bot_data/cache"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_MAX_BYTES = int(float(os.getenv("CACHE_MAX_BYTES", str(2 * 1024 ** 3))))
CACHE_MAX_FILES = int(os.getenv("CACHE_MAX_FILES", "500"))
MAX_CONCURRENT_DOWNLOADS = max(1, int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "3")))
RAW_COOKIES_PATH = os.getenv("YTDLP_COOKIES", "").strip()
COOKIES_FILE: Optional[str] = None
if RAW_COOKIES_PATH:
    candidate = Path(RAW_COOKIES_PATH).expanduser()
    if candidate.exists():
        COOKIES_FILE = str(candidate)
    else:
        print(f"[ytdl] warning: cookies file not found at {candidate}")

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


ytdl_format_options = {
    "format": "bestaudio/best",
    "outtmpl": str(CACHE_DIR / "%(extractor)s-%(id)s.%(ext)s"),
    "restrictfilenames": True,
    "cachedir": False,  # لا يكتب ملفات كاش لتقليل I/O
    "noplaylist": True,
    "concurrent_fragment_downloads": 1,  # حمل جزء واحد لتقليل استهلاك الـ CPU/الشبكة
    "http_chunk_size": 1048576,
    "fragment_retries": 10,
    "nocheckcertificate": True,
    "ignoreerrors": False,
    "logtostderr": False,
    "quiet": True,
    "no_warnings": True,
    "default_search": "auto",
    "cookiefile": COOKIES_FILE,
}
REMOTE_BEFORE_OPTIONS = "-reconnect 1 -reconnect_at_eof 1 -reconnect_streamed 1 -reconnect_delay_max 5 -reconnect_on_network_error 1 -reconnect_on_http_error 4xx,5xx -rw_timeout 10000000 -probesize 32768 -analyzeduration 0"
BASE_FFMPEG_OPTIONS = "-vn -af aresample=async=1:min_hard_comp=0.100000:first_pts=0 -bufsize 65536"


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
        self.guild_channels: dict[int, int] = {}
        self.volumes: dict[int, float] = {}
        self.text_channels: dict[int, int] = {}
        self.fade_tasks: dict[int, asyncio.Task] = {}
        self.reconnect_task: Optional[asyncio.Task] = None
        self.reconnect_interval = VOICE_RECONNECT_INTERVAL
        self.track_retry_limit = TRACK_RETRY_LIMIT
        self.manual_disconnects: dict[int, float] = {}
        self.rejoin_tasks: dict[int, asyncio.Task] = {}
        self.emoji_file = storage_dir / "emoji_overrides_global.json"
        self.cache_dir = CACHE_DIR
        self.ytdl = yt_dlp.YoutubeDL(ytdl_format_options)
        self.ytdl_downloader = yt_dlp.YoutubeDL(ytdl_format_options)
        self.ytdl_lock = asyncio.Lock()
        self.download_lock = asyncio.Lock()
        self.download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
        self._load_emoji_overrides()

    def _ffmpeg_kwargs(self, source: str) -> dict[str, str]:
        path = Path(source)
        if path.exists():
            return {"executable": FFMPEG_BIN, "options": BASE_FFMPEG_OPTIONS}
        is_remote = source.lower().startswith(("http://", "https://"))
        if is_remote:
            return {
                "executable": FFMPEG_BIN,
                "before_options": REMOTE_BEFORE_OPTIONS,
                "options": BASE_FFMPEG_OPTIONS,
            }
        return {"executable": FFMPEG_BIN, "options": BASE_FFMPEG_OPTIONS}

    def _storage_file(self) -> Optional[Path]:
        if not self.bot.user:
            return None
        return self.storage_dir / f"guild_channels_{self.bot.user.id}.json"

    def _load_channels(self):
        path = self._storage_file()
        if not path or not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.guild_channels = {int(k): int(v) for k, v in data.items()}
        except Exception:
            self.guild_channels = {}

    def _persist_channels(self):
        path = self._storage_file()
        if not path:
            return
        try:
            path.write_text(
                json.dumps({str(k): v for k, v in self.guild_channels.items()}),
                encoding="utf-8",
            )
        except Exception:
            pass

    async def _auto_reconnect(self):
        for guild_id, channel_id in list(self.guild_channels.items()):
            guild = self.bot.get_guild(guild_id)
            if not guild:
                continue
            if self._manual_disconnect_active(guild_id):
                continue
            channel = guild.get_channel(channel_id)
            if not isinstance(channel, discord.VoiceChannel):
                continue
            vc = guild.voice_client
            if vc and vc.channel and vc.channel.id != channel.id:
                try:
                    await vc.move_to(channel)
                except Exception:
                    continue
            if not vc or not vc.is_connected():
                try:
                    await channel.connect()
                except Exception:
                    continue
                vc = guild.voice_client
            if not vc:
                continue
            if guild.id in self.current_track and not vc.is_playing() and not vc.is_paused():
                track = self.current_track.pop(guild.id, None)
                if not track:
                    continue
                track.pop("_panel_sent", None)
                queue = self.queues.setdefault(guild.id, [])
                queue.insert(0, track)
                await self._start_next_track(guild)

    @commands.Cog.listener()
    async def on_ready(self):
        self._load_channels()
        self._load_text_channels()
        if not self.reconnect_task or self.reconnect_task.done():
            self.reconnect_task = asyncio.create_task(self._reconnect_loop())

    def cog_unload(self):
        if self.reconnect_task:
            self.reconnect_task.cancel()
        for task in self.rejoin_tasks.values():
            task.cancel()
        self.rejoin_tasks.clear()

    async def _reconnect_loop(self):
        await self.bot.wait_until_ready()
        try:
            while not self.bot.is_closed():
                await self._auto_reconnect()
                await asyncio.sleep(self.reconnect_interval)
        except asyncio.CancelledError:
            pass

    def _record_channel(self, guild: discord.Guild | None, channel: discord.VoiceChannel | None):
        if guild and channel:
            self.guild_channels[guild.id] = channel.id
            self._persist_channels()

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
            path.write_text(
                json.dumps({str(k): v for k, v in self.text_channels.items()}),
                encoding="utf-8",
            )
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        if not self.bot.user or member.id != self.bot.user.id:
            return
        target = after.channel or before.channel
        if target:
            self._record_channel(member.guild, target)
        guild = member.guild
        if after.channel:
            self._cancel_rejoin_task(guild.id)
        if before.channel and after.channel is None:
            if self._manual_disconnect_active(guild.id):
                return
            self._schedule_voice_return(guild)

    def _load_emoji_overrides(self):
        if not self.emoji_file.exists():
            return
        try:
            data = json.loads(self.emoji_file.read_text(encoding="utf-8"))
            for guild_id, overrides in data.items():
                GLOBAL_EMOJI_OVERRIDES[int(guild_id)] = overrides
        except Exception:
            pass

    def _persist_emoji_overrides(self):
        try:
            self.emoji_file.write_text(
                json.dumps({str(k): v for k, v in GLOBAL_EMOJI_OVERRIDES.items()}),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _get_emojis(self, guild: Optional[discord.Guild]) -> dict[str, str]:
        return get_guild_emojis(guild.id if guild else None)

    def _record_text_channel(
        self, guild: discord.Guild | None, channel: Optional[discord.abc.GuildChannel]
    ):
        if not guild or not channel:
            return
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            self.text_channels[guild.id] = channel.id
            self._persist_text_channels()

    def _bot_member(self, guild: discord.Guild | None) -> Optional[discord.Member]:
        if not guild or not self.bot.user:
            return None
        if guild.me:
            return guild.me
        try:
            return guild.get_member(self.bot.user.id)
        except Exception:
            return None

    def _can_send_in_channel(
        self, guild: discord.Guild, channel: discord.abc.GuildChannel | discord.Thread
    ) -> bool:
        member = self._bot_member(guild)
        if not member or not hasattr(channel, "permissions_for"):
            return False
        perms = channel.permissions_for(member)
        can_send = getattr(perms, "send_messages", False)
        if isinstance(channel, discord.Thread):
            can_threads = getattr(perms, "send_messages_in_threads", True)
            return can_send and can_threads
        return can_send

    def _get_panel_channel(self, guild: discord.Guild | None) -> Optional[discord.TextChannel | discord.Thread]:
        if not guild:
            return None
        channel_id = self.text_channels.get(guild.id)
        if channel_id:
            channel = guild.get_channel(channel_id)
            if isinstance(channel, (discord.TextChannel, discord.Thread)) and self._can_send_in_channel(guild, channel):
                return channel
        blocked_ids = {
            getattr(guild.system_channel, "id", None),
            getattr(guild.rules_channel, "id", None),
        }
        for channel in guild.text_channels:
            if channel.id in blocked_ids:
                continue
            if self._can_send_in_channel(guild, channel):
                return channel
        for channel in guild.text_channels:
            if self._can_send_in_channel(guild, channel):
                return channel
        return None

    async def _ensure_voice(self, guild: discord.Guild) -> Optional[discord.VoiceClient]:
        if not guild:
            return None
        if guild.voice_client:
            return guild.voice_client
        if self._manual_disconnect_active(guild.id):
            return None
        channel_id = self.guild_channels.get(guild.id)
        if channel_id:
            channel = guild.get_channel(channel_id)
            if isinstance(channel, discord.VoiceChannel):
                try:
                    vc = await channel.connect()
                    self._cancel_rejoin_task(guild.id)
                    return vc
                except Exception:
                    pass
        return None

    def _cancel_fade_task(self, guild_id: int):
        task = self.fade_tasks.pop(guild_id, None)
        if task:
            task.cancel()

    def _mark_manual_disconnect(self, guild_id: int):
        self.manual_disconnects[guild_id] = time.time()

    def _manual_disconnect_active(self, guild_id: int) -> bool:
        ts = self.manual_disconnects.get(guild_id)
        if not ts:
            return False
        if time.time() - ts <= MANUAL_LEAVE_GRACE:
            return True
        self.manual_disconnects.pop(guild_id, None)
        return False

    def _cancel_rejoin_task(self, guild_id: int):
        task = self.rejoin_tasks.pop(guild_id, None)
        if task:
            task.cancel()

    def _schedule_voice_return(self, guild: discord.Guild):
        if self._manual_disconnect_active(guild.id):
            return
        task = self.rejoin_tasks.get(guild.id)
        if task and not task.done():
            return
        self.rejoin_tasks[guild.id] = asyncio.create_task(self._voice_return_worker(guild.id))

    async def _voice_return_worker(self, guild_id: int):
        await asyncio.sleep(2.0)
        try:
            guild = self.bot.get_guild(guild_id)
            if not guild or self._manual_disconnect_active(guild_id):
                return
            channel = self._get_stored_channel(guild)
            if not channel:
                return
            vc = guild.voice_client
            if vc and vc.channel and vc.channel.id == channel.id and vc.is_connected():
                return
            try:
                if vc:
                    await vc.move_to(channel)
                else:
                    await channel.connect()
            except Exception as exc:
                print(f"[voice] rejoin failed in {guild.name}: {exc}")
        finally:
            self.rejoin_tasks.pop(guild_id, None)

    def _cleanup_cache_sync(self):
        files = []
        try:
            for path in self.cache_dir.glob("*"):
                if path.is_file():
                    stat = path.stat()
                    files.append((stat.st_mtime, stat.st_size, path))
        except Exception:
            return
        files.sort(reverse=True)
        total_bytes = sum(size for _, size, _ in files)
        total_files = len(files)
        max_bytes = CACHE_MAX_BYTES if CACHE_MAX_BYTES > 0 else None
        max_files = CACHE_MAX_FILES if CACHE_MAX_FILES > 0 else None
        if (not max_bytes or total_bytes <= max_bytes) and (not max_files or total_files <= max_files):
            return
        for _, size, path in reversed(files):
            if (not max_bytes or total_bytes <= max_bytes) and (not max_files or total_files <= max_files):
                break
            try:
                path.unlink()
            except OSError:
                pass
            else:
                total_bytes -= size
                total_files -= 1

    async def _enforce_cache_limits(self):
        if CACHE_MAX_BYTES <= 0 and CACHE_MAX_FILES <= 0:
            return
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._cleanup_cache_sync)

    async def _fetch_cache(self, track: dict[str, str]) -> Optional[str]:
        cache_path = track.get("cache_path")
        if not cache_path:
            return None
        dest = Path(cache_path)
        if dest.exists():
            return str(dest)
        source = track.get("webpage_url") or track.get("original_query")
        if not source:
            return None
        async with self.download_semaphore:
            async with self.download_lock:
                try:
                    await self.bot.loop.run_in_executor(
                        None, lambda: self.ytdl_downloader.download([source])
                    )
                except Exception as exc:
                    print(f"[cache] download failed for {track.get('title', 'unknown')}: {exc}")
                    return None
        if dest.exists():
            await self._enforce_cache_limits()
            return str(dest)
        return None

    async def _get_local_source(self, track: dict[str, str]) -> Optional[str]:
        cache_path = track.get("cache_path")
        if cache_path and Path(cache_path).exists():
            return cache_path
        task = track.get("_prefetch_task")
        if isinstance(task, asyncio.Task):
            try:
                await task
            except Exception as exc:
                print(f"[cache] prefetch error: {exc}")
            finally:
                track.pop("_prefetch_task", None)
        cache_path = track.get("cache_path")
        if cache_path and Path(cache_path).exists():
            return cache_path
        return await self._fetch_cache(track)

    def _kickoff_prefetch(self, track: dict[str, str]):
        if track.get("_prefetch_task"):
            return
        cache_path = track.get("cache_path")
        if not cache_path:
            return
        if Path(cache_path).exists():
            return
        track["_prefetch_task"] = self.bot.loop.create_task(self._fetch_cache(track))

    async def _fade_in_source(self, guild_id: int, source: discord.PCMVolumeTransformer, target: float):
        task = asyncio.current_task()
        steps = 10
        duration = 1.0
        try:
            for idx in range(1, steps + 1):
                await asyncio.sleep(duration / steps)
                source.volume = target * (idx / steps)
        except asyncio.CancelledError:
            source.volume = target
            raise
        finally:
            source.volume = target
            if task and self.fade_tasks.get(guild_id) is task:
                self.fade_tasks.pop(guild_id, None)

    async def _handle_track_end(self, guild: discord.Guild, track: dict[str, str], error: Optional[Exception]):
        if error:
            print(f"[voice] playback error in {guild.name}: {error}")
            recovered = await self._retry_track_playback(guild, track, error)
            if recovered:
                return
        await self._start_next_track(guild)

    async def _retry_track_playback(
        self,
        guild: discord.Guild,
        track: dict[str, str],
        error: Optional[Exception] = None,
    ) -> bool:
        if self.track_retry_limit <= 0:
            return False
        attempts = track.get("_retry_count", 0)
        if attempts >= self.track_retry_limit:
            print(f"[voice] retry limit reached in {guild.name} ({attempts} tries).")
            return False
        refreshed = await self._refresh_track_source(track)
        if not refreshed:
            return False
        track["_retry_count"] = attempts + 1
        track["_panel_sent"] = True
        title = track.get("title", "unknown")
        print(f"[voice] retrying '{title}' in {guild.name} (attempt {track['_retry_count']}/{self.track_retry_limit}).")
        queue = self.queues.setdefault(guild.id, [])
        queue.insert(0, track)
        self.current_track.pop(guild.id, None)
        await self._start_next_track(guild)
        return True

    async def _refresh_track_source(self, track: dict[str, str]) -> bool:
        query = track.get("webpage_url") or track.get("original_query")
        if not query:
            return False
        fresh = await self.prepare_track(query)
        if not fresh or "url" not in fresh:
            return False
        track["url"] = fresh["url"]
        if fresh.get("title"):
            track["title"] = fresh["title"]
        if fresh.get("webpage_url"):
            track["webpage_url"] = fresh["webpage_url"]
        track.setdefault("original_query", query)
        return True

    async def _require_assigned_channel(self, ctx: commands.Context) -> bool:
        if not ctx.guild:
            return False
        stored = self._get_stored_channel(ctx.guild)
        if not stored:
            return False
        if not ctx.author.voice or ctx.author.voice.channel.id != stored.id:
            return False
        return True

    async def _start_next_track(self, guild: discord.Guild):
        previous = self.current_track.get(guild.id)
        if previous:
            self.last_tracks[guild.id] = previous.copy()
        queue = self.queues.get(guild.id, [])
        if not queue:
            self.current_track.pop(guild.id, None)
            return
        track = queue.pop(0)
        vc = guild.voice_client or await self._ensure_voice(guild)
        if not vc:
            queue.insert(0, track)
            self.current_track.pop(guild.id, None)
            return
        self.current_track[guild.id] = track
        target_volume = self.volumes.setdefault(guild.id, 1.0)
        local_source = await self._get_local_source(track)
        source_input = local_source or track["url"]
        ffmpeg_kwargs = self._ffmpeg_kwargs(source_input)
        try:
            raw = discord.FFmpegPCMAudio(source_input, **ffmpeg_kwargs)
        except Exception as exc:
            print(f"[voice] failed to prepare audio in {guild.name}: {exc}")
            handled = await self._retry_track_playback(guild, track, exc)
            if handled:
                return
            queue.insert(0, track)
            self.current_track.pop(guild.id, None)
            await self._start_next_track(guild)
            return
        source = discord.PCMVolumeTransformer(raw, volume=0.0)

        def after(exc):
            asyncio.run_coroutine_threadsafe(self._handle_track_end(guild, track, exc), self.bot.loop)

        try:
            vc.play(source, after=after)
        except Exception as exc:
            print(f"[voice] failed to start playback in {guild.name}: {exc}")
            handled = await self._retry_track_playback(guild, track, exc)
            if handled:
                return
            queue.insert(0, track)
            self.current_track.pop(guild.id, None)
            await self._start_next_track(guild)
            return

        self._cancel_fade_task(guild.id)
        self.fade_tasks[guild.id] = asyncio.create_task(self._fade_in_source(guild.id, source, target_volume))
        if track.pop("_panel_sent", False):
            return
        await self._send_track_panel(guild, track)

    async def _send_track_panel(self, guild: discord.Guild, track: dict[str, str]):
        channel: Optional[Messageable] = None
        member: Optional[discord.Member] = None
        requester_id = track.get("requester_id")
        if requester_id:
            try:
                member = guild.get_member(int(requester_id))
            except (ValueError, TypeError):
                member = None
        text_channel_id = track.get("text_channel_id")
        candidate = None
        if text_channel_id:
            try:
                candidate = guild.get_channel(int(text_channel_id))
            except (ValueError, TypeError):
                candidate = None
        if not isinstance(candidate, (discord.TextChannel, discord.Thread)):
            panel_channel_id = self.text_channels.get(guild.id)
            if panel_channel_id:
                maybe = guild.get_channel(panel_channel_id)
                if isinstance(maybe, (discord.TextChannel, discord.Thread)):
                    candidate = maybe
            if not isinstance(candidate, (discord.TextChannel, discord.Thread)):
                candidate = self._get_panel_channel(guild)
        if isinstance(candidate, (discord.TextChannel, discord.Thread)):
            channel = candidate
        if not channel:
            return
        if not member:
            member = guild.me
        if not member:
            return
        await self.send_panel(channel, member, guild, track_title=track.get("title"))

    async def _manual_join(
        self,
        interaction: discord.Interaction,
        guild: discord.Guild,
        channel: Optional[discord.VoiceChannel] = None,
    ):
        stored = self._get_stored_channel(guild)
        target = (
            channel
            or (interaction.user.voice.channel if interaction.user and interaction.user.voice else None)
            or stored
        )
        if not target:
            await interaction.response.send_message("⚠️ ادخل روم صوتي أو استخدم !join مع ذكر القناة.", ephemeral=True)
            return
        vc = guild.voice_client
        try:
            if vc:
                await vc.move_to(target)
            else:
                await target.connect()
            self._record_channel(guild, target)
            await interaction.response.send_message(f"✅ دخلت القناة {target.name}", ephemeral=True)
        except Exception as exc:
            await interaction.response.send_message(f"⚠️ فشل الدخول: {exc}", ephemeral=True)

    async def add_to_queue(self, guild: discord.Guild, track: dict[str, str]):
        queue = self.queues.setdefault(guild.id, [])
        queue.append(track)
        self.queues[guild.id] = queue
        self._kickoff_prefetch(track)
        if guild.id not in self.current_track:
            await self._start_next_track(guild)

    async def skip_track(self, guild: discord.Guild | None, response: Optional[discord.InteractionResponse] = None) -> Optional[str]:
        if not guild or not guild.voice_client:
            msg = "لا يوجد تشغيل حالي."
            if response:
                await response.send_message(msg, ephemeral=True)
            return None
        guild.voice_client.stop()
        msg = "⏭️ تم التخطي."
        if response:
            await response.send_message(msg, ephemeral=True)
            return None
        return msg

    async def stop_track(self, guild: discord.Guild | None, response: Optional[discord.InteractionResponse] = None) -> Optional[str]:
        if not guild or not guild.voice_client:
            msg = "لا يوجد تشغيل حالي."
            if response:
                await response.send_message(msg, ephemeral=True)
            return None
        guild.voice_client.stop()
        self.current_track.pop(guild.id, None)
        msg = "⏹️ تم الإيقاف."
        if response:
            await response.send_message(msg, ephemeral=True)
            return None
        return msg

    async def restart_track(self, guild: discord.Guild | None, response: Optional[discord.InteractionResponse] = None) -> Optional[str]:
        if not guild:
            return None
        track = self.current_track.get(guild.id) or self.last_tracks.get(guild.id)
        if not track:
            msg = "لا يوجد مسار لإعادة تشغيله."
            if response:
                await response.send_message(msg, ephemeral=True)
            return None
        fresh = track.copy()
        fresh.pop("_panel_sent", None)
        queue = self.queues.setdefault(guild.id, [])
        queue.insert(0, fresh)
        vc = guild.voice_client
        if not vc or not vc.is_playing():
            await self._start_next_track(guild)
        msg = "🔁 سيتم تشغيل المسار مرة أخرى."
        if response:
            await response.send_message(msg, ephemeral=True)
            return None
        return msg

    async def resume_track(self, guild: discord.Guild | None, response: Optional[discord.InteractionResponse] = None) -> Optional[str]:
        if not guild or not guild.voice_client:
            msg = "غير متصل في قناة."
            if response:
                await response.send_message(msg, ephemeral=True)
            return None
        vc = guild.voice_client
        if vc.is_paused():
            vc.resume()
            msg = "▶️ تم الاستئناف."
        elif not vc.is_playing() and guild.id in self.current_track:
            await self._start_next_track(guild)
            msg = "▶️ يتم الآن التشغيل."
        else:
            msg = "لا يوجد شيء للتشغيل."
        if response:
            await response.send_message(msg, ephemeral=True)
            return None
        return msg

    async def change_volume(
        self,
        guild: discord.Guild | None,
        delta: float,
        response: Optional[discord.InteractionResponse] = None,
    ) -> Optional[str]:
        if not guild:
            return None
        current = self.volumes.get(guild.id, 1.0)
        new = min(max(current + delta, 0.1), 2.0)
        self.volumes[guild.id] = new
        vc = guild.voice_client
        if vc and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = new
        msg = f"🔊 مستوى الصوت الآن {int(new * 100)}%."
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
        target_channel: Optional[Messageable] = channel
        if getattr(channel, "guild", None) is None:
            alt = self._get_panel_channel(target_guild)
            if alt:
                target_channel = alt
            else:
                return
        panel_channel = (
            target_channel
            if isinstance(target_channel, (discord.TextChannel, discord.Thread))
            else None
        )
        self._record_text_channel(target_guild, panel_channel)
        panel_text = "\u200b"
        if track_title:
            panel_text = f"🎶 {self._format_track_title(track_title)}"
        elif target_guild:
            current = self._current_track_title(target_guild)
            if current:
                panel_text = f"🎶 {self._format_track_title(current)}"
        emojis = self._get_emojis(target_guild)
        view = ControlView(member, emojis, self, target_guild)
        try:
            await target_channel.send(panel_text, view=view)
        except Exception as exc:
            print(f"[panel] failed to send in {target_guild.name}: {exc}")

    def _current_channel_name(self, guild: discord.Guild | None) -> Optional[str]:
        if not guild:
            return None
        vc = guild.voice_client
        if vc and vc.channel:
            return vc.channel.name
        channel_id = self.guild_channels.get(guild.id)
        channel = guild.get_channel(channel_id) if channel_id else None
        return channel.name if isinstance(channel, discord.VoiceChannel) else None

    def _current_track_title(self, guild: Optional[discord.Guild]) -> Optional[str]:
        if not guild:
            return None
        track = self.current_track.get(guild.id)
        if not track:
            return None
        return track.get("title")

    def _format_track_title(self, title: Optional[str]) -> str:
        clean = (title or "غير معروف").strip() or "غير معروف"
        return f"**{clean}**"

    async def prepare_track(self, query: str) -> Optional[dict[str, str]]:
        try:
            async with self.ytdl_lock:
                data = await self.bot.loop.run_in_executor(
                    None, lambda: self.ytdl.extract_info(query, download=False)
                )
        except Exception as exc:
            print(f"[ytdl] failed to fetch info for {query}: {exc}")
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
        track_data: dict[str, str] = {
            "title": data.get("title", "??? ?????"),
            "url": url,
            "original_query": query,
        }
        if data.get("id"):
            track_data["track_id"] = data["id"]
        if data.get("ext"):
            track_data["ext"] = data["ext"]
        try:
            track_data["cache_path"] = self.ytdl.prepare_filename(data)
        except Exception:
            if track_data.get("track_id"):
                safe_ext = track_data.get("ext") or "webm"
                track_data["cache_path"] = str(self.cache_dir / f"{track_data['track_id']}.{safe_ext}")
        webpage = data.get("webpage_url") or data.get("original_url")
        if isinstance(webpage, str):
            track_data["webpage_url"] = webpage
        duration = data.get("duration")
        if duration:
            track_data["duration"] = str(duration)
        return track_data

    @commands.command(name="join")
    async def join(self, ctx, channel: discord.VoiceChannel | None = None):
        target = channel or (ctx.author.voice.channel if ctx.author.voice else None) or self._get_stored_channel(ctx.guild)
        if not target:
            await ctx.send("⚠️ حدد قناة صوتية أو استخدم البانل.")
            return
        vc = ctx.guild.voice_client
        try:
            if vc:
                await vc.move_to(target)
            else:
                await target.connect()
            self._record_channel(ctx.guild, target)
            await ctx.send(f"✅ انضممت إلى: {target.name}")
            self._record_text_channel(ctx.guild, ctx.channel)
        except Exception as exc:
            await ctx.send(f"⚠️ تعذر الاتصال: {exc}")

    @commands.command(name="شغل")
    async def play_ar(self, ctx, *, query: str):
        if not await self._require_assigned_channel(ctx):
            return
        self._record_text_channel(ctx.guild, ctx.channel)
        track = await self.prepare_track(query)
        if not track:
            await ctx.send("⚠️ لم أستطع جلب الصوت.")
            return
        if not ctx.guild:
            await ctx.send("⚠️ يجب أن يتم الأمر داخل سيرفر.")
            return
        if ctx.author:
            track["requester_id"] = str(ctx.author.id)
        if isinstance(ctx.channel, (discord.TextChannel, discord.Thread)):
            track["text_channel_id"] = str(ctx.channel.id)
        is_first = ctx.guild.id not in self.current_track
        if is_first:
            track["_panel_sent"] = True
        self.queues.setdefault(ctx.guild.id, [])
        await self.add_to_queue(ctx.guild, track)
        if is_first:
            await ctx.send(f"▶️ يتم الآن تشغيل {self._format_track_title(track.get('title'))}.")
            await self.send_panel(ctx.channel, ctx.author, ctx.guild, track_title=track.get("title"))
        else:
            await ctx.send(f"✅ أضيف {self._format_track_title(track.get('title'))} إلى قائمة الانتظار.")

    @commands.command()
    async def stop(self, ctx):
        if not await self._require_assigned_channel(ctx):
            return
        if not ctx.guild:
            return
        msg = await self.stop_track(ctx.guild)
        if msg:
            await ctx.send(msg)

    @commands.command()
    async def pause(self, ctx):
        if not await self._require_assigned_channel(ctx):
            return
        if not ctx.guild:
            return
        if not ctx.guild.voice_client or not ctx.guild.voice_client.is_playing():
            await ctx.send("⚠️ لا يوجد تشغيل.")
            return
        ctx.guild.voice_client.pause()
        await ctx.send("⏸️ تم الإيقاف.")

    @commands.command()
    async def resume(self, ctx):
        if not await self._require_assigned_channel(ctx):
            return
        if not ctx.guild:
            return
        msg = await self.resume_track(ctx.guild)
        if msg:
            await ctx.send(msg)

    @commands.command()
    async def leave(self, ctx):
        if not await self._require_assigned_channel(ctx):
            return
        if not ctx.guild:
            return
        if ctx.guild.voice_client:
            self._mark_manual_disconnect(ctx.guild.id)
            self._cancel_rejoin_task(ctx.guild.id)
            await ctx.guild.voice_client.disconnect()
            await ctx.send("👋 تم قطع الاتصال.")

    @commands.command()
    async def panel(self, ctx):
        if not await self._require_assigned_channel(ctx):
            return
        if not ctx.guild:
            return
        self._record_text_channel(ctx.guild, ctx.channel)
        await self.send_panel(
            ctx.channel,
            ctx.author,
            ctx.guild,
            track_title=self._current_track_title(ctx.guild),
        )

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
        self._record_text_channel(interaction.guild, interaction.channel)
        await self._manual_join(interaction, interaction.guild, channel)

    @app_commands.command(name="panel", description="إظهار لوحة التحكم الخاصة بالبث")
    async def slash_panel(self, interaction: discord.Interaction):
        if not interaction.guild or not interaction.channel:
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        self._record_text_channel(interaction.guild, interaction.channel)
        try:
            await self.send_panel(
                interaction.channel,
                interaction.user,
                interaction.guild,
                track_title=self._current_track_title(interaction.guild),
            )
            await interaction.followup.send("✅ أرسلت اللوحة.", ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(f"❌ فشل عرض اللوحة: {exc}", ephemeral=True)

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

    def _get_stored_channel(self, guild: Optional[discord.Guild]) -> Optional[discord.VoiceChannel]:
        if not guild:
            return None
        channel_id = self.guild_channels.get(guild.id)
        channel = guild.get_channel(channel_id) if channel_id else None
        return channel if isinstance(channel, discord.VoiceChannel) else None


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
            print(f"\u274c فشل تسجيل الدخول للبوت رقم {index + 1}: {exc}")
            await bot.close()
            return
        except asyncio.CancelledError:
            await bot.close()
            raise
        except Exception as exc:
            attempt += 1
            await bot.close()
            wait_time = max(1.0, restart_delay)
            print(f"\u26a0\ufe0f تعطل البوت رقم {index + 1} (محاولة {attempt}): {exc}. إعادة المحاولة بعد {wait_time}ث.")
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
        print("\u2754 ما في توكنات متوفرة لتشغيل البوت")
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


