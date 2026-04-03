import time
import discord
from discord.ext import commands
from TagScriptEngine import Interpreter, block


_blocks = [block.MathBlock(), block.RandomBlock(), block.RangeBlock()]
_engine = Interpreter(_blocks)


class Calculator(commands.Cog):
    """
    Do math (alias calc)
    """

    __version__ = "1.0.1"

    def __init__(self, bot_instance: commands.Bot):
        self.bot = bot_instance
        blocks_ = [
            block.MathBlock(),
            block.RandomBlock(),
            block.RangeBlock(),
        ]
        self.engine = Interpreter(blocks_)

    @commands.command(aliases=["calc"])
    async def calculate(self, ctx: commands.Context, *, query: str):
        """
        Calculate a math expression.

        Example:
        !calculate 7 / (2 * 2)
        """
        query = query.replace(",", "")
        engine_input = "{m:" + query + "}"
        start = time.monotonic()
        output = self.engine.process(engine_input)
        end = time.monotonic()

        output_string = output.body.replace("{m:", "").replace("}", "")
        try:
            fmt_str = f"{float(output_string):,}"
        except ValueError:
            fmt_str = output_string

        color = getattr(ctx, "embed_color", None)
        if callable(color):
            try:
                embed_color = await ctx.embed_color()
            except Exception:
                embed_color = discord.Color.blurple()
        else:
            embed_color = discord.Color.blurple()

        e = discord.Embed(
            color=embed_color,
            title=f"Input: `{query[:247]}`",
            description=f"Output: `{fmt_str}`",
        )
        e.set_footer(text=f"Calculated in {round((end - start) * 1000, 3)} ms")
        await ctx.send(embed=e)


def setup(bot: commands.Bot):
    @bot.command(name="calculate", aliases=["calc"])
    async def calculate(ctx: commands.Context, *, query: str):
        """Calculate a math expression. Example: >calculate 7 / (2 * 2)"""
        query = query.replace(",", "")
        engine_input = "{m:" + query + "}"
        start = time.monotonic()
        output = _engine.process(engine_input)
        end = time.monotonic()

        output_string = output.body.replace("{m:", "").replace("}", "")
        try:
            fmt_str = f"{float(output_string):,}"
        except Exception:
            fmt_str = output_string

        embed_color = discord.Color.blurple()
        e = discord.Embed(
            color=embed_color,
            title=f"Input: `{query[:247]}`",
            description=f"Output: `{fmt_str}`",
        )
        e.set_footer(text=f"Calculated in {round((end - start) * 1000, 3)} ms")
        await ctx.send(embed=e)
