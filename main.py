import re
import json
import time
import traceback
import aiohttp
import asyncio
import tempfile
import os
from pathlib import Path

try:
    import imageio_ffmpeg as _iio_ffmpeg

    _ffmpeg_exe = _iio_ffmpeg.get_ffmpeg_exe()
    _ffmpeg_dir = str(Path(_ffmpeg_exe).parent)
    os.environ["PATH"] = _ffmpeg_dir + os.pathsep + os.environ.get("PATH", "")
    os.environ["IMAGEIO_FFMPEG_EXE"] = _ffmpeg_exe
except Exception:
    pass

try:
    from curl_cffi.requests import AsyncSession as CurlSession

    _CURL_CFFI_AVAILABLE = True
except ImportError:
    _CURL_CFFI_AVAILABLE = False

import discord
from discord.ext import commands
from yt_dlp import YoutubeDL
import ffmpeg
import aiofiles
from keep_alive import keep_alive
from dotenv import load_dotenv

from modules.utils import _log, _short_error, send_file_and_cleanup
from modules.help_cmd import EmbedHelpCommand
from modules.ai_core import explain_error
import modules.utils as _utils_mod
import modules.monitoring as _monitoring_mod
import modules.calculator as _calc_mod

load_dotenv()

INTENTS = discord.Intents.default()
INTENTS.message_content = True
bot = commands.Bot(command_prefix=">", intents=INTENTS)

_COOKIEFILE = os.environ.get("YTDL_COOKIEFILE")

try:
    from yt_dlp.networking.impersonate import ImpersonateTarget
    import curl_cffi  # noqa: F401

    _IMPERSONATE_OPTS = {"impersonate": ImpersonateTarget(client="chrome")}
except ImportError:
    _IMPERSONATE_OPTS = {}

YTDLP_COMMON = {
    "noplaylist": True,
    "quiet": False,
    "no_warnings": False,
    "format": "bestaudio/best",
    "outtmpl": "%(id)s.%(ext)s",
    "default_search": "auto",
    "retries": 3,
    "geo_bypass": True,
    "no_check_certificate": True,
    "socket_timeout": 15,
    "sleep_interval_requests": 0,
    **(
        {"ffmpeg_location": os.environ["IMAGEIO_FFMPEG_EXE"]}
        if "IMAGEIO_FFMPEG_EXE" in os.environ
        else {}
    ),
    "extractor_args": {
        "youtube": {
            "player_client": ["ios", "android", "android_vr", "mweb", "web_creator"],
        },
    },
    **_IMPERSONATE_OPTS,
    "http_headers": {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "*/*",
    },
    **({"cookiefile": _COOKIEFILE} if _COOKIEFILE else {}),
}

_YTDLP_FATAL_PHRASES = (
    "video unavailable",
    "private video",
    "has been removed",
    "copyright",
    "age-restricted",
    "members-only",
    "not available in your country",
    "this live event will begin",
    "premiere will begin",
)

_INVIDIOUS_INSTANCES = [
    "https://invidious.f5.si",
    "https://yewtu.be",
    "https://yt.cdaut.de",
    "https://invidious.privacydev.net",
    "https://inv.nadeko.net",
    "https://invidious.perennialte.ch",
    "https://iv.datura.network",
    "https://invidious.slipfox.xyz",
    "https://invidious.projectsegfau.lt",
    "https://inv.in.projectsegfau.lt",
    "https://inv.bp.projectsegfau.lt",
]

_INV_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}


def _is_ytdlp_fatal(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(phrase in msg for phrase in _YTDLP_FATAL_PHRASES)


def _bar(pct: int, width: int = 12) -> str:
    filled = round(pct / 100 * width)
    return "▰" * filled + "░" * (width - filled)


def _sm(pct: int, *lines: str) -> str:
    return f"`[{_bar(pct)}]` **{pct}%**\n" + "\n".join(lines)


def _platform(url: str) -> str:
    u = url.lower()
    if "instagram.com" in u:
        return "Instagram"
    if "twitter.com" in u or "x.com" in u:
        return "Twitter/X"
    if "youtube.com" in u or "youtu.be" in u:
        return "YouTube"
    if "tiktok.com" in u:
        return "TikTok"
    if "reddit.com" in u:
        return "Reddit"
    if "twitch.tv" in u:
        return "Twitch"
    if "vimeo.com" in u:
        return "Vimeo"
    if "facebook.com" in u or "fb.watch" in u:
        return "Facebook"
    if "soundcloud.com" in u:
        return "SoundCloud"
    if "bilibili.com" in u:
        return "Bilibili"
    if "dailymotion.com" in u:
        return "Dailymotion"
    return "Media"


async def _send_result(
    ctx, msg, file_path: Path, media_type: str, title: str, start: float
) -> bool:
    size_mb = file_path.stat().st_size / (1024 * 1024)
    if size_mb > 25:
        await msg.edit(
            content=_sm(
                0, f"❌  File too large ({size_mb:.1f} MB — Discord limit is 25 MB)"
            )
        )
        return False
    await msg.edit(content=_sm(90, "📤  Uploading..."))
    title_line = f"\n**{title}**" if title else ""
    try:
        await ctx.send(
            f"{ctx.author.mention}, here is your {media_type}:{title_line}",
            file=discord.File(str(file_path)),
        )
    except discord.errors.HTTPException as e:
        await msg.edit(content=_sm(0, f"❌  Upload failed: {e}"))
        return False
    finally:
        file_path.unlink(missing_ok=True)
    total = time.time() - start
    await msg.edit(
        content=_sm(100, f"<a:confetti:1489625600678166729>  Done in {total:.1f}s")
    )
    return True


def run_ffmpeg_extract(input_path: Path, output_path: Path, to_mp3: bool):
    """Convert/remux a downloaded file. Raises with stderr on failure."""
    try:
        if to_mp3:
            out, err = (
                ffmpeg.input(str(input_path))
                .audio.output(str(output_path), acodec="libmp3lame", ab="128k")
                .overwrite_output()
                .run(capture_stdout=True, capture_stderr=True)
            )
        else:
            out, err = (
                ffmpeg.input(str(input_path))
                .output(str(output_path), vcodec="copy", acodec="copy", format="mp4")
                .overwrite_output()
                .run(capture_stdout=True, capture_stderr=True)
            )
    except Exception as e:
        stderr_bytes = getattr(e, "stderr", None)
        if stderr_bytes:
            stderr = stderr_bytes.decode(errors="replace")
        else:
            stderr = str(e)
        _log(f"[ffmpeg] ERROR:\n{stderr}")
        raise Exception(f"ffmpeg failed: {stderr[-400:]}")


def _ytdlp_extract(opts: dict, query: str):
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(query, download=True)
        return info["entries"][0] if "entries" in info else info


def download_sync(query: str, tmpdir: Path, prefer_video: bool = False):
    base_opts = YTDLP_COMMON.copy()
    base_opts["outtmpl"] = str(tmpdir / "%(id)s.%(ext)s")
    if prefer_video:
        base_opts["format"] = "bestvideo[ext=mp4]+bestaudio/best[ext=m4a]/best"
    else:
        base_opts["format"] = "bestaudio/best"

    _log("[yt-dlp] Attempt A — impersonation + multi-client")
    first_error = None
    try:
        return _ytdlp_extract(base_opts, query)
    except Exception as e:
        if _is_ytdlp_fatal(e):
            raise
        first_error = e
        _log(f"[yt-dlp] Attempt A failed: {e}")

    if _IMPERSONATE_OPTS:
        _log("[yt-dlp] Attempt B — no impersonation fallback")
        plain_opts = {k: v for k, v in base_opts.items() if k != "impersonate"}
        try:
            return _ytdlp_extract(plain_opts, query)
        except Exception as e:
            if _is_ytdlp_fatal(e):
                raise
            _log(f"[yt-dlp] Attempt B failed: {e}")
            raise first_error

    raise first_error


def _extract_video_id(query: str) -> str | None:
    m = re.search(
        r"(?:youtube\.com/(?:watch\?v=|shorts/|embed/)|youtu\.be/)([a-zA-Z0-9_-]{11})",
        query,
    )
    return m.group(1) if m else None


async def _resolve_video_id(query: str) -> str:
    vid = _extract_video_id(query)
    if vid:
        return vid
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as sess:
        for inst in _INVIDIOUS_INSTANCES:
            try:
                async with sess.get(
                    f"{inst}/api/v1/search",
                    params={"q": query, "type": "video", "fields": "videoId"},
                ) as r:
                    if r.status == 200:
                        data = await r.json()
                        if data:
                            return data[0]["videoId"]
            except Exception:
                continue
    raise Exception("Could not resolve a YouTube video ID for that query")


def _is_valid_mp4(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            header = f.read(32)
        if len(header) < 8:
            return False
        if header[:5] in (b"<!DOC", b"<html", b"HTTP/", b"<?xml"):
            return False
        box_type = header[4:8]
        if box_type in (b"ftyp", b"moov", b"mdat", b"free", b"skip"):
            return True
        return True
    except OSError:
        return False


def _curl_download_sync(url: str, dest_str: str) -> tuple[str, bytes]:
    from curl_cffi.requests import Session

    with Session(impersonate="chrome") as s:
        r = s.get(url, headers=_INV_HEADERS, allow_redirects=True, stream=True)
        ct = r.headers.get("Content-Type", "")
        if r.status_code != 200:
            return ct, f"HTTP {r.status_code}".encode()
        if "text/html" in ct or "text/plain" in ct:
            return ct, r.content[:200]
        with open(dest_str, "wb") as f:
            for chunk in r.iter_content(chunk_size=256 * 1024):
                if chunk:
                    f.write(chunk)
        return ct, b""


async def _try_download_url(url: str, dest: Path, label: str) -> bool:
    loop = asyncio.get_event_loop()
    dest.unlink(missing_ok=True)

    curl_available = True
    try:
        ct, err = await loop.run_in_executor(
            None, lambda: _curl_download_sync(url, str(dest))
        )
        if err:
            _log(f"[Invidious] {label} → {err.decode(errors='replace')[:160]}")
            dest.unlink(missing_ok=True)
            return False
    except ImportError:
        curl_available = False
    except Exception as ex:
        _log(f"[Invidious] {label} → curl error: {ex}")
        dest.unlink(missing_ok=True)
        return False

    if not curl_available:
        try:
            dl_timeout = aiohttp.ClientTimeout(total=600, connect=15)
            async with aiohttp.ClientSession(timeout=dl_timeout) as sess:
                async with sess.get(
                    url, headers=_INV_HEADERS, allow_redirects=True
                ) as r:
                    if r.status != 200:
                        _log(f"[Invidious] {label} → HTTP {r.status}")
                        return False
                    ct = r.headers.get("Content-Type", "")
                    if "text/html" in ct or "text/plain" in ct:
                        snippet = await r.read()
                        _log(
                            f"[Invidious] {label} → non-video ({ct}): {snippet[:120]!r}"
                        )
                        return False
                    async with aiofiles.open(dest, "wb") as f:
                        async for chunk in r.content.iter_chunked(256 * 1024):
                            await f.write(chunk)
        except Exception as ex:
            _log(f"[Invidious] {label} → aiohttp error: {ex}")
            dest.unlink(missing_ok=True)
            return False

    size = dest.stat().st_size if dest.exists() else 0
    if size < 50_000:
        _log(f"[Invidious] {label} → too small ({size} bytes), likely garbage")
        dest.unlink(missing_ok=True)
        return False
    if not _is_valid_mp4(dest):
        _log(f"[Invidious] {label} → file header invalid (not a real MP4)")
        dest.unlink(missing_ok=True)
        return False
    return True


async def _download_via_invidious(query: str, tmpdir: Path, prefer_video: bool) -> dict:
    _log("[Invidious] Resolving video ID...")
    video_id = await _resolve_video_id(query)
    _log(f"[Invidious] Video ID: {video_id}")

    info = None
    meta_timeout = aiohttp.ClientTimeout(total=15, connect=8)
    async with aiohttp.ClientSession(timeout=meta_timeout) as sess:
        for inst in _INVIDIOUS_INSTANCES:
            _log(f"[Invidious] Trying instance: {inst}")
            try:
                async with sess.get(
                    f"{inst}/api/v1/videos/{video_id}",
                    params={"local": "true"},
                ) as r:
                    if r.status == 200:
                        data = await r.json(content_type=None)
                        if isinstance(data, dict) and data.get("formatStreams"):
                            info = data
                            _log(f"[Invidious] Metadata from {inst}")
                            break
                        _log(f"[Invidious] {inst} → no formatStreams in response")
                    else:
                        _log(f"[Invidious] {inst} → HTTP {r.status}")
            except Exception as ex:
                _log(f"[Invidious] {inst} error: {ex}")

    if not info:
        raise Exception("All Invidious instances failed to return video metadata")

    streams = info.get("formatStreams", [])
    streams_sorted = sorted(
        streams,
        key=lambda s: int(s.get("resolution", "0p").rstrip("p") or 0),
        reverse=prefer_video,
    )

    ext = "mp4"

    for stream in streams_sorted:
        itag = stream.get("itag")
        res = stream.get("resolution", "?")
        if not itag:
            fallback_url = stream.get("url", "")
            if not fallback_url:
                continue
            dest = tmpdir / f"{video_id}.{ext}"
            label = f"{res} (no-itag fallback)"
            _log(f"[Invidious] Trying stream {label}")
            if await _try_download_url(fallback_url, dest, label):
                size = dest.stat().st_size
                _log(f"[Invidious] Download complete: {dest.name} ({size:,} bytes)")
                return {"id": video_id, "ext": ext}
            continue

        for inst in _INVIDIOUS_INSTANCES:
            url = f"{inst}/latest_version?id={video_id}&itag={itag}&local=true"
            dest = tmpdir / f"{video_id}.{ext}"
            label = f"{res} itag={itag} via {inst}"
            _log(f"[Invidious] Trying stream {label}")
            if await _try_download_url(url, dest, label):
                size = dest.stat().st_size
                _log(f"[Invidious] Download complete: {dest.name} ({size:,} bytes)")
                return {"id": video_id, "ext": ext}

    raise Exception("All Invidious streams and instance proxies exhausted")


async def download(
    query: str, tmpdir: Path, prefer_video: bool, on_status=None
) -> dict:
    """
    Download with 2-stage fallback chain:
      1. yt-dlp direct  (impersonation + multi-client, then plain fallback)
      2. Invidious public mirror API
    """
    loop = asyncio.get_event_loop()
    last_error = None
    mode = "mp4" if prefer_video else "mp3"
    _log(f"[download] Starting {mode} download — query: {query!r}")

    async def _status(msg: str):
        _log(f"[download] Status → {msg}")
        if on_status:
            try:
                await on_status(msg)
            except Exception:
                pass

    _log("[Stage 1] yt-dlp direct")
    await _status("`Downloading...`")
    try:
        result = await loop.run_in_executor(
            None, lambda: download_sync(query, tmpdir, prefer_video)
        )
        _log("[Stage 1] SUCCESS")
        return result
    except Exception as e:
        last_error = e
        _log(f"[Stage 1] FAILED: {type(e).__name__}: {e}")
        _log(f"[Stage 1] Traceback:\n{traceback.format_exc()}")
        if _is_ytdlp_fatal(e):
            await _status(f"`Download failed: {_short_error(e)}`")
            raise

    _log("[Stage 2] Invidious public mirror API")
    await _status("`Trying Invidious mirror...`")
    try:
        result = await _download_via_invidious(query, tmpdir, prefer_video)
        _log("[Stage 2] SUCCESS via Invidious")
        return result
    except Exception as e:
        last_error = e
        _log(f"[Stage 2] FAILED: {type(e).__name__}: {e}")
        await _status(f"`All methods failed: {_short_error(e)}`")

    _log(
        f"[download] All stages exhausted. Final error: {type(last_error).__name__}: {last_error}"
    )
    raise last_error


@bot.command(name="ytmp3")
async def ytmp3(ctx, *, query: str):
    """Download a YouTube Video in MP3."""
    _log(f"[ytmp3] Invoked by {ctx.author} in #{ctx.channel} — query: {query!r}")
    start = time.time()
    msg = await ctx.send(_sm(5, "⬇  Downloading YouTube Audio  ·  yt-dlp"))
    tmpdir = Path(tempfile.mkdtemp())
    try:
        info = None
        stage1_err = ""
        try:
            info = await asyncio.get_event_loop().run_in_executor(
                None, lambda: download_sync(query, tmpdir, False)
            )
        except Exception as e:
            if _is_ytdlp_fatal(e):
                await msg.edit(
                    content=_sm(
                        0,
                        f"❌  yt-dlp: {_short_error(e)}",
                        f"   · {time.time() - start:.1f}s",
                    )
                )
                return
            stage1_err = _short_error(e)
            _log(f"[ytmp3] Stage 1 failed: {e}")

        if info is None:
            await msg.edit(
                content=_sm(
                    35,
                    f"⚠  yt-dlp failed: {stage1_err}",
                    "↩  Trying Invidious fallback...",
                )
            )
            try:
                info = await _download_via_invidious(query, tmpdir, False)
            except Exception as e:
                await msg.edit(
                    content=_sm(
                        0,
                        f"⚠  yt-dlp: {stage1_err}",
                        f"❌  Invidious: {_short_error(e)}",
                        f"   · {time.time() - start:.1f}s",
                    )
                )
                return

        elapsed_dl = time.time() - start
        title = info.get("title", "") or ""
        video_id = info["id"]
        downloaded = tmpdir / f"{video_id}.{info.get('ext', 'm4a')}"
        out_path = tmpdir / f"{video_id}.mp3"

        await msg.edit(
            content=_sm(
                65, f"✅  Downloaded  · {elapsed_dl:.1f}s", "🔄  Converting to MP3..."
            )
        )
        await asyncio.get_event_loop().run_in_executor(
            None, lambda: run_ffmpeg_extract(downloaded, out_path, True)
        )
        await _send_result(ctx, msg, out_path, "MP3", title, start)

    except Exception as e:
        _log(f"[ytmp3] ERROR: {e}")
        ai_msg = await asyncio.get_event_loop().run_in_executor(
            None, lambda: explain_error(f"[ytmp3] ERROR: {traceback.format_exc()}")
        )
        await msg.edit(content=_sm(0, f"❌  {ai_msg}"))
    finally:
        try:
            for f in tmpdir.iterdir():
                f.unlink(missing_ok=True)
            tmpdir.rmdir()
        except Exception:
            pass


@bot.command(name="ytmp4")
async def ytmp4(ctx, *, query: str):
    """Download a YouTube Video in MP4."""
    _log(f"[ytmp4] Invoked by {ctx.author} in #{ctx.channel} — query: {query!r}")
    start = time.time()
    msg = await ctx.send(_sm(5, "⬇  Downloading YouTube Video  ·  yt-dlp"))
    tmpdir = Path(tempfile.mkdtemp())
    try:
        info = None
        stage1_err = ""
        try:
            info = await asyncio.get_event_loop().run_in_executor(
                None, lambda: download_sync(query, tmpdir, True)
            )
        except Exception as e:
            if _is_ytdlp_fatal(e):
                await msg.edit(
                    content=_sm(
                        0,
                        f"❌  yt-dlp: {_short_error(e)}",
                        f"   · {time.time() - start:.1f}s",
                    )
                )
                return
            stage1_err = _short_error(e)
            _log(f"[ytmp4] Stage 1 failed: {e}")

        if info is None:
            await msg.edit(
                content=_sm(
                    35,
                    f"⚠  yt-dlp failed: {stage1_err}",
                    "↩  Trying Invidious fallback...",
                )
            )
            try:
                info = await _download_via_invidious(query, tmpdir, True)
            except Exception as e:
                await msg.edit(
                    content=_sm(
                        0,
                        f"⚠  yt-dlp: {stage1_err}",
                        f"❌  Invidious: {_short_error(e)}",
                        f"   · {time.time() - start:.1f}s",
                    )
                )
                return

        elapsed_dl = time.time() - start
        title = info.get("title", "") or ""
        video_id = info["id"]
        downloaded = tmpdir / f"{video_id}.{info.get('ext', 'mp4')}"
        tmp_out = tmpdir / f"{video_id}_tmp.mp4"
        out_path = tmpdir / f"{video_id}.mp4"

        await msg.edit(
            content=_sm(
                65, f"✅  Downloaded  · {elapsed_dl:.1f}s", "🔄  Remuxing to MP4..."
            )
        )
        await asyncio.get_event_loop().run_in_executor(
            None, lambda: run_ffmpeg_extract(downloaded, tmp_out, False)
        )
        tmp_out.rename(out_path)
        await _send_result(ctx, msg, out_path, "MP4", title, start)

    except Exception as e:
        _log(f"[ytmp4] ERROR: {e}")
        ai_msg = await asyncio.get_event_loop().run_in_executor(
            None, lambda: explain_error(f"[ytmp4] ERROR: {traceback.format_exc()}")
        )
        await msg.edit(content=_sm(0, f"❌  {ai_msg}"))
    finally:
        try:
            for f in tmpdir.iterdir():
                f.unlink(missing_ok=True)
            tmpdir.rmdir()
        except Exception:
            pass


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    if _IMPERSONATE_OPTS:
        print("[startup] curl_cffi available — Chrome TLS impersonation ACTIVE")
    else:
        print("[startup] curl_cffi NOT available — impersonation DISABLED")
    from modules.monitoring import load_stats_from_pastebin, health_check_loop

    await load_stats_from_pastebin()
    bot.loop.create_task(health_check_loop(bot))


@bot.event
async def on_command_error(ctx: commands.Context, error):
    if isinstance(error, commands.MissingRequiredArgument):
        cmd = ctx.command
        prefix = ctx.clean_prefix
        embed = discord.Embed(
            title=f"Missing argument: `{error.param.name}`",
            description=(
                f"**Usage:** `{prefix}{cmd.qualified_name} {cmd.signature}`\n\n"
                f"{cmd.help or cmd.short_doc or ''}"
            ),
            color=0x5865F2,
        )
        embed.set_footer(text=f"Type {prefix}help {cmd.qualified_name} for more info.")
        await ctx.send(embed=embed)
    elif isinstance(error, commands.CommandNotFound):
        pass
    else:
        raise error


YTDLP_GENERIC = {
    "noplaylist": True,
    "quiet": False,
    "no_warnings": False,
    "outtmpl": "%(id)s.%(ext)s",
    "retries": 3,
    "geo_bypass": True,
    "no_check_certificate": True,
    "socket_timeout": 15,
    **_IMPERSONATE_OPTS,
    "http_headers": {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "*/*",
    },
    **({"cookiefile": _COOKIEFILE} if _COOKIEFILE else {}),
}


def download_generic_sync(url: str, tmpdir: Path, prefer_video: bool) -> dict:
    opts = YTDLP_GENERIC.copy()
    opts["outtmpl"] = str(tmpdir / "%(id)s.%(ext)s")
    if prefer_video:
        opts["format"] = (
            "bestvideo[ext=mp4]+bestaudio/best[ext=mp4]/bestvideo+bestaudio/best"
        )
        opts["merge_output_format"] = "mp4"
    else:
        opts["format"] = "bestaudio/best"
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return info["entries"][0] if "entries" in info else info


def _is_youtube(url: str) -> bool:
    u = url.lower()
    return "youtube.com" in u or "youtu.be" in u


_COBALT_INSTANCE_CACHE: list[tuple[str, str]] = []
_COBALT_CACHE_TS: float = 0.0
_COBALT_CACHE_TTL: float = 5 * 60

_COBALT_FALLBACK_INSTANCES = [
    ("cobalt-api.meowing.de", "v11"),
    ("cobalt-backend.canine.tools", "v11"),
    ("kityune.imput.net", "v11"),
    ("nachos.imput.net", "v11"),
    ("sunny.imput.net", "v11"),
    ("capi.3kh0.net", "v11"),
    ("blossom.imput.net", "v11"),
    ("downloadapi.stuff.solutions", "v7"),
]


async def _fetch_cobalt_instances() -> list[tuple[str, str]]:
    """Fetch open, online, YouTube-supporting instances from the registry. Cached for 5 min."""
    global _COBALT_INSTANCE_CACHE, _COBALT_CACHE_TS
    now = time.time()
    if _COBALT_INSTANCE_CACHE and (now - _COBALT_CACHE_TS) < _COBALT_CACHE_TTL:
        return _COBALT_INSTANCE_CACHE
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(
                "https://instances.cobalt.best/instances.json",
                headers={"User-Agent": "discord-bot/1.0 (cobalt downloader)"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                if r.status != 200:
                    raise Exception(f"registry HTTP {r.status}")
                data = await r.json(content_type=None)
        usable = [
            i
            for i in data
            if i.get("online")
            and not i.get("info", {}).get("auth")
            and i.get("services", {}).get("youtube")
            and i.get("api")
        ]
        usable.sort(key=lambda x: x.get("score", 0), reverse=True)
        result = []
        for inst in usable:
            host = inst["api"]
            ver_str = inst.get("version", "")
            try:
                major = int(ver_str.split(".")[0])
            except (ValueError, IndexError):
                major = 11
            ver = "v7" if major < 10 else "v11"
            result.append((host, ver))
        if result:
            _COBALT_INSTANCE_CACHE = result
            _COBALT_CACHE_TS = now
            _log(
                f"[cobalt] Registry returned {len(result)} usable instance(s): {[h for h, _ in result[:5]]}"
            )
            return result
    except Exception as e:
        _log(f"[cobalt] Registry fetch failed ({e}), using fallback list")
    return _COBALT_FALLBACK_INSTANCES


async def _cobalt_youtube_download(
    url: str, tmpdir: Path, audio_only: bool, session: aiohttp.ClientSession
) -> tuple[Path, str]:
    """Try each Cobalt instance in order. Returns (file_path, title) or raises."""
    failures: list[str] = []
    instances = await _fetch_cobalt_instances()

    curl_kwargs = {"impersonate": "chrome"} if _CURL_CFFI_AVAILABLE else {}

    for host, api_ver in instances:
        try:
            if api_ver == "v11":
                endpoint = f"https://{host}/"
                headers = {
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                }
                body: dict = {"url": url}
                if audio_only:
                    body["downloadMode"] = "audio"
                    body["audioFormat"] = "mp3"
                    body["audioBitrate"] = "320"
                else:
                    body["downloadMode"] = "auto"
                    body["videoQuality"] = "1080"
                    body["youtubeVideoCodec"] = "h264"
                    body["youtubeVideoContainer"] = "mp4"
            else:
                endpoint = f"https://{host}/api/json"
                headers = {
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                }
                if audio_only:
                    body = {"url": url, "isAudioOnly": True, "aFormat": "mp3"}
                else:
                    body = {"url": url, "vQuality": "720", "isAudioOnly": False}

            if _CURL_CFFI_AVAILABLE:
                async with CurlSession(impersonate="chrome") as cs:
                    api_resp = await cs.post(
                        endpoint, json=body, headers=headers, timeout=20
                    )
                    api_status = api_resp.status_code
                    api_text = api_resp.text
            else:
                async with aiohttp.ClientSession() as tmp_sess:
                    async with tmp_sess.post(
                        endpoint,
                        json=body,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=20),
                    ) as resp:
                        api_status = resp.status
                        api_text = await resp.text()

            if api_status not in (200, 400):
                reason = f"HTTP {api_status}"
                _log(f"[cobalt] {host} — {reason}")
                failures.append(f"{host}: {reason}")
                continue

            try:
                data = json.loads(api_text)
            except Exception:
                reason = f"HTTP {api_status}, non-JSON response"
                _log(f"[cobalt] {host} — {reason}")
                failures.append(f"{host}: {reason}")
                continue

            if api_status == 400 and data.get("status") != "error":
                reason = f"HTTP 400"
                _log(f"[cobalt] {host} — {reason}")
                failures.append(f"{host}: {reason}")
                continue

            status = data.get("status", "")
            if status == "error":
                code = data.get("error", {}).get("code") or data.get("text", "unknown")
                reason = f"api error: {code}"
                _log(f"[cobalt] {host} — {reason}")
                failures.append(f"{host}: {reason}")
                continue
            if status == "local-processing":
                reason = "requires local processing"
                _log(f"[cobalt] {host} — {reason}")
                failures.append(f"{host}: {reason}")
                continue
            if status not in ("tunnel", "redirect", "stream"):
                reason = f"unexpected status {status!r}"
                _log(f"[cobalt] {host} — {reason}")
                failures.append(f"{host}: {reason}")
                continue

            dl_url = data.get("url") or data.get("tunnel")
            if not dl_url:
                reason = "no download URL in response"
                _log(f"[cobalt] {host} — {reason}")
                failures.append(f"{host}: {reason}")
                continue

            cobalt_filename = data.get("filename", "")
            title = Path(cobalt_filename).stem if cobalt_filename else ""

            ext = "mp3" if audio_only else "mp4"
            out_path = tmpdir / f"cobalt_dl.{ext}"

            if _CURL_CFFI_AVAILABLE:
                async with CurlSession(impersonate="chrome") as cs:
                    dl_resp = await cs.get(dl_url, timeout=300)
                    dl_status = dl_resp.status_code
                    if dl_status != 200:
                        reason = f"download HTTP {dl_status}"
                        _log(f"[cobalt] {host} — {reason}")
                        failures.append(f"{host}: {reason}")
                        continue
                    async with aiofiles.open(out_path, "wb") as f:
                        await f.write(dl_resp.content)
            else:
                dl_headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                }
                async with aiohttp.ClientSession() as tmp_sess:
                    async with tmp_sess.get(
                        dl_url,
                        headers=dl_headers,
                        timeout=aiohttp.ClientTimeout(total=300),
                    ) as dl_resp:
                        if dl_resp.status != 200:
                            reason = f"download HTTP {dl_resp.status}"
                            _log(f"[cobalt] {host} — {reason}")
                            failures.append(f"{host}: {reason}")
                            continue
                        async with aiofiles.open(out_path, "wb") as f:
                            async for chunk in dl_resp.content.iter_chunked(1024 * 256):
                                await f.write(chunk)

            if not out_path.exists() or out_path.stat().st_size < 1024:
                reason = "downloaded file empty or too small"
                _log(f"[cobalt] {host} — {reason}")
                failures.append(f"{host}: {reason}")
                out_path.unlink(missing_ok=True)
                continue

            _log(f"[cobalt] {host} — success")
            return out_path, title

        except Exception as e:
            reason = _short_error(e)
            _log(f"[cobalt] {host} — {reason}")
            failures.append(f"{host}: {reason}")
            continue

    summary = " | ".join(failures) if failures else "all instances unavailable"
    raise Exception(f"All Cobalt instances failed — {summary}")


async def _loaderto_download(
    url: str, out_dir: Path, audio_only: bool
) -> tuple[Path, str]:
    """Download via loader.to. Returns (file_path, title) or raises."""
    import re as _re

    hdrs = {"Accept": "application/json", "Referer": "https://loader.to/"}
    formats = ["mp3"] if audio_only else ["1080", "720", "480"]
    ext = "mp3" if audio_only else "mp4"

    job_id = None
    async with CurlSession(impersonate="chrome") as cs:
        for fmt in formats:
            try:
                r = await cs.get(
                    f"https://loader.to/ajax/download.php?format={fmt}&url={url}",
                    headers=hdrs,
                    timeout=15,
                )
                d = r.json()
                if d.get("success") and d.get("id"):
                    job_id = d["id"]
                    break
            except Exception:
                continue

        if not job_id:
            raise RuntimeError("loader.to: failed to start download job")

        _log(f"[loader.to] Job {job_id} started ({'audio' if audio_only else 'video'})")

        dl_url = None
        for _ in range(60):
            await asyncio.sleep(3)
            try:
                r = await cs.get(
                    f"https://loader.to/ajax/progress.php?id={job_id}",
                    headers=hdrs,
                    timeout=15,
                )
                d = r.json()
                if d.get("success") == 1 and d.get("download_url"):
                    dl_url = d["download_url"]
                    break
            except Exception:
                continue

        if not dl_url:
            raise RuntimeError("loader.to: job timed out")

        out_path = out_dir / f"loaderto_{job_id}.{ext}"
        title = "Unknown"

        r_dl = await cs.get(
            dl_url, headers={"Referer": "https://loader.to/"}, timeout=300
        )
        if r_dl.status_code != 200:
            raise RuntimeError(f"loader.to: download HTTP {r_dl.status_code}")

        cd = r_dl.headers.get("content-disposition", "")
        m = _re.search(r'filename="(.+?)"', cd)
        if m:
            title = _re.sub(
                r"\.(mp3|mp4|webm|m4a)$", "", m.group(1), flags=_re.IGNORECASE
            ).strip()

        with open(out_path, "wb") as f:
            f.write(r_dl.content)

        if not out_path.exists() or out_path.stat().st_size < 1024:
            raise RuntimeError("loader.to: downloaded file too small")

        _log(f"[loader.to] Success — {out_path.stat().st_size:,} bytes")
        return out_path, title


@bot.command(name="mp4")
async def mp4(ctx, *, url: str):
    """Download a video (Instagram, Twitter/X, YouTube, etc.) as MP4."""
    plat = _platform(url)
    _log(f"[mp4] Invoked by {ctx.author} in #{ctx.channel} — url: {url!r}")
    start = time.time()
    tmpdir = Path(tempfile.mkdtemp())
    try:
        if _is_youtube(url):
            msg = await ctx.send(_sm(5, f"⬇  Downloading {plat} Video  ·  cobalt"))
            try:
                async with aiohttp.ClientSession() as session:
                    out_path, title = await _cobalt_youtube_download(
                        url, tmpdir, False, session
                    )
                await msg.edit(
                    content=_sm(
                        65,
                        f"✅  Downloaded via cobalt  · {time.time() - start:.1f}s",
                        "📤  Uploading...",
                    )
                )
                await _send_result(ctx, msg, out_path, "MP4", title, start)
                return
            except Exception as cobalt_err:
                _log(f"[mp4] Cobalt failed ({cobalt_err}), trying loader.to...")
                await msg.edit(content=_sm(10, "⚠  Cobalt failed, trying loader.to..."))
            try:
                out_path, title = await _loaderto_download(url, tmpdir, False)
                await msg.edit(
                    content=_sm(
                        65,
                        f"✅  Downloaded via loader.to  · {time.time() - start:.1f}s",
                        "📤  Uploading...",
                    )
                )
                await _send_result(ctx, msg, out_path, "MP4", title, start)
                return
            except Exception as lt_err:
                _log(f"[mp4] loader.to failed ({lt_err}), falling back to yt-dlp")
                await msg.edit(
                    content=_sm(15, "⚠  loader.to failed, retrying with yt-dlp...")
                )
        else:
            msg = await ctx.send(_sm(5, f"⬇  Downloading {plat} Video  ·  yt-dlp"))

        info = await asyncio.get_event_loop().run_in_executor(
            None, lambda: download_generic_sync(url, tmpdir, True)
        )
        elapsed_dl = time.time() - start
        title = info.get("title", "") or ""
        video_id = info.get("id", "video")
        ext = info.get("ext", "mp4")
        src = tmpdir / f"{video_id}.{ext}"
        if not src.exists():
            candidates = list(tmpdir.iterdir())
            src = candidates[0] if candidates else None
        if not src or not src.exists():
            await msg.edit(content=_sm(0, "❌  Download failed: file not found"))
            return

        await msg.edit(
            content=_sm(
                65, f"✅  Downloaded  · {elapsed_dl:.1f}s", "🔄  Remuxing to MP4..."
            )
        )
        tmp_out = tmpdir / f"{video_id}_tmp.mp4"
        out_path = tmpdir / f"{video_id}.mp4"
        await asyncio.get_event_loop().run_in_executor(
            None, lambda: run_ffmpeg_extract(src, tmp_out, False)
        )
        tmp_out.rename(out_path)
        await _send_result(ctx, msg, out_path, "MP4", title, start)

    except Exception as e:
        _log(f"[mp4] ERROR: {e}")
        ai_msg = await asyncio.get_event_loop().run_in_executor(
            None, lambda: explain_error(f"[mp4] ERROR: {traceback.format_exc()}")
        )
        await msg.edit(content=_sm(0, f"❌  {ai_msg}"))
    finally:
        try:
            for f in tmpdir.iterdir():
                f.unlink(missing_ok=True)
            tmpdir.rmdir()
        except Exception:
            pass


@bot.command(name="mp3")
async def mp3(ctx, *, url: str):
    """Download audio from a video (Instagram, Twitter/X, YouTube, etc.) as MP3."""
    plat = _platform(url)
    _log(f"[mp3] Invoked by {ctx.author} in #{ctx.channel} — url: {url!r}")
    start = time.time()
    tmpdir = Path(tempfile.mkdtemp())
    try:
        if _is_youtube(url):
            msg = await ctx.send(_sm(5, f"⬇  Downloading {plat} Audio  ·  cobalt"))
            try:
                async with aiohttp.ClientSession() as session:
                    out_path, title = await _cobalt_youtube_download(
                        url, tmpdir, True, session
                    )
                await msg.edit(
                    content=_sm(
                        65,
                        f"✅  Downloaded via cobalt  · {time.time() - start:.1f}s",
                        "📤  Uploading...",
                    )
                )
                await _send_result(ctx, msg, out_path, "MP3", title, start)
                return
            except Exception as cobalt_err:
                _log(f"[mp3] Cobalt failed ({cobalt_err}), trying loader.to...")
                await msg.edit(content=_sm(10, "⚠  Cobalt failed, trying loader.to..."))
            try:
                out_path, title = await _loaderto_download(url, tmpdir, True)
                await msg.edit(
                    content=_sm(
                        65,
                        f"✅  Downloaded via loader.to  · {time.time() - start:.1f}s",
                        "📤  Uploading...",
                    )
                )
                await _send_result(ctx, msg, out_path, "MP3", title, start)
                return
            except Exception as lt_err:
                _log(f"[mp3] loader.to failed ({lt_err}), falling back to yt-dlp")
                await msg.edit(
                    content=_sm(15, "⚠  loader.to failed, retrying with yt-dlp...")
                )
        else:
            msg = await ctx.send(_sm(5, f"⬇  Downloading {plat} Audio  ·  yt-dlp"))

        info = await asyncio.get_event_loop().run_in_executor(
            None, lambda: download_generic_sync(url, tmpdir, False)
        )
        elapsed_dl = time.time() - start
        title = info.get("title", "") or ""
        video_id = info.get("id", "audio")
        ext = info.get("ext", "m4a")
        src = tmpdir / f"{video_id}.{ext}"
        if not src.exists():
            candidates = list(tmpdir.iterdir())
            src = candidates[0] if candidates else None
        if not src or not src.exists():
            await msg.edit(content=_sm(0, "❌  Download failed: file not found"))
            return

        await msg.edit(
            content=_sm(
                65, f"✅  Downloaded  · {elapsed_dl:.1f}s", "🔄  Converting to MP3..."
            )
        )
        out_path = tmpdir / f"{video_id}.mp3"
        await asyncio.get_event_loop().run_in_executor(
            None, lambda: run_ffmpeg_extract(src, out_path, True)
        )
        await _send_result(ctx, msg, out_path, "MP3", title, start)

    except Exception as e:
        _log(f"[mp3] ERROR: {e}")
        ai_msg = await asyncio.get_event_loop().run_in_executor(
            None, lambda: explain_error(f"[mp3] ERROR: {traceback.format_exc()}")
        )
        await msg.edit(content=_sm(0, f"❌  {ai_msg}"))
    finally:
        try:
            for f in tmpdir.iterdir():
                f.unlink(missing_ok=True)
            tmpdir.rmdir()
        except Exception:
            pass


_utils_mod.setup(bot)
_monitoring_mod.setup(bot)
_calc_mod.setup(bot)

bot.help_command = EmbedHelpCommand()

bot.get_command("ytmp3").category = "YouTube"
bot.get_command("ytmp4").category = "YouTube"
bot.get_command("mp4").category = "Downloader"
bot.get_command("mp3").category = "Downloader"
bot.get_command("stats").category = "Monitoring"
bot.get_command("uptime").category = "Monitoring"
bot.get_command("ping").category = "Utilities"
bot.get_command("help").category = "Utilities"
bot.get_command("calculate").category = "Utilities"


if __name__ == "__main__":
    keep_alive()
    TOKEN = os.environ.get("TOKEN")
    if not TOKEN:
        print("WARNING: TOKEN env var not set. Bot will not connect to Discord.")
        import threading

        threading.Event().wait()
    bot.run(TOKEN)
