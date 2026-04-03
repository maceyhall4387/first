import time
import discord
from pathlib import Path
from discord.ext import commands


def _log(msg: str):
    """Print a timestamped console log line."""
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def _short_error(e: Exception, max_len: int = 120) -> str:
    """Return a concise single-line error message from a yt-dlp or ffmpeg exception."""
    msg = str(e)
    for line in msg.splitlines():
        line = line.strip()
        if not line:
            continue
        line = line.removeprefix("ERROR: ").strip()
        if len(line) > max_len:
            line = line[:max_len] + "…"
        return line
    return msg[:max_len]


_BOT_DETECTION_PHRASES = (
    "sign in to confirm you're not a bot",
    "sign in to confirm",
    "confirm you are not a robot",
    "please sign in",
    "bot detection",
    "not a bot",
)


def _is_bot_error(e: Exception) -> bool:
    msg = str(e).lower()
    return any(phrase in msg for phrase in _BOT_DETECTION_PHRASES)


async def send_file_and_cleanup(ctx, file_path: Path, user_mention: str, desc: str):
    try:
        await ctx.send(f"{user_mention}, {desc}", file=discord.File(str(file_path)))
    except discord.errors.HTTPException as e:
        await ctx.send(f"`Upload failed: {e}`")
    finally:
        if file_path.exists():
            try:
                file_path.unlink()
            except Exception:
                pass


def setup(bot: commands.Bot):
    @bot.command(name="ping")
    async def ping(ctx: commands.Context):
        """Ping pong."""
        latencies = getattr(bot, "latencies", None)
        embed = discord.Embed(title="Pong!", color=0x4AC26B)
        if latencies:
            total = len(latencies)
            for shard_index, lat in enumerate(latencies):
                ms = round(lat * 1000)
                embed.add_field(
                    name=f"Shard {shard_index + 1}/{total}", value=f"{ms} ms", inline=False
                )
        else:
            ms = round(bot.latency * 1000)
            embed.add_field(name="Latency", value=f"{ms} ms", inline=False)

        await ctx.send(embed=embed)
