import time
import json
import asyncio
import aiohttp
import psutil
import platform
import speedtest
import os
from datetime import datetime, timezone
from collections import deque, defaultdict

import discord
from discord.ext import commands

from modules.utils import _log, _short_error


START_TS = time.time()
HISTORY_SECONDS = 30 * 24 * 3600
SAMPLE_INTERVAL = 5 * 60
CHECK_HISTORY = deque()
PASTEBIN_API_KEY = os.environ.get("PASTEBIN_API_KEY")
PASTEBIN_PASTE_KEY = os.environ.get("PASTEBIN_PASTE_KEY")
LAST_UPLOAD = 0.0
UPLOAD_INTERVAL = 1 * 60


def prune_history():
    cutoff = int(time.time()) - HISTORY_SECONDS
    while CHECK_HISTORY and CHECK_HISTORY[0][0] < cutoff:
        CHECK_HISTORY.popleft()


def compute_summary():
    prune_history()
    total = len(CHECK_HISTORY)
    ups = sum(1 for _, up in CHECK_HISTORY if up)
    downs = total - ups
    uptime_pct = round(ups / total * 100, 2) if total else None
    downtime_pct = round(100 - uptime_pct, 2) if uptime_pct is not None else None

    longest_down = 0
    cur = 0
    for _, up in CHECK_HISTORY:
        if not up:
            cur += 1
            longest_down = max(longest_down, cur)
        else:
            cur = 0
    longest_down_seconds = longest_down * SAMPLE_INTERVAL

    if total:
        last_up = CHECK_HISTORY[-1][1]
        streak = 1
        for ts, up in reversed(list(CHECK_HISTORY)[:-1]):
            if up == last_up:
                streak += 1
            else:
                break
        streak_seconds = streak * SAMPLE_INTERVAL
    else:
        last_up = True
        streak_seconds = 0

    days = defaultdict(lambda: {"total": 0, "up": 0})
    now = int(time.time())
    for ts, up in CHECK_HISTORY:
        day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        days[day]["total"] += 1
        if up:
            days[day]["up"] += 1
    day_list = []
    for i in range(29, -1, -1):
        d = datetime.fromtimestamp(now - i * 86400, tz=timezone.utc).strftime("%Y-%m-%d")
        rec = days.get(d, {"total": 0, "up": 0})
        pct = round(rec["up"] / rec["total"] * 100, 2) if rec["total"] else None
        day_list.append({"day": d, "uptime_pct": pct, "samples": rec["total"]})

    return {
        "generated_at": int(time.time()),
        "since": int(START_TS),
        "samples": total,
        "uptime_percent": uptime_pct,
        "downtime_percent": downtime_pct,
        "longest_downtime_seconds": longest_down_seconds,
        "current_state_up": bool(last_up) if total else None,
        "current_streak_seconds": streak_seconds,
        "daily": day_list,
        "recent": list(CHECK_HISTORY),
    }


def sparkline_for_day_list(day_list):
    bars = "▁▂▃▄▅▆▇█"
    out = []
    for d in day_list:
        pct = d["uptime_pct"]
        if pct is None:
            out.append("·")
        else:
            idx = min(len(bars) - 1, max(0, int(round(pct / 100 * (len(bars) - 1)))))
            out.append(bars[idx])
    return "".join(out)


async def upload_stats_overwrite():
    global PASTEBIN_PASTE_KEY
    if not PASTEBIN_API_KEY:
        return None
    summary = compute_summary()
    payload = {
        "api_dev_key": PASTEBIN_API_KEY,
        "api_option": "paste",
        "api_paste_code": json.dumps(summary, indent=2),
        "api_paste_private": "1",
        "api_paste_expire_date": "1M",
        "api_paste_name": "bot-stats",
    }
    async with aiohttp.ClientSession() as sess:
        try:
            async with sess.post(
                "https://pastebin.com/api/api_post.php", data=payload, timeout=30
            ) as resp:
                text = await resp.text()
                if resp.status == 200 and text.startswith("http"):
                    key = text.rsplit("/", 1)[-1]
                    PASTEBIN_PASTE_KEY = key
                    return text
        except Exception:
            return None
    return None


async def load_stats_from_pastebin():
    """On startup, restore CHECK_HISTORY and START_TS from the last Pastebin save."""
    global CHECK_HISTORY, START_TS
    if not PASTEBIN_PASTE_KEY:
        return
    url = f"https://pastebin.com/raw/{PASTEBIN_PASTE_KEY}"
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15)
        ) as sess:
            async with sess.get(url) as r:
                if r.status != 200:
                    return
                text = await r.text()
        data = json.loads(text)
        history = data.get("recent", [])
        now = int(time.time())
        if history:
            cutoff = now - HISTORY_SECONDS
            CHECK_HISTORY.clear()
            for entry in history:
                ts, up = int(entry[0]), bool(entry[1])
                if ts > cutoff:
                    CHECK_HISTORY.append((ts, up))

            last_ts = CHECK_HISTORY[-1][0] if CHECK_HISTORY else None
            if last_ts is not None:
                gap = now - last_ts
                if gap > SAMPLE_INTERVAL:
                    num_down = int(gap // SAMPLE_INTERVAL)
                    for i in range(1, num_down + 1):
                        sample_ts = last_ts + i * SAMPLE_INTERVAL
                        if sample_ts < now and sample_ts > cutoff:
                            CHECK_HISTORY.append((sample_ts, False))
                    print(f"Injected {num_down} down sample(s) to cover {gap // 60:.0f}m offline gap.")

        saved_since = data.get("since")
        if saved_since and saved_since < START_TS:
            START_TS = saved_since
        print(
            f"Loaded {len(CHECK_HISTORY)} history samples from Pastebin (since {data.get('since')})."
        )
    except Exception as e:
        print(f"Could not load stats from Pastebin: {e}")


async def health_check_loop(bot: commands.Bot, interval_seconds: int = SAMPLE_INTERVAL):
    await bot.wait_until_ready()
    global LAST_UPLOAD
    while not bot.is_closed():
        now = int(time.time())
        is_up = bot.is_ready() and bot.latency is not None and bot.latency < 10
        CHECK_HISTORY.append((now, bool(is_up)))
        prune_history()
        if PASTEBIN_API_KEY and (time.time() - LAST_UPLOAD) >= UPLOAD_INTERVAL:
            await upload_stats_overwrite()
            LAST_UPLOAD = time.time()
        await asyncio.sleep(interval_seconds)


def setup(bot: commands.Bot):
    @bot.command(name="uptime")
    async def uptime(ctx: commands.Context):
        """Show bot uptime, availability percentage, and daily stats for the last 30 days."""
        s = compute_summary()
        now = int(time.time())
        delta = now - s["since"]
        days, rem = divmod(delta, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, seconds = divmod(rem, 60)
        uptime_str = f"{days}d {hours}h {minutes}m {seconds}s"

        uptime_pct = s["uptime_percent"]
        longest_down = s["longest_downtime_seconds"]
        cur_state = (
            "Up"
            if s["current_state_up"]
            else "Down"
            if s["current_state_up"] is not None
            else "Unknown"
        )
        streak = s["current_streak_seconds"]

        spark = sparkline_for_day_list(s["daily"])

        embed = discord.Embed(title="Uptime & Availability", color=0x6EE7B7)
        desc_lines = [
            f"• Uptime: **{uptime_str}**",
            f"• Recent uptime: **{uptime_pct}%**"
            if uptime_pct is not None
            else "• Recent uptime: **N/A**",
            f"• Current state: **{cur_state}** (streak {streak // 3600}h {(streak % 3600) // 60}m)",
            f"• Longest downtime: **{longest_down // 60}m {longest_down % 60}s**",
            "",
            f"Daily (last 30d): {spark}",
            "",
            "**Per-day (last 7 days)**",
        ]
        embed.description = "\n".join(desc_lines)

        last7 = s["daily"][-7:]
        for day in last7:
            pct = f"{day['uptime_pct']}%" if day["uptime_pct"] is not None else "N/A"
            embed.add_field(
                name=day["day"], value=f"{pct}\n{day['samples']}samp", inline=True
            )

        if PASTEBIN_API_KEY:
            async def do_upload_and_edit():
                url = await upload_stats_overwrite()
                if url:
                    try:
                        embed.set_footer(text=f"Stats paste: {url}")
                        await msg.edit(embed=embed)
                    except Exception:
                        pass

            msg = await ctx.send(embed=embed)
            bot.loop.create_task(do_upload_and_edit())
        else:
            await ctx.send(embed=embed)

    @bot.command(name="stats")
    async def stats(ctx: commands.Context):
        """Show CPU, RAM, and internet speed incrementally in an embed."""
        proc = psutil.Process()

        cpu_percent = psutil.cpu_percent(interval=0.5)
        mem = psutil.virtual_memory()
        proc_cpu = proc.cpu_percent(interval=None) / max(psutil.cpu_count(), 1)
        proc_mem_mb = proc.memory_info().rss / (1024 * 1024)

        embed = discord.Embed(title="System & Network Stats", color=0x3B82F6)
        embed.add_field(name="CPU (system)", value=f"{cpu_percent:.1f}%", inline=True)
        embed.add_field(name="CPU (process)", value=f"{proc_cpu:.1f}%", inline=True)
        embed.add_field(
            name="Memory (system)",
            value=f"{mem.percent:.1f}% ({mem.used // (1024**2)} / {mem.total // (1024**2)} MB)",
            inline=False,
        )
        embed.add_field(name="Memory (process)", value=f"{proc_mem_mb:.1f} MB", inline=False)
        embed.add_field(name="Download", value="`Pending...`", inline=True)
        embed.add_field(name="Upload", value="`Pending...`", inline=True)
        embed.add_field(name="Ping", value="`Pending...`", inline=False)
        embed.set_footer(text=f"{platform.node()} • PID {proc.pid}")

        msg = await ctx.send(embed=embed)

        loop = asyncio.get_event_loop()
        footer_base = f"{platform.node()} • PID {proc.pid}"
        speedtest_method = "speedtest-cli"

        st = None
        try:
            def make_st():
                s = speedtest.Speedtest()
                s.get_best_server()
                return s

            st = await loop.run_in_executor(None, make_st)
        except Exception:
            st = None

        if st is not None:
            try:
                down_bps = await loop.run_in_executor(None, st.download)
                embed.set_field_at(4, name="Download", value=f"{down_bps / 1_000_000:.2f} Mbps", inline=True)
                await msg.edit(embed=embed)
            except Exception as e:
                embed.set_field_at(4, name="Download", value=f"Failed: {_short_error(e)}", inline=True)
                await msg.edit(embed=embed)

            try:
                up_bps = await loop.run_in_executor(None, st.upload)
                embed.set_field_at(5, name="Upload", value=f"{up_bps / 1_000_000:.2f} Mbps", inline=True)
                await msg.edit(embed=embed)
            except Exception as e:
                embed.set_field_at(5, name="Upload", value=f"Failed: {_short_error(e)}", inline=True)
                await msg.edit(embed=embed)

            try:
                res = st.results
                ping_ms = getattr(res, "ping", None)
                embed.set_field_at(
                    6,
                    name="Ping",
                    value=f"{ping_ms:.1f} ms" if ping_ms is not None else "Unknown",
                    inline=False,
                )
                try:
                    footer_base += f" • IP {res.client.get('ip')}"
                except Exception:
                    pass
            except Exception as e:
                embed.set_field_at(6, name="Ping", value=f"Failed: {_short_error(e)}", inline=False)

            embed.set_footer(text=footer_base)
            await msg.edit(embed=embed)

        else:
            speedtest_method = "HTTP (Cloudflare)"
            import time as _t

            try:
                t0 = _t.monotonic()
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as sess:
                    async with sess.get("https://speed.cloudflare.com/__down?bytes=1000") as r:
                        await r.read()
                embed.set_field_at(
                    6, name="Ping", value=f"{(_t.monotonic() - t0) * 1000:.1f} ms", inline=False
                )
            except Exception as e:
                embed.set_field_at(6, name="Ping", value=f"Failed: {_short_error(e)}", inline=False)
            await msg.edit(embed=embed)

            try:
                size = 10_000_000
                t0 = _t.monotonic()
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40)) as sess:
                    async with sess.get(f"https://speed.cloudflare.com/__down?bytes={size}") as r:
                        data = await r.read()
                elapsed = _t.monotonic() - t0
                embed.set_field_at(
                    4, name="Download", value=f"{len(data) * 8 / elapsed / 1_000_000:.2f} Mbps", inline=True
                )
            except Exception as e:
                embed.set_field_at(4, name="Download", value=f"Failed: {_short_error(e)}", inline=True)
            await msg.edit(embed=embed)

            try:
                size = 5_000_000
                payload = bytes(size)
                t0 = _t.monotonic()
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40)) as sess:
                    async with sess.post("https://speed.cloudflare.com/__up", data=payload) as r:
                        await r.read()
                elapsed = _t.monotonic() - t0
                embed.set_field_at(
                    5, name="Upload", value=f"{size * 8 / elapsed / 1_000_000:.2f} Mbps", inline=True
                )
            except Exception as e:
                embed.set_field_at(5, name="Upload", value=f"Failed: {_short_error(e)}", inline=True)
            await msg.edit(embed=embed)

            embed.set_footer(text=f"{footer_base} • via {speedtest_method}")
            await msg.edit(embed=embed)
