import asyncio
import discord
from discord.ext import commands
from discord.ext.commands import HelpCommand, Command, Group


def get_category(cmd: Command):
    cog = cmd.cog
    if cog and getattr(cog, "qualified_name", None):
        return cog.qualified_name
    return getattr(cmd, "category", "No Category")


class EmbedHelpCommand(HelpCommand):
    async def send_bot_help(self, mapping):
        cats = {}
        for cog, cmds in mapping.items():
            for cmd in cmds:
                if not await self.filter_commands([cmd]):
                    continue
                cat = get_category(cmd)
                cats.setdefault(cat, []).append(cmd)

        fields = []
        for cat, cmds in sorted(cats.items()):
            lines = []
            for c in sorted(cmds, key=lambda x: x.name):
                short = c.short_doc or c.description or ""
                lines.append(f"`{c.name}` — {short}")
            fields.append((cat, "\n".join(lines) if lines else "No commands"))

        embed = discord.Embed(title="Help", color=0x5865F2)
        for name, value in fields:
            embed.add_field(name=name, value=value, inline=False)

        prefix = None
        if getattr(self, "context", None) and getattr(self.context, "clean_prefix", None):
            prefix = self.context.clean_prefix
        else:
            try:
                get_prefix = getattr(self.get_bot(), "get_prefix", None)
                if callable(get_prefix):
                    maybe = get_prefix(None)
                    prefix = (
                        maybe
                        if isinstance(maybe, str)
                        else (await maybe if asyncio.iscoroutine(maybe) else None)
                    )
            except Exception:
                prefix = None
        if not prefix:
            prefix = ">"

        embed.set_footer(text=f"Type {prefix}help <command> for more info.")
        destination = self.get_destination()
        await destination.send(embed=embed)

    async def send_cog_help(self, cog):
        cmds = await self.filter_commands(cog.get_commands())
        embed = discord.Embed(title=f"{cog.qualified_name} Commands", color=0x5865F2)
        for c in sorted(cmds, key=lambda x: x.name):
            embed.add_field(
                name=self.get_command_signature(c),
                value=c.short_doc or c.description or "No description",
                inline=False,
            )
        await self.get_destination().send(embed=embed)

    async def send_group_help(self, group: Group):
        embed = discord.Embed(title=f"{group.qualified_name} (group)", color=0x5865F2)
        embed.description = group.help or group.short_doc or ""
        for c in group.commands:
            embed.add_field(
                name=self.get_command_signature(c),
                value=c.short_doc or c.description or "No description",
                inline=False,
            )
        await self.get_destination().send(embed=embed)

    async def send_command_help(self, command: Command):
        embed = discord.Embed(title=self.get_command_signature(command), color=0x5865F2)
        desc = command.help or command.short_doc or ""
        embed.description = desc
        if command.aliases:
            embed.add_field(name="Aliases", value=", ".join(command.aliases), inline=False)
        await self.get_destination().send(embed=embed)
