"""core.py (1/5) — shared DB, bleed-style embeds, config helpers, perm checks."""
import os
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env.txt")

TOKEN = os.environ["DISCORD_BOT_TOKEN"]
PREFIX = os.environ.get("DISCORD_PREFIX", ",")
MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]

mongo = AsyncIOMotorClient(MONGO_URL)
db = mongo[DB_NAME]

import time
BRAND = "canary"
# bleed-style: invisible embed (blends into Discord dark background)
COLOR = 0xB18FE3          # purple theme — for info/help/content embeds
COLOR_OK = 0xB18FE3         # bleed green — success ✓
COLOR_WARN = 0xFAA61A      # bleed amber — warnings ⚠️
COLOR_ERR = 0xB18FE3        # bleed red — hard errors ✘
COLOR_LOG = 0xB18FE3       # purple — modlog entries match the theme
START_TIME = time.time()
command_counter = {"count": 0}

# updated on_ready by bot.py
STATE = {"icon": None, "name": BRAND, "emojis": {}}


def _embed(description=None, *, title=None, color=COLOR, author=None,
           author_icon=None, thumbnail=None, image=None, footer=True, fields=None):
    e = discord.Embed(color=color)
    if title:
        e.title = title
    if description:
        e.description = description
    if author:
        e.set_author(name=author, icon_url=author_icon or STATE["icon"])
    if thumbnail:
        e.set_thumbnail(url=thumbnail)
    if image:
        e.set_image(url=image)
    if fields:
        for name, value, inline in fields:
            e.add_field(name=name, value=value, inline=inline)
    if footer:
        e.set_footer(text=BRAND, icon_url=STATE["icon"])
    return e


# Rich content embeds (invisible bg + footer/author)
def base_embed(description=None, title=None, color=COLOR):
    return _embed(description, title=title, color=color)


def info_embed(title, description=None, **kw):
    return _embed(description, title=title, **kw)


# Minimal one-line response embeds (bleed style: colored left bar, no footer)
def ok_embed(member, text):
    mark = str(STATE["emojis"].get("check") or "✓")
    return discord.Embed(color=COLOR_OK, description=f"{mark} {member.mention}: {text}")


def warn_embed(member, text):
    return discord.Embed(color=COLOR_WARN, description=f"⚠️ {member.mention}: {text}")


def err_embed(member, text):
    mark = str(STATE["emojis"].get("xmark") or "✘")
    return discord.Embed(color=COLOR_ERR, description=f"{mark} {member.mention}: {text}")


def _module_of(cmd):
    return (cmd.cog.qualified_name.lower() if cmd.cog else "general")


HELP_COLOR = 0xB18FE3  # purple theme accent


def build_help_embed(cmd, author, prefix=PREFIX):
    name = cmd.qualified_name
    sig = re.sub(r"=[^\]>]*", "", cmd.signature)
    is_group = isinstance(cmd, commands.Group)
    subs = sorted({c.name for c in cmd.commands}) if is_group else []
    example = f"{prefix}{name}"
    if subs:
        example += f" {subs[0]}"
    else:
        for tok in sig.split():
            raw = tok.strip("<>[]").split("=")[0]
            if "member" in raw or "user" in raw or raw.startswith("@"):
                example += " @user"
            elif "channel" in raw or raw.startswith("#"):
                example += " #general"
            elif "role" in raw:
                example += " @role"
            elif raw == "reason":
                example += " spam"
            elif raw in ("message", "text", "query", "word", "suggestion", "name"):
                example += " example"
            elif "duration" in raw or "time" in raw:
                example += " 10m"
            elif raw:
                example += f" {raw}"
    syntax = f"{prefix}{name} " + (f"({'|'.join(subs)})" if subs else sig.replace("<", "(").replace(">", ")"))
    syntax = syntax.strip()
    e = discord.Embed(color=HELP_COLOR, title=name,
                      description=f">>> {cmd.help or 'No description provided.'}")
    e.set_author(name=author.display_name, icon_url=author.display_avatar.url)
    e.add_field(name="\u200b",
                value=f"```\nSyntax: {syntax}\nExample: {example}\n```",
                inline=False)
    return e


class HelpGotoModal(discord.ui.Modal, title="Go to page"):
    def __init__(self, view):
        super().__init__()
        self.view_ref = view
        self.page = discord.ui.TextInput(label="Page number",
                                         placeholder=f"1-{len(view.commands)}", max_length=3)
        self.add_item(self.page)

    async def on_submit(self, interaction):
        try:
            n = int(self.page.value) - 1
        except ValueError:
            return await interaction.response.defer()
        if 0 <= n < len(self.view_ref.commands):
            self.view_ref.index = n
            return await interaction.response.edit_message(embed=self.view_ref._embed(), view=self.view_ref)
        await interaction.response.defer()


class CommandHelpView(discord.ui.View):
    """greed-style pagination: one subcommand (1 syntax · 1 example) per page."""

    def __init__(self, group, author, prefix=PREFIX):
        super().__init__(timeout=180)
        self.author = author
        self.prefix = prefix
        self.commands = sorted(group.commands, key=lambda c: c.qualified_name)
        self.index = 0
        em = STATE.get("emojis", {})
        names = {"hp:prev": "left", "hp:next": "right", "hp:close": "xmark"}
        for child in self.children:
            n = names.get(getattr(child, "custom_id", None))
            if n and em.get(n):
                child.emoji = em[n]

    def _embed(self):
        cmd = self.commands[self.index]
        e = build_help_embed(cmd, self.author, self.prefix)
        e.set_footer(text=f"Page {self.index + 1}/{len(self.commands)} · {BRAND}",
                     icon_url=STATE["icon"])
        return e

    async def _guard(self, interaction):
        if interaction.user.id != self.author.id:
            await interaction.response.send_message("This isn't your menu.", ephemeral=True)
            return False
        return True

    @discord.ui.button(emoji="◀", style=discord.ButtonStyle.secondary, custom_id="hp:prev")
    async def prev(self, interaction, button):
        if not await self._guard(interaction):
            return
        self.index = (self.index - 1) % len(self.commands)
        await interaction.response.edit_message(embed=self._embed(), view=self)

    @discord.ui.button(emoji="▶", style=discord.ButtonStyle.secondary, custom_id="hp:next")
    async def nxt(self, interaction, button):
        if not await self._guard(interaction):
            return
        self.index = (self.index + 1) % len(self.commands)
        await interaction.response.edit_message(embed=self._embed(), view=self)

    @discord.ui.button(emoji="✖", style=discord.ButtonStyle.secondary, custom_id="hp:close")
    async def close(self, interaction, button):
        if not await self._guard(interaction):
            return
        await interaction.response.edit_message(view=None)
        self.stop()


async def send_help(dest, cmd, author):
    guild = getattr(dest, "guild", None)
    prefix = (await get_cfg(guild.id)).get("prefix", PREFIX) if guild else PREFIX
    # Every command group uses the greed-style paginated buttons.
    if isinstance(cmd, commands.Group) and len(cmd.commands):
        view = CommandHelpView(cmd, author, prefix)
        return await dest.send(embed=view._embed(), view=view)
    return await dest.send(embed=build_help_embed(cmd, author, prefix))


# ---- Guild config ---------------------------------------------------------
async def get_cfg(guild_id):
    return await db.guild_config.find_one({"_id": str(guild_id)}) or {}


async def set_cfg(guild_id, **kwargs):
    await db.guild_config.update_one({"_id": str(guild_id)}, {"$set": kwargs}, upsert=True)


async def push_cfg(guild_id, field, value):
    await db.guild_config.update_one({"_id": str(guild_id)}, {"$addToSet": {field: value}}, upsert=True)


async def pull_cfg(guild_id, field, value):
    await db.guild_config.update_one({"_id": str(guild_id)}, {"$pull": {field: value}})


async def send_log(guild, embed):
    cfg = await get_cfg(guild.id)
    ch_id = cfg.get("logs_channel") or cfg.get("modlog") or cfg.get("log_channel")
    if ch_id:
        ch = guild.get_channel(int(ch_id))
        if ch:
            try:
                await ch.send(embed=embed)
            except discord.HTTPException:
                pass


async def modlog_entry(guild, action, *, user="N/A", moderator="N/A", reason="No reason provided"):
    await db.guild_config.update_one({"_id": str(guild.id)}, {"$inc": {"modlog_case": 1}}, upsert=True)
    cfg = await db.guild_config.find_one({"_id": str(guild.id)}) or {}
    case = cfg.get("modlog_case", 1)
    now = datetime.now(timezone.utc)
    e = discord.Embed(color=COLOR_LOG, title="Information", timestamp=now,
                      description=(f"**Case #{case}** | {action}\n"
                                   f"**User**: {user}\n"
                                   f"**Moderator**: {moderator}\n"
                                   f"**Reason**: {reason}\n"
                                   f"**Time**: <t:{int(now.timestamp())}:R>"))
    e.set_author(name="Modlog Entry", icon_url=(guild.me.display_avatar.url if guild.me else None))
    return e, case


async def send_modlog(guild, action, *, user="N/A", moderator="N/A", reason="No reason provided"):
    e, case = await modlog_entry(guild, action, user=user, moderator=moderator, reason=reason)
    await send_log(guild, e)
    return case


# ---- Permission check producing the amber warn embed ----------------------
def require_perms(**perms):
    async def predicate(ctx):
        missing = [p for p, v in perms.items() if v and not getattr(ctx.author.guild_permissions, p, False)]
        if missing and ctx.guild:
            cfg = await get_cfg(ctx.guild.id)
            fake = cfg.get("fakeperms", {})
            granted = set()
            for role in ctx.author.roles:
                granted.update(fake.get(str(role.id), []))
            missing = [p for p in missing if p not in granted]
        if missing:
            raise commands.MissingPermissions(missing)
        return True
    return commands.check(predicate)


def parse_duration(s: str) -> int:
    m = re.match(r"^(\d+)([smhd])$", s.lower())
    if not m:
        return 0
    return int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
