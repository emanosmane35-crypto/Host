"""config_cog.py (4/5) — server configuration: prefix, settings, boosts, alias,
sticky, welcome/goodbye, imgonly, invoke, filter, autoresponder, pagination,
enable/disable, ignore, webhook, fakepermissions, reposter, suggest, customize,
badge, pins, seticon/banner, extract emotes/stickers."""
import io
import re
import time
from datetime import datetime, timezone
from typing import Union

import aiohttp
import discord
from discord.ext import commands

from core import (require_perms, ok_embed, warn_embed, err_embed, base_embed, info_embed,
                  get_cfg, set_cfg, push_cfg, pull_cfg, db, BRAND, PREFIX,
                  COLOR, COLOR_OK, COLOR_ERR, COLOR_WARN, build_help_embed, send_help, send_modlog)

sticky_lock = {}


def _antinuke_embed(author, guild, an):
    mods = an.get("modules", {})
    enabled = an.get("enabled")

    def mark(on):
        return "✅" if on else "❌"

    modules = (
        f"Mass Member Ban: {mark(enabled and mods.get('ban', True))}\n"
        f"Channel Creation/Deletion: {mark(enabled and (mods.get('channelcreate', True) or mods.get('channeldelete', True)))}\n"
        f"Mass Member Kick: {mark(enabled and mods.get('kick', True))}\n"
        f"Role Deletion: {mark(enabled and mods.get('roledelete', True))}\n"
        f"Webhook Creation: {mark(enabled and mods.get('webhook', True))}\n"
        f"Emoji Deletion: {mark(enabled and mods.get('emoji', True))}\n"
        f"Vanity Protection: {mark(enabled and mods.get('vanity', False))}"
    )
    keys = ["botadd", "channelcreate", "channeldelete", "rolecreate", "roledelete", "webhook", "admin"]
    active = sum(1 for k in keys if enabled and mods.get(k, True))
    wl_bots, wl_members = [], []
    for uid in an.get("whitelist", []):
        m = guild.get_member(int(uid)) if guild else None
        (wl_bots if (m and m.bot) else wl_members).append(f"<@{uid}>")
    for uid in an.get("whitelist_bots", []):
        wl_bots.append(f"<@{uid}>")
    general = (
        f"Whitelisted Bots: {', '.join(wl_bots) or 'None'}\n"
        f"Whitelisted Members: {', '.join(wl_members) or 'None'}\n"
        f"Protection Modules: {active} enabled\n"
        f"Deny Bot Joins (botadd): {mark(enabled and mods.get('botadd', True))}"
    )
    e = discord.Embed(color=COLOR, title="Settings",
                      description=f"Antinuke is **{'enabled' if enabled else 'disabled'}** in this server")
    e.add_field(name="Modules", value=modules, inline=False)
    e.add_field(name="General", value=general, inline=False)
    e.set_author(name=author.display_name, icon_url=author.display_avatar.url)
    return e


def opt_on(val: str) -> bool:
    return str(val).lower() in ("yes", "on", "enable", "enabled", "true", "1")


_LOG_CATS = ["Message", "Member", "Role", "Channel", "Voice", "Invite",
             "Emoji", "Sticker", "Integration", "Server"]


class LogSelect(discord.ui.Select):
    def __init__(self, guild_id, current):
        self.guild_id = guild_id
        opts = [discord.SelectOption(label=c, value=c, default=(current is None or c in current))
                for c in _LOG_CATS]
        super().__init__(placeholder="Select events to log...", min_values=0,
                         max_values=len(_LOG_CATS), options=opts)

    async def callback(self, interaction):
        await set_cfg(self.guild_id, log_events=self.values)
        await interaction.response.send_message(
            f"✓ Now logging: **{', '.join(self.values) if self.values else 'none'}**", ephemeral=True)


class LogChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, guild_id):
        self.guild_id = guild_id
        super().__init__(channel_types=[discord.ChannelType.text],
                         placeholder="Select a logging channel...", min_values=1, max_values=1)

    async def callback(self, interaction):
        ch = self.values[0]
        await set_cfg(self.guild_id, logs_channel=str(ch.id), modlog=str(ch.id))
        await interaction.response.send_message(f"✓ Logging channel set to <#{ch.id}>", ephemeral=True)


class LogEventsView(discord.ui.View):
    def __init__(self, guild_id, current):
        super().__init__(timeout=180)
        self.add_item(LogChannelSelect(guild_id))
        self.add_item(LogSelect(guild_id, current))


_EMBED_BLOCK_RE = re.compile(r"\{(\w+):\s*(.*?)\}", re.DOTALL)


def parse_embed_code(code: str):
    """Parse the website Embed Builder code → (content, embed, view)."""
    idx = code.find("{embed}")
    if idx != -1:
        content = code[:idx].strip()
        body = code[idx + len("{embed}"):]
    else:
        first = code.find("{")
        content = code[:first].strip() if first > 0 else ""
        body = code
    e = discord.Embed()
    has = False
    btns = []
    for m in _EMBED_BLOCK_RE.finditer(body):
        key, val = m.group(1).lower(), m.group(2).strip()
        if key == "embed" or not val:
            continue
        if key == "color":
            try:
                e.color = discord.Color(int(val.replace("#", ""), 16)); has = True
            except ValueError:
                pass
        elif key == "title":
            e.title = val[:256]; has = True
        elif key == "url":
            e.url = val
        elif key == "description":
            e.description = val[:4096]; has = True
        elif key == "author":
            p = [x.strip() for x in val.split("&&")]
            e.set_author(name=p[0][:256], icon_url=(p[1] if len(p) > 1 and p[1] else None)); has = True
        elif key == "thumbnail":
            e.set_thumbnail(url=val); has = True
        elif key == "image":
            e.set_image(url=val); has = True
        elif key == "footer":
            p = [x.strip() for x in val.split("&&")]
            e.set_footer(text=p[0][:2048], icon_url=(p[1] if len(p) > 1 and p[1] else None)); has = True
        elif key == "field":
            p = [x.strip() for x in val.split("&&")]
            inline = len(p) > 2 and p[2].lower() in ("true", "1", "yes")
            e.add_field(name=(p[0][:256] or "\u200b"),
                        value=((p[1][:1024] if len(p) > 1 and p[1] else "\u200b")), inline=inline); has = True
        elif key == "button":
            p = [x.strip() for x in val.split("&&")]
            if p and p[0]:
                btns.append((p[0][:80], p[1] if len(p) > 1 and p[1] else None))
    view = None
    valid_btns = [(l, u) for l, u in btns if u and u.startswith("http")]
    if valid_btns:
        view = discord.ui.View()
        for label, link in valid_btns:
            view.add_item(discord.ui.Button(label=label, url=link))
    return (content or None), (e if has else None), view


class Configuration(commands.Cog):
    def __init__(self, bot): self.bot = bot

    # ===== LISTENERS =====
    @commands.Cog.listener()
    async def on_member_join(self, member):
        cfg = await get_cfg(member.guild.id)
        # anti-nuke bot-add protection
        an = cfg.get("antinuke", {})
        if member.bot and an.get("enabled") and an.get("modules", {}).get("botadd", True):
            actor = await self._audit_actor(member.guild, discord.AuditLogAction.bot_add)
            try:
                await member.kick(reason="Anti-Nuke: unauthorized bot add")
            except discord.HTTPException:
                pass
            if actor:
                await self._antinuke_punish(member.guild, actor, "adding a bot")
            return
        for rid in cfg.get("autoroles", []):
            if r := member.guild.get_role(int(rid)):
                try: await member.add_roles(r, reason="Autorole")
                except discord.HTTPException: pass
        for entry in cfg.get("welcome_msgs", []):
            ch = member.guild.get_channel(int(entry["channel"]))
            if ch:
                msg = self._vars(entry["message"], member)
                try: await ch.send(embed=base_embed(msg, color=COLOR_OK))
                except discord.HTTPException: pass

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        for entry in (await get_cfg(member.guild.id)).get("goodbye_msgs", []):
            ch = member.guild.get_channel(int(entry["channel"]))
            if ch:
                try: await ch.send(embed=base_embed(self._vars(entry["message"], member)))
                except discord.HTTPException: pass

    def _vars(self, text, member):
        return (text.replace("{user}", member.mention).replace("{user.name}", member.name)
                .replace("{server}", member.guild.name).replace("{member_count}", str(member.guild.member_count)))

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or not message.guild: return
        cfg = await get_cfg(message.guild.id)
        cl = message.content.lower()
        perm = message.author.guild_permissions
        # imgonly
        if str(message.channel.id) in cfg.get("imgonly", []) and not message.attachments and not perm.manage_messages:
            try: await message.delete()
            except discord.HTTPException: pass
            return
        # filter enforcement
        f = cfg.get("filter", {})
        if not perm.manage_messages:
            if f.get("invites") and re.search(r"discord\.gg/|discord\.com/invite/", cl): return await self._del(message, "invites aren't allowed.")
            if f.get("links") and re.search(r"https?://", cl): return await self._del(message, "links aren't allowed.")
            if f.get("caps") and len(message.content) > 8 and sum(c.isupper() for c in message.content) / max(1, len(message.content)) > 0.7:
                return await self._del(message, "too many caps.")
            if f.get("massmention") and len(message.mentions) >= f.get("massmention", 99): return await self._del(message, "too many mentions.")
            for w in f.get("words", []):
                if w in cl: return await self._del(message, "filtered word.")
        # autoresponders
        for trig, reply in cfg.get("autoresponders", {}).items():
            if trig in cl:
                try: await message.channel.send(reply)
                except discord.HTTPException: pass
                break
        # reaction triggers
        for word, emoji in cfg.get("reactiontriggers", {}).items():
            if word in cl:
                try: await message.add_reaction(emoji)
                except discord.HTTPException: pass
        # sticky
        st = cfg.get("sticky", {}).get(str(message.channel.id))
        if st and time.time() - sticky_lock.get(message.channel.id, 0) > 3:
            sticky_lock[message.channel.id] = time.time()
            try:
                if st.get("last_id"):
                    old = await message.channel.fetch_message(int(st["last_id"])); await old.delete()
            except discord.HTTPException: pass
            try:
                m = await message.channel.send(embed=info_embed("📌 Sticky", st["text"]))
                await db.guild_config.update_one({"_id": str(message.guild.id)}, {"$set": {f"sticky.{message.channel.id}.last_id": str(m.id)}})
            except discord.HTTPException: pass

    async def _del(self, message, reason):
        try:
            await message.delete()
            await message.channel.send(embed=warn_embed(message.author, reason), delete_after=4)
        except discord.HTTPException: pass

    # ===== PREFIX =====
    @commands.group(invoke_without_command=True, help="View guild prefix.")
    async def prefix(self, ctx):
        cur = (await get_cfg(ctx.guild.id)).get('prefix', PREFIX)
        name = ctx.guild.me.display_name
        await ctx.send(embed=ok_embed(ctx.author,
            f"**{name}'s prefix** for this **server** is `{cur}`\n> Set a new prefix by using `{PREFIX}prefix set`"))

    @prefix.command(name="set", help="Set command prefix for server.")
    @require_perms(administrator=True)
    async def prefix_set(self, ctx, symbol):
        await set_cfg(ctx.guild.id, prefix=symbol)
        await ctx.send(embed=ok_embed(ctx.author, f"prefix set to `{symbol}`."))

    @prefix.command(name="remove", help="Remove command prefix for server.")
    @require_perms(administrator=True)
    async def prefix_remove(self, ctx):
        await set_cfg(ctx.guild.id, prefix=PREFIX)
        await ctx.send(embed=ok_embed(ctx.author, f"prefix reset to `{PREFIX}`."))

    @prefix.command(name="self", help="Set a personal prefix across all servers.")
    async def prefix_self(self, ctx, symbol):
        await db.user_prefix.update_one({"_id": str(ctx.author.id)}, {"$set": {"prefix": symbol}}, upsert=True)
        await ctx.send(embed=ok_embed(ctx.author, f"personal prefix set to `{symbol}`."))

    # ===== SETTINGS =====
    @commands.group(invoke_without_command=True, help="Server configuration.")
    @require_perms(manage_guild=True)
    async def settings(self, ctx):
        await send_help(ctx, ctx.command, ctx.author)

    async def _set_simple(self, ctx, key, value, label):
        await set_cfg(ctx.guild.id, **{key: value}); await ctx.send(embed=ok_embed(ctx.author, f"{label} updated."))

    @settings.command(name="modlog")
    @require_perms(manage_guild=True)
    async def settings_modlog(self, ctx, channel: discord.TextChannel):
        await self._set_simple(ctx, "modlog", str(channel.id), f"mod log → {channel.mention}")

    @settings.command(name="joinlogs")
    @require_perms(manage_guild=True)
    async def settings_joinlogs(self, ctx, channel: discord.TextChannel):
        await self._set_simple(ctx, "joinlogs", str(channel.id), f"join logs → {channel.mention}")

    @settings.command(name="staff")
    @require_perms(manage_guild=True)
    async def settings_staff(self, ctx, role: discord.Role):
        await push_cfg(ctx.guild.id, "staff_roles", str(role.id)); await ctx.send(embed=ok_embed(ctx.author, f"added **{role.name}** as staff."))

    @settings.command(name="jail")
    @require_perms(manage_guild=True)
    async def settings_jail(self, ctx, channel: discord.TextChannel):
        await self._set_simple(ctx, "jail_channel", str(channel.id), f"jail channel → {channel.mention}")

    @settings.command(name="jailrole")
    @require_perms(manage_guild=True)
    async def settings_jailrole(self, ctx, role: discord.Role):
        await self._set_simple(ctx, "jail_role", str(role.id), f"jail role → {role.name}")

    @settings.command(name="dj")
    @require_perms(manage_guild=True)
    async def settings_dj(self, ctx, role: discord.Role):
        await self._set_simple(ctx, "dj_role", str(role.id), f"DJ role → {role.name}")

    @settings.command(name="config")
    @require_perms(manage_guild=True)
    async def settings_config(self, ctx):
        cfg = await get_cfg(ctx.guild.id)
        keys = ["prefix", "modlog", "joinlogs", "jail_channel", "jail_role", "dj_role"]
        body = "\n".join(f"**{k}**: {cfg.get(k, '—')}" for k in keys)
        await ctx.send(embed=info_embed("Server Configuration", body))

    @settings.command(name="reset")
    @require_perms(administrator=True)
    async def settings_reset(self, ctx):
        await db.guild_config.delete_one({"_id": str(ctx.guild.id)}); await ctx.send(embed=ok_embed(ctx.author, "settings reset."))

    # ===== STICKY MESSAGE =====
    @commands.group(invoke_without_command=True, aliases=["stickymsg", "sticky"], help="Sticky messages.")
    async def stickymessage(self, ctx):
        await ctx.send(embed=info_embed("Sticky Message", f"`{PREFIX}stickymessage add #ch <msg>` · `view` · `remove` · `list`"))

    @stickymessage.command(name="add")
    @require_perms(manage_guild=True)
    async def sticky_add(self, ctx, channel: discord.TextChannel, *, message):
        await set_cfg(ctx.guild.id, **{f"sticky.{channel.id}": {"text": message}})
        await ctx.send(embed=ok_embed(ctx.author, f"sticky added to {channel.mention}."))

    @stickymessage.command(name="remove")
    @require_perms(manage_guild=True)
    async def sticky_remove(self, ctx, channel: discord.TextChannel):
        await db.guild_config.update_one({"_id": str(ctx.guild.id)}, {"$unset": {f"sticky.{channel.id}": ""}})
        await ctx.send(embed=ok_embed(ctx.author, "sticky removed."))

    @stickymessage.command(name="view")
    async def sticky_view(self, ctx, channel: discord.TextChannel):
        st = (await get_cfg(ctx.guild.id)).get("sticky", {}).get(str(channel.id))
        await ctx.send(embed=info_embed("Sticky", st["text"] if st else "none"))

    @stickymessage.command(name="list")
    async def sticky_list(self, ctx):
        sticky = (await get_cfg(ctx.guild.id)).get("sticky", {})
        await ctx.send(embed=info_embed("Sticky Messages", "\n".join(f"<#{cid}>" for cid in sticky) or "none"))

    # ===== WELCOME / GOODBYE (multi-channel) =====
    @commands.group(invoke_without_command=True, help="Welcome messages.")
    async def welcome(self, ctx):
        await send_help(ctx, ctx.command, ctx.author)

    @welcome.command(name="add")
    @require_perms(manage_guild=True)
    async def welcome_add(self, ctx, channel: discord.TextChannel, *, message):
        await push_cfg(ctx.guild.id, "welcome_msgs", {"channel": str(channel.id), "message": message})
        await ctx.send(embed=ok_embed(ctx.author, f"welcome added to {channel.mention}."))

    @welcome.command(name="remove")
    @require_perms(manage_guild=True)
    async def welcome_remove(self, ctx, channel: discord.TextChannel):
        cfg = await get_cfg(ctx.guild.id)
        msgs = [m for m in cfg.get("welcome_msgs", []) if m["channel"] != str(channel.id)]
        await set_cfg(ctx.guild.id, welcome_msgs=msgs); await ctx.send(embed=ok_embed(ctx.author, "welcome removed."))

    @welcome.command(name="list")
    async def welcome_list(self, ctx):
        msgs = (await get_cfg(ctx.guild.id)).get("welcome_msgs", [])
        await ctx.send(embed=info_embed("Welcome Messages", "\n".join(f"<#{m['channel']}>" for m in msgs) or "none"))

    @welcome.command(name="variables")
    async def welcome_vars(self, ctx):
        await ctx.send(embed=info_embed("Variables", "`{user}` `{user.name}` `{server}` `{member_count}`"))

    @commands.group(invoke_without_command=True, help="Goodbye messages.")
    async def goodbye(self, ctx):
        await send_help(ctx, ctx.command, ctx.author)

    @goodbye.command(name="add")
    @require_perms(manage_guild=True)
    async def goodbye_add(self, ctx, channel: discord.TextChannel, *, message):
        await push_cfg(ctx.guild.id, "goodbye_msgs", {"channel": str(channel.id), "message": message})
        await ctx.send(embed=ok_embed(ctx.author, f"goodbye added to {channel.mention}."))

    @goodbye.command(name="remove")
    @require_perms(manage_guild=True)
    async def goodbye_remove(self, ctx, channel: discord.TextChannel):
        cfg = await get_cfg(ctx.guild.id)
        msgs = [m for m in cfg.get("goodbye_msgs", []) if m["channel"] != str(channel.id)]
        await set_cfg(ctx.guild.id, goodbye_msgs=msgs); await ctx.send(embed=ok_embed(ctx.author, "goodbye removed."))

    @goodbye.command(name="list")
    async def goodbye_list(self, ctx):
        msgs = (await get_cfg(ctx.guild.id)).get("goodbye_msgs", [])
        await ctx.send(embed=info_embed("Goodbye Messages", "\n".join(f"<#{m['channel']}>" for m in msgs) or "none"))

    @goodbye.command(name="variables")
    async def goodbye_vars(self, ctx):
        await ctx.send(embed=info_embed("Variables", "`{user}` `{user.name}` `{server}` `{member_count}`"))

    # ===== IMGONLY =====
    @commands.group(invoke_without_command=True, help="Image-only channels.")
    async def imgonly(self, ctx):
        await send_help(ctx, ctx.command, ctx.author)

    @imgonly.command(name="add")
    @require_perms(manage_guild=True)
    async def imgonly_add(self, ctx, channel: discord.TextChannel):
        await push_cfg(ctx.guild.id, "imgonly", str(channel.id)); await ctx.send(embed=ok_embed(ctx.author, f"{channel.mention} is now image-only."))

    @imgonly.command(name="remove")
    @require_perms(manage_guild=True)
    async def imgonly_remove(self, ctx, channel: discord.TextChannel):
        await pull_cfg(ctx.guild.id, "imgonly", str(channel.id)); await ctx.send(embed=ok_embed(ctx.author, "removed image-only."))

    @imgonly.command(name="list")
    async def imgonly_list(self, ctx):
        await ctx.send(embed=info_embed("Image-Only Channels", "\n".join(f"<#{c}>" for c in (await get_cfg(ctx.guild.id)).get("imgonly", [])) or "none"))

    # ===== ALIAS =====
    @commands.group(invoke_without_command=True, help="Command shortcuts.")
    async def alias(self, ctx):
        await send_help(ctx, ctx.command, ctx.author)

    @alias.command(name="add")
    @require_perms(manage_guild=True)
    async def alias_add(self, ctx, shortcut, *, command):
        await set_cfg(ctx.guild.id, **{f"aliases.{shortcut.lower()}": command})
        await ctx.send(embed=ok_embed(ctx.author, f"`{shortcut}` → `{command}`."))

    @alias.command(name="remove")
    @require_perms(manage_guild=True)
    async def alias_remove(self, ctx, shortcut):
        await db.guild_config.update_one({"_id": str(ctx.guild.id)}, {"$unset": {f"aliases.{shortcut.lower()}": ""}})
        await ctx.send(embed=ok_embed(ctx.author, f"removed alias `{shortcut}`."))

    @alias.command(name="list")
    async def alias_list(self, ctx):
        al = (await get_cfg(ctx.guild.id)).get("aliases", {})
        await ctx.send(embed=info_embed("Aliases", "\n".join(f"`{k}` → `{v}`" for k, v in al.items()) or "none"))

    # ===== AUTORESPONDER =====
    @commands.group(invoke_without_command=True, aliases=["ar"], help="Auto-responders.")
    async def autoresponder(self, ctx):
        await send_help(ctx, ctx.command, ctx.author)

    @autoresponder.command(name="add")
    @require_perms(manage_channels=True)
    async def ar_add(self, ctx, trigger, *, reply):
        await set_cfg(ctx.guild.id, **{f"autoresponders.{trigger.lower()}": reply})
        await ctx.send(embed=ok_embed(ctx.author, f"added responder for `{trigger}`."))

    @autoresponder.command(name="remove")
    @require_perms(manage_channels=True)
    async def ar_remove(self, ctx, trigger):
        await db.guild_config.update_one({"_id": str(ctx.guild.id)}, {"$unset": {f"autoresponders.{trigger.lower()}": ""}})
        await ctx.send(embed=ok_embed(ctx.author, f"removed `{trigger}`."))

    @autoresponder.command(name="list")
    async def ar_list(self, ctx):
        ar = (await get_cfg(ctx.guild.id)).get("autoresponders", {})
        await ctx.send(embed=info_embed("Auto Responders", "\n".join(f"`{k}`" for k in ar) or "none"))

    @autoresponder.command(name="variables")
    async def ar_vars(self, ctx):
        await ctx.send(embed=info_embed("Variables", "`{user}` `{server}` `{channel}`"))

    # ===== FILTER =====
    @commands.group(invoke_without_command=True, aliases=["automod"], help="Chat filters.")
    @require_perms(manage_channels=True)
    async def filter(self, ctx):
        f = (await get_cfg(ctx.guild.id)).get("filter", {})
        body = "\n".join(f"**{k}**: {f.get(k, 'off')}" for k in ["links", "invites", "caps", "spam", "massmention", "emoji", "spoilers"])
        await ctx.send(embed=info_embed("Filter & AutoMod", body + f"\n\n`{PREFIX}filter <type> on/off` · `filter add <word>` · `filter list`"))

    @filter.command(name="add")
    @require_perms(manage_guild=True)
    async def filter_add(self, ctx, *, word):
        await push_cfg(ctx.guild.id, "filter.words", word.lower()); await ctx.send(embed=ok_embed(ctx.author, f"added `{word}`."))

    @filter.command(name="remove")
    @require_perms(manage_guild=True)
    async def filter_remove(self, ctx, *, word):
        await pull_cfg(ctx.guild.id, "filter.words", word.lower()); await ctx.send(embed=ok_embed(ctx.author, f"removed `{word}`."))

    @filter.command(name="list")
    async def filter_list(self, ctx):
        words = (await get_cfg(ctx.guild.id)).get("filter", {}).get("words", [])
        await ctx.send(embed=info_embed("Filtered Words", ", ".join(f"`{w}`" for w in words) or "none"))

    @filter.command(name="links")
    @require_perms(manage_channels=True)
    async def filter_links(self, ctx, setting): await set_cfg(ctx.guild.id, **{"filter.links": opt_on(setting)}); await ctx.send(embed=ok_embed(ctx.author, "links filter updated."))

    @filter.command(name="invites")
    @require_perms(manage_channels=True)
    async def filter_invites(self, ctx, setting): await set_cfg(ctx.guild.id, **{"filter.invites": opt_on(setting)}); await ctx.send(embed=ok_embed(ctx.author, "invites filter updated."))

    @filter.command(name="caps")
    @require_perms(manage_channels=True)
    async def filter_caps(self, ctx, setting): await set_cfg(ctx.guild.id, **{"filter.caps": opt_on(setting)}); await ctx.send(embed=ok_embed(ctx.author, "caps filter updated."))

    @filter.command(name="spam")
    @require_perms(manage_channels=True)
    async def filter_spam(self, ctx, setting): await set_cfg(ctx.guild.id, **{"filter.spam": opt_on(setting)}); await ctx.send(embed=ok_embed(ctx.author, "spam filter updated."))

    @filter.command(name="massmention")
    @require_perms(manage_channels=True)
    async def filter_mm(self, ctx, threshold: int): await set_cfg(ctx.guild.id, **{"filter.massmention": threshold}); await ctx.send(embed=ok_embed(ctx.author, f"mass-mention threshold {threshold}."))

    @filter.command(name="reset")
    @require_perms(manage_guild=True)
    async def filter_reset(self, ctx): await set_cfg(ctx.guild.id, filter={}); await ctx.send(embed=ok_embed(ctx.author, "filters reset."))

    # ===== FAKEPERMISSIONS =====
    @commands.group(invoke_without_command=True, aliases=["fakeperms"], help="Fake permissions for roles.")
    @require_perms(administrator=True)
    async def fakepermissions(self, ctx):
        await send_help(ctx, ctx.command, ctx.author)

    @fakepermissions.command(name="add")
    @require_perms(administrator=True)
    async def fp_add(self, ctx, role: discord.Role, perm):
        await push_cfg(ctx.guild.id, f"fakeperms.{role.id}", perm); await ctx.send(embed=ok_embed(ctx.author, f"granted `{perm}` to **{role.name}**."))

    @fakepermissions.command(name="remove")
    @require_perms(administrator=True)
    async def fp_remove(self, ctx, role: discord.Role, perm):
        await pull_cfg(ctx.guild.id, f"fakeperms.{role.id}", perm); await ctx.send(embed=ok_embed(ctx.author, f"revoked `{perm}` from **{role.name}**."))

    @fakepermissions.command(name="list")
    async def fp_list(self, ctx, role: discord.Role):
        perms = (await get_cfg(ctx.guild.id)).get("fakeperms", {}).get(str(role.id), [])
        await ctx.send(embed=info_embed(f"Fake perms — {role.name}", "\n".join(f"`{p}`" for p in perms) or "none"))

    @fakepermissions.command(name="reset")
    @require_perms(administrator=True)
    async def fp_reset(self, ctx):
        await set_cfg(ctx.guild.id, fakeperms={}); await ctx.send(embed=ok_embed(ctx.author, "fake permissions reset."))

    # ===== INVOKE (custom punishment messages, generic) =====
    @commands.command(help="Set custom punishment messages: ,invoke <action> <message|message view>")
    @require_perms(manage_guild=True)
    async def invoke(self, ctx, action: str = None, kind: str = "message", *, message: str = None):
        actions = ["ban", "kick", "mute", "unmute", "warn", "jail", "timeout", "untimeout", "tempban", "unban", "softban", "hardban"]
        if not action or action not in actions:
            return await ctx.send(embed=info_embed("Invoke", f"`{PREFIX}invoke <action> message <text>` to set, `... message view` to view.\nActions: {', '.join(f'`{a}`' for a in actions)}"))
        key = f"invoke.{action}"
        if kind == "view" or (message is None and kind != "view"):
            val = (await get_cfg(ctx.guild.id)).get("invoke", {}).get(action, "default")
            return await ctx.send(embed=info_embed(f"Invoke — {action}", val))
        await set_cfg(ctx.guild.id, **{key: message})
        await ctx.send(embed=ok_embed(ctx.author, f"`{action}` message updated."))

    # ===== ENABLE / DISABLE =====
    @commands.command(help="Disable a command in this server.")
    @require_perms(manage_channels=True)
    async def disablecommand(self, ctx, *, command):
        await push_cfg(ctx.guild.id, "disabled_commands", command.lower()); await ctx.send(embed=ok_embed(ctx.author, f"disabled `{command}`."))

    @commands.command(help="Enable a previously disabled command.")
    @require_perms(manage_channels=True)
    async def enablecommand(self, ctx, *, command):
        await pull_cfg(ctx.guild.id, "disabled_commands", command.lower()); await ctx.send(embed=ok_embed(ctx.author, f"enabled `{command}`."))

    @commands.command(help="Disable a module.")
    @require_perms(manage_channels=True)
    async def disablemodule(self, ctx, *, module):
        await push_cfg(ctx.guild.id, "disabled_modules", module.lower()); await ctx.send(embed=ok_embed(ctx.author, f"disabled module `{module}`."))

    @commands.command(help="Enable a module.")
    @require_perms(manage_channels=True)
    async def enablemodule(self, ctx, *, module):
        await pull_cfg(ctx.guild.id, "disabled_modules", module.lower()); await ctx.send(embed=ok_embed(ctx.author, f"enabled module `{module}`."))

    # ===== IGNORE =====
    @commands.group(invoke_without_command=True, help="Ignore members/channels.")
    @require_perms(administrator=True)
    async def ignore(self, ctx):
        await send_help(ctx, ctx.command, ctx.author)

    @ignore.command(name="add")
    @require_perms(administrator=True)
    async def ignore_add(self, ctx, target: Union[discord.Member, discord.TextChannel]):
        await push_cfg(ctx.guild.id, "ignored", str(target.id)); await ctx.send(embed=ok_embed(ctx.author, "added to ignore list."))

    @ignore.command(name="remove")
    @require_perms(administrator=True)
    async def ignore_remove(self, ctx, target: Union[discord.Member, discord.TextChannel]):
        await pull_cfg(ctx.guild.id, "ignored", str(target.id)); await ctx.send(embed=ok_embed(ctx.author, "removed from ignore list."))

    @ignore.command(name="list")
    async def ignore_list(self, ctx):
        await ctx.send(embed=info_embed("Ignored", ", ".join((await get_cfg(ctx.guild.id)).get("ignored", [])) or "none"))

    # ===== SERVER BRANDING =====
    @commands.command(help="Set a new guild icon.")
    @require_perms(manage_guild=True)
    @commands.bot_has_permissions(manage_guild=True)
    async def seticon(self, ctx, url):
        async with aiohttp.ClientSession() as s:
            async with s.get(url) as r:
                await ctx.guild.edit(icon=await r.read())
        await ctx.send(embed=ok_embed(ctx.author, "**guild icon updated.**"))

    @commands.command(help="Set a new guild banner.")
    @require_perms(manage_guild=True)
    @commands.bot_has_permissions(manage_guild=True)
    async def setbanner(self, ctx, url):
        async with aiohttp.ClientSession() as s:
            async with s.get(url) as r:
                await ctx.guild.edit(banner=await r.read())
        await ctx.send(embed=ok_embed(ctx.author, "guild banner updated."))

    # ===== EXTRACT =====
    @commands.command(help="Send all server emojis in a zip.")
    @require_perms(administrator=True)
    async def extractemotes(self, ctx):
        import zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for e in ctx.guild.emojis:
                z.writestr(f"{e.name}.{'gif' if e.animated else 'png'}", await e.read())
        buf.seek(0)
        await ctx.send(file=discord.File(buf, "emotes.zip"))

    @commands.command(help="Send all server stickers in a zip.")
    @require_perms(administrator=True)
    async def extractstickers(self, ctx):
        import zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for s in ctx.guild.stickers:
                z.writestr(f"{s.name}.png", await s.read())
        buf.seek(0)
        await ctx.send(file=discord.File(buf, "stickers.zip"))

    # ===== WEBHOOK =====
    @commands.group(invoke_without_command=True, help="Manage webhooks.")
    @require_perms(manage_webhooks=True)
    async def webhook(self, ctx):
        await send_help(ctx, ctx.command, ctx.author)

    @webhook.command(name="create")
    @require_perms(manage_webhooks=True)
    @commands.bot_has_permissions(manage_webhooks=True)
    async def wh_create(self, ctx, *, name):
        wh = await ctx.channel.create_webhook(name=name)
        await ctx.send(embed=ok_embed(ctx.author, f"created webhook **{name}**."))

    @webhook.command(name="send")
    @require_perms(manage_webhooks=True)
    async def wh_send(self, ctx, name, *, message):
        whs = await ctx.channel.webhooks()
        wh = discord.utils.get(whs, name=name)
        if not wh: return await ctx.send(embed=warn_embed(ctx.author, "webhook not found in this channel."))
        await wh.send(message)
        await ctx.send(embed=ok_embed(ctx.author, "message sent."))

    @webhook.command(name="list")
    @require_perms(manage_webhooks=True)
    async def wh_list(self, ctx):
        whs = await ctx.guild.webhooks()
        await ctx.send(embed=info_embed("Webhooks", "\n".join(f"**{w.name}** in {w.channel.mention}" for w in whs) or "none"))

    @webhook.command(name="delete")
    @require_perms(manage_webhooks=True)
    async def wh_delete(self, ctx, *, name):
        whs = await ctx.channel.webhooks()
        wh = discord.utils.get(whs, name=name)
        if wh: await wh.delete()
        await ctx.send(embed=ok_embed(ctx.author, "webhook deleted."))

    # ===== SUGGEST =====
    @commands.group(invoke_without_command=True, help="Submit a suggestion.")
    async def suggest(self, ctx, *, suggestion=None):
        if not suggestion:
            return await send_help(ctx, ctx.command, ctx.author)
        ch_id = (await get_cfg(ctx.guild.id)).get("suggest_channel")
        ch = ctx.guild.get_channel(int(ch_id)) if ch_id else ctx.channel
        e = info_embed("New Suggestion", suggestion, author=str(ctx.author), author_icon=ctx.author.display_avatar.url)
        m = await ch.send(embed=e)
        try: await m.add_reaction("👍"); await m.add_reaction("👎")
        except discord.HTTPException: pass
        if ch != ctx.channel: await ctx.send(embed=ok_embed(ctx.author, "suggestion submitted."))

    @suggest.command(name="set")
    @require_perms(manage_channels=True)
    async def suggest_set(self, ctx, channel: discord.TextChannel):
        await set_cfg(ctx.guild.id, suggest_channel=str(channel.id)); await ctx.send(embed=ok_embed(ctx.author, f"suggestions → {channel.mention}."))

    # ===== BOOSTS =====
    @commands.group(invoke_without_command=True, help="Boost messages.")
    async def boosts(self, ctx):
        await send_help(ctx, ctx.command, ctx.author)

    @boosts.command(name="add")
    @require_perms(manage_guild=True)
    async def boosts_add(self, ctx, channel: discord.TextChannel, *, message):
        await push_cfg(ctx.guild.id, "boost_msgs", {"channel": str(channel.id), "message": message})
        await ctx.send(embed=ok_embed(ctx.author, f"boost message added to {channel.mention}."))

    @boosts.command(name="list")
    async def boosts_list(self, ctx):
        msgs = (await get_cfg(ctx.guild.id)).get("boost_msgs", [])
        await ctx.send(embed=info_embed("Boost Messages", "\n".join(f"<#{m['channel']}>" for m in msgs) or "none"))

    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        if not before.premium_since and after.premium_since:
            for entry in (await get_cfg(after.guild.id)).get("boost_msgs", []):
                ch = after.guild.get_channel(int(entry["channel"]))
                if ch:
                    try: await ch.send(embed=base_embed(self._vars(entry["message"], after)))
                    except discord.HTTPException: pass

    # ===== AUTOROLE / LOGGING =====
    @commands.group(invoke_without_command=True, help="Autoroles.")
    async def autorole(self, ctx):
        await send_help(ctx, ctx.command, ctx.author)

    @autorole.command(name="add")
    @require_perms(manage_roles=True)
    async def autorole_add(self, ctx, role: discord.Role):
        await push_cfg(ctx.guild.id, "autoroles", str(role.id)); await ctx.send(embed=ok_embed(ctx.author, f"new members get **{role.name}**."))

    @autorole.command(name="remove")
    @require_perms(manage_roles=True)
    async def autorole_remove(self, ctx, role: discord.Role):
        await pull_cfg(ctx.guild.id, "autoroles", str(role.id)); await ctx.send(embed=ok_embed(ctx.author, f"removed **{role.name}**."))

    # ===== LOGS (server audit logs — Modlog Entry format) =====
    @commands.group(invoke_without_command=True, aliases=["logging"],
                    help="Configure server event logging (Modlog Entry style).")
    @require_perms(manage_guild=True)
    async def logs(self, ctx):
        cfg = await get_cfg(ctx.guild.id)
        ch = cfg.get("logs_channel")
        events = cfg.get("log_events")
        desc = (f"> Logging to <#{ch}> · Events: **{', '.join(events) if events else 'All'}**"
                if ch else f"> No logging channels are configured. Use `{PREFIX}logs add #channel` to add one.")
        e = discord.Embed(color=COLOR, title="Logging Channels", description=desc)
        e.set_author(name=ctx.guild.name, icon_url=ctx.guild.icon.url if ctx.guild.icon else None)
        await ctx.send(embed=e, view=LogEventsView(ctx.guild.id, events))

    @logs.command(name="add", aliases=["set"], help="Set the logging channel: ,logs add #channel")
    @require_perms(manage_guild=True)
    async def logs_add(self, ctx, channel: discord.TextChannel):
        await set_cfg(ctx.guild.id, logs_channel=str(channel.id), modlog=str(channel.id))
        await ctx.send(embed=ok_embed(ctx.author, f"server logs → {channel.mention}. Pick which events to log below."),
                       view=LogEventsView(ctx.guild.id, (await get_cfg(ctx.guild.id)).get("log_events")))

    @logs.command(name="disable", aliases=["off", "remove"], help="Turn off server logging.")
    @require_perms(manage_guild=True)
    async def logs_disable(self, ctx):
        await db.guild_config.update_one({"_id": str(ctx.guild.id)}, {"$unset": {"logs_channel": ""}})
        await ctx.send(embed=ok_embed(ctx.author, "server logging disabled."))

    async def _slog(self, guild, category, action, *, user="N/A", moderator="N/A", reason="—"):
        cfg = await get_cfg(guild.id)
        if not cfg.get("logs_channel"):
            return
        events = cfg.get("log_events")
        if events is not None and category not in events:
            return
        await send_modlog(guild, action, user=user, moderator=moderator, reason=reason)

    @commands.Cog.listener("on_message_delete")
    async def log_message_delete(self, message):
        if not message.guild or message.author.bot:
            return
        parts = []
        if message.content:
            parts.append(message.content[:400])
        if message.attachments:
            parts.append("📎 " + ", ".join(a.url for a in message.attachments))
        if message.stickers:
            parts.append("🏷 sticker: " + ", ".join(s.name for s in message.stickers))
        content = "\n".join(parts) or "*(no text — embed/other)*"
        await self._slog(message.guild, "Message", "message deleted",
                         user=f"{message.author} ({message.author.id})", moderator=f"#{message.channel}",
                         reason=content[:900])

    @commands.Cog.listener("on_message_edit")
    async def log_message_edit(self, before, after):
        if not before.guild or before.author.bot or before.content == after.content:
            return
        await self._slog(before.guild, "Message", "message edited",
                         user=f"{before.author} ({before.author.id})", moderator=f"#{before.channel}",
                         reason=f"{(before.content or '—')[:200]} → {(after.content or '—')[:200]}")

    @commands.Cog.listener("on_member_join")
    async def log_member_join(self, member):
        await self._slog(member.guild, "Member", "member joined",
                         user=f"{member} ({member.id})", reason=f"Member #{member.guild.member_count}")

    @commands.Cog.listener("on_member_remove")
    async def log_member_remove(self, member):
        await self._slog(member.guild, "Member", "member left", user=f"{member} ({member.id})")

    @commands.Cog.listener("on_member_ban")
    async def log_member_ban(self, guild, user):
        actor = await self._audit_actor(guild, discord.AuditLogAction.ban)
        await self._slog(guild, "Member", "member banned", user=f"{user} ({user.id})",
                         moderator=(f"<@{actor}>" if actor else "N/A"))

    @commands.Cog.listener("on_member_unban")
    async def log_member_unban(self, guild, user):
        actor = await self._audit_actor(guild, discord.AuditLogAction.unban)
        await self._slog(guild, "Member", "member unbanned", user=f"{user} ({user.id})",
                         moderator=(f"<@{actor}>" if actor else "N/A"))

    @commands.Cog.listener("on_member_update")
    async def log_member_update(self, before, after):
        if before.nick != after.nick:
            await self._slog(after.guild, "Member", "nickname changed", user=f"{after} ({after.id})",
                             reason=f"{before.nick or '—'} → {after.nick or '—'}")
        added = [r.name for r in after.roles if r not in before.roles]
        removed = [r.name for r in before.roles if r not in after.roles]
        if added or removed:
            chg = (("+" + ", ".join(added) + " ") if added else "") + (("-" + ", ".join(removed)) if removed else "")
            await self._slog(after.guild, "Member", "roles updated", user=f"{after} ({after.id})", reason=chg.strip())

    @commands.Cog.listener("on_guild_channel_create")
    async def log_channel_create(self, channel):
        await self._slog(channel.guild, "Channel", "channel created", user=f"#{channel.name} ({channel.id})")

    @commands.Cog.listener("on_guild_channel_delete")
    async def log_channel_delete(self, channel):
        await self._slog(channel.guild, "Channel", "channel deleted", user=f"#{channel.name} ({channel.id})")

    @commands.Cog.listener("on_guild_channel_update")
    async def log_channel_update(self, before, after):
        if before.name != after.name:
            await self._slog(after.guild, "Channel", "channel renamed", user=f"({after.id})",
                             reason=f"#{before.name} → #{after.name}")

    @commands.Cog.listener("on_guild_role_create")
    async def log_role_create(self, role):
        await self._slog(role.guild, "Role", "role created", user=f"{role.name} ({role.id})")

    @commands.Cog.listener("on_guild_role_delete")
    async def log_role_delete(self, role):
        await self._slog(role.guild, "Role", "role deleted", user=f"{role.name} ({role.id})")

    @commands.Cog.listener("on_guild_role_update")
    async def log_role_update(self, before, after):
        if before.name != after.name:
            await self._slog(after.guild, "Role", "role renamed", user=f"({after.id})",
                             reason=f"{before.name} → {after.name}")

    @commands.Cog.listener("on_voice_state_update")
    async def log_voice(self, member, before, after):
        if before.channel == after.channel:
            return
        if before.channel is None:
            action, reason = "voice joined", after.channel.name
        elif after.channel is None:
            action, reason = "voice left", before.channel.name
        else:
            action, reason = "voice moved", f"{before.channel.name} → {after.channel.name}"
        await self._slog(member.guild, "Voice", action, user=f"{member} ({member.id})", reason=reason)

    # ===== REACTION TRIGGERS =====
    @commands.command(help="Auto-react to a keyword: ,reactiontrigger <word> <emoji>")
    @require_perms(manage_guild=True)
    async def reactiontrigger(self, ctx, word, emoji):
        await set_cfg(ctx.guild.id, **{f"reactiontriggers.{word.lower()}": emoji})
        await ctx.send(embed=ok_embed(ctx.author, f"reacting with {emoji} on `{word}`."))

    # ===== SETUP =====
    @commands.command(help="Auto-setup: creates logging channel, jail/mute roles, and the VoiceMaster system.")
    @require_perms(administrator=True)
    @commands.bot_has_permissions(manage_channels=True, manage_roles=True)
    async def setup(self, ctx):
        from general_cog import ensure_vm_emojis, interface_embed, VoiceInterface
        cfg = await get_cfg(ctx.guild.id)
        status = await ctx.send(embed=info_embed("Setup", "⏳ Setting up the moderation system..."))

        # 1) logging channel
        if not cfg.get("logs_channel"):
            try:
                logch = await ctx.guild.create_text_channel("mod-logs", reason="canary setup")
                await set_cfg(ctx.guild.id, logs_channel=str(logch.id), modlog=str(logch.id))
            except discord.HTTPException:
                pass

        # 2) jail + mute roles
        async def _gor(name):
            r = discord.utils.get(ctx.guild.roles, name=name)
            if not r:
                try:
                    r = await ctx.guild.create_role(name=name, reason="canary setup")
                except discord.HTTPException:
                    return None
            return r
        jailed = await _gor("jailed")
        imute = await _gor("imute")
        rmute = await _gor("rmute")
        if jailed:
            await set_cfg(ctx.guild.id, jail_role=str(jailed.id))

        # 3) jail channel (only jailed can see)
        if not cfg.get("jail_channel") and jailed:
            try:
                overwrites = {ctx.guild.default_role: discord.PermissionOverwrite(view_channel=False),
                              jailed: discord.PermissionOverwrite(view_channel=True, send_messages=True)}
                jailch = await ctx.guild.create_text_channel("jail", overwrites=overwrites, reason="canary setup")
                await set_cfg(ctx.guild.id, jail_channel=str(jailch.id))
            except discord.HTTPException:
                pass

        # 4) VoiceMaster (own category + interface "menu" + voice "j2c")
        hub_ok = cfg.get("vm_hub") and ctx.guild.get_channel(int(cfg["vm_hub"]))
        if not hub_ok:
            emojis = {}
            if ctx.guild.me.guild_permissions.manage_emojis:
                try:
                    emojis = await ensure_vm_emojis(ctx.guild)
                except discord.HTTPException:
                    emojis = {}
            try:
                j2c_cat = await ctx.guild.create_category("VoiceMaster")
                vc_cat = await ctx.guild.create_category("Voice Channels")
                hub = await ctx.guild.create_voice_channel("j2c", category=j2c_cat)
                interface = await ctx.guild.create_text_channel("menu", category=j2c_cat)
                await interface.send(embed=interface_embed(emojis, ctx.guild, ctx.guild.me), view=VoiceInterface(emojis))
                await set_cfg(ctx.guild.id, vm_hub=str(hub.id), vm_j2c_category=str(j2c_cat.id),
                              vm_vc_category=str(vc_cat.id), vm_interface=str(interface.id))
            except discord.HTTPException:
                pass

        # 5) results
        name = ctx.guild.me.display_name
        await status.edit(embed=ok_embed(ctx.author,
            "**Moderation system set up** has been completed. Please make sure that all your "
            "channels and roles have been configured properly."))

        bot_top = ctx.guild.me.top_role
        below = [r.name for r in (jailed, imute, rmute) if r and bot_top.position <= r.position]
        if below:
            await ctx.send(embed=warn_embed(ctx.author,
                f"**{name}** must be higher than the **jailed**, **imute**, and **rmute** roles. "
                f"In Server Settings → Roles, drag **{name}** above those roles, then run `setup` again."))

        await send_modlog(ctx.guild, "setup", user="N/A",
                          moderator=f"{ctx.author} ({ctx.author.id})", reason="Moderation setup completed")

    async def _post_embed(self, ctx, code):
        if not code:
            return await ctx.send(embed=info_embed("Custom Embed",
                f"Build an embed on the website Embed Builder, copy the code, then run:\n"
                f"`{PREFIX}embed <code>` or `{PREFIX}ec embed <code>`\n\n"
                "Supports `{embed}` `{title:}` `{description:}` `{color: #hex}` `{author: name && icon}` "
                "`{thumbnail:}` `{image:}` `{field: name && value && true}` `{footer: text && icon}` `{button: label && url}`."))
        content, e, view = parse_embed_code(code)
        if not e and not content:
            return await ctx.send(embed=warn_embed(ctx.author, "couldn't parse that embed code — copy it from the website Embed Builder."))
        kwargs = {}
        if content:
            kwargs["content"] = content
        if e:
            kwargs["embed"] = e
        if view:
            kwargs["view"] = view
        await ctx.send(**kwargs)
        try:
            await ctx.message.delete()
        except discord.HTTPException:
            pass

    @commands.command(name="embed", aliases=["createembed"],
                      help="Post a custom embed from website Embed Builder code.")
    @require_perms(manage_messages=True)
    async def embed_cmd(self, ctx, *, code: str = None):
        await self._post_embed(ctx, code)

    @commands.group(name="ec", invoke_without_command=True,
                    help="Create a custom embed (paste website Embed Builder code).")
    @require_perms(manage_messages=True)
    async def ec(self, ctx, *, code: str = None):
        if code and "{" in code:
            return await self._post_embed(ctx, code)
        await send_help(ctx, ctx.command, ctx.author)

    @ec.command(name="embed", help="Create a custom embed from website code.")
    @require_perms(manage_messages=True)
    async def ec_embed(self, ctx, *, code: str = None):
        await self._post_embed(ctx, code)

    @commands.command(help="Set the bot's language for this server.")
    @require_perms(manage_guild=True)
    async def language(self, ctx, code: str):
        await set_cfg(ctx.guild.id, language=code.lower())
        await ctx.send(embed=ok_embed(ctx.author, f"language set to `{code.lower()}`."))

    @commands.command(help="Create a self-assignable role menu message.")
    @require_perms(manage_roles=True)
    async def rolemenu(self, ctx, *, name="Role Menu"):
        e = info_embed(name, "React below to assign yourself a role.\nUse `,reactionrole <messageId> <emoji> <@role>` to bind reactions.")
        m = await ctx.send(embed=e)
        await ctx.send(embed=ok_embed(ctx.author, f"role menu created (message id `{m.id}`)."))

    # ===== INVITES =====
    @commands.group(invoke_without_command=True, help="Show a member's invite count.")
    async def invites(self, ctx, member: discord.Member = None):
        member = member or ctx.author
        try:
            invs = await ctx.guild.invites()
            total = sum(i.uses for i in invs if i.inviter and i.inviter.id == member.id)
        except discord.Forbidden:
            return await ctx.send(embed=warn_embed(ctx.author, "I need `Manage Server` to read invites."))
        await ctx.send(embed=info_embed("Invites", f"**{member}** has **{total}** invites."))

    @invites.command(name="leaderboard", aliases=["top"])
    async def invites_lb(self, ctx):
        try:
            invs = await ctx.guild.invites()
        except discord.Forbidden:
            return await ctx.send(embed=warn_embed(ctx.author, "I need `Manage Server` to read invites."))
        tally = {}
        for i in invs:
            if i.inviter:
                tally[i.inviter] = tally.get(i.inviter, 0) + i.uses
        top = sorted(tally.items(), key=lambda x: x[1], reverse=True)[:10]
        body = "\n".join(f"**{n}.** {u.mention} — {c}" for n, (u, c) in enumerate(top, 1)) or "no invites yet."
        await ctx.send(embed=info_embed("🏆 Invite Leaderboard", body))

    # ===== REACTION ROLES =====
    @commands.command(aliases=["rr"], help="Reaction role: ,reactionrole <messageId> <emoji> <@role>")
    @require_perms(manage_roles=True)
    async def reactionrole(self, ctx, message_id: int, emoji: str, role: discord.Role):
        await set_cfg(ctx.guild.id, **{f"reactionroles.{message_id}.{emoji}": str(role.id)})
        try:
            msg = await ctx.channel.fetch_message(message_id)
            await msg.add_reaction(emoji)
        except discord.HTTPException:
            pass
        await ctx.send(embed=ok_embed(ctx.author, f"{emoji} on that message now grants **{role.name}**."))

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        if not payload.guild_id or (payload.member and payload.member.bot):
            return
        cfg = await get_cfg(payload.guild_id)
        rid = cfg.get("reactionroles", {}).get(str(payload.message_id), {}).get(str(payload.emoji))
        if rid:
            guild = self.bot.get_guild(payload.guild_id)
            role = guild.get_role(int(rid))
            if role and payload.member:
                try: await payload.member.add_roles(role, reason="Reaction role")
                except discord.HTTPException: pass

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload):
        if not payload.guild_id:
            return
        cfg = await get_cfg(payload.guild_id)
        rid = cfg.get("reactionroles", {}).get(str(payload.message_id), {}).get(str(payload.emoji))
        if rid:
            guild = self.bot.get_guild(payload.guild_id)
            member = guild.get_member(payload.user_id)
            role = guild.get_role(int(rid))
            if role and member:
                try: await member.remove_roles(role, reason="Reaction role")
                except discord.HTTPException: pass

    # ===== ANTINUKE =====
    @commands.group(invoke_without_command=True, aliases=["an", "antiraid"], help="Real-time protection against mass destruction.")
    @require_perms(administrator=True)
    async def antinuke(self, ctx, sub: str = None, *, rest: str = None):
        await send_help(ctx, ctx.command, ctx.author)

    @antinuke.command(name="channel", help="Protect against mass channel create + delete.")
    @require_perms(administrator=True)
    async def antinuke_channel(self, ctx, setting: str):
        on = opt_on(setting)
        await set_cfg(ctx.guild.id, **{"antinuke.modules.channelcreate": on, "antinuke.modules.channeldelete": on})
        await ctx.send(embed=ok_embed(ctx.author, f"anti-nuke `channel` (create+delete) {'enabled' if on else 'disabled'}."))

    @antinuke.command(name="role", help="Protect against mass role create + delete.")
    @require_perms(administrator=True)
    async def antinuke_role(self, ctx, setting: str):
        on = opt_on(setting)
        await set_cfg(ctx.guild.id, **{"antinuke.modules.rolecreate": on, "antinuke.modules.roledelete": on})
        await ctx.send(embed=ok_embed(ctx.author, f"anti-nuke `role` (create+delete) {'enabled' if on else 'disabled'}."))

    @antinuke.command(name="enable")
    @require_perms(administrator=True)
    async def antinuke_enable(self, ctx):
        await set_cfg(ctx.guild.id, **{"antinuke.enabled": True}); await ctx.send(embed=ok_embed(ctx.author, "anti-nuke **enabled**."))

    @antinuke.command(name="disable")
    @require_perms(administrator=True)
    async def antinuke_disable(self, ctx):
        await set_cfg(ctx.guild.id, **{"antinuke.enabled": False}); await ctx.send(embed=ok_embed(ctx.author, "anti-nuke **disabled**."))

    @antinuke.command(name="whitelist", aliases=["wl"])
    @require_perms(administrator=True)
    async def antinuke_whitelist(self, ctx, member: discord.Member):
        await push_cfg(ctx.guild.id, "antinuke.whitelist", str(member.id)); await ctx.send(embed=ok_embed(ctx.author, f"whitelisted **{member}**."))

    @antinuke.command(name="unwhitelist", aliases=["unwl", "removewhitelist", "rwl"])
    @require_perms(administrator=True)
    async def antinuke_unwhitelist(self, ctx, member: discord.Member):
        an = (await get_cfg(ctx.guild.id)).get("antinuke", {})
        if str(member.id) not in an.get("whitelist", []):
            return await ctx.send(embed=warn_embed(ctx.author, f"**{member}** is not whitelisted."))
        await pull_cfg(ctx.guild.id, "antinuke.whitelist", str(member.id))
        await ctx.send(embed=ok_embed(ctx.author, f"removed **{member}** from the antinuke whitelist."))

    @antinuke.command(name="punishment")
    @require_perms(administrator=True)
    async def antinuke_punishment(self, ctx, action: str):
        if action not in ("ban", "kick", "strip"):
            return await ctx.send(embed=warn_embed(ctx.author, "punishment must be `ban`, `kick`, or `strip`."))
        await set_cfg(ctx.guild.id, **{"antinuke.punishment": action}); await ctx.send(embed=ok_embed(ctx.author, f"punishment set to `{action}`."))

    @antinuke.command(name="status")
    async def antinuke_status(self, ctx):
        an = (await get_cfg(ctx.guild.id)).get("antinuke", {})
        await ctx.send(embed=_antinuke_embed(ctx.author, ctx.guild, an))

    @antinuke.command(name="config", aliases=["settings", "view"])
    async def antinuke_config(self, ctx):
        an = (await get_cfg(ctx.guild.id)).get("antinuke", {})
        await ctx.send(embed=_antinuke_embed(ctx.author, ctx.guild, an))

    async def _module_toggle(self, ctx, module, setting):
        on = opt_on(setting)
        await set_cfg(ctx.guild.id, **{f"antinuke.modules.{module}": on})
        await ctx.send(embed=ok_embed(ctx.author, f"anti-nuke `{module}` {'enabled' if on else 'disabled'}."))

    @antinuke.command(name="botadd", help="Punish anyone who adds a bot.")
    @require_perms(administrator=True)
    async def antinuke_botadd(self, ctx, setting: str): await self._module_toggle(ctx, "botadd", setting)

    @antinuke.command(name="channelcreate", help="Protect against mass channel creation.")
    @require_perms(administrator=True)
    async def antinuke_chcreate(self, ctx, setting: str): await self._module_toggle(ctx, "channelcreate", setting)

    @antinuke.command(name="channeldelete", help="Protect against mass channel deletion.")
    @require_perms(administrator=True)
    async def antinuke_chdelete(self, ctx, setting: str): await self._module_toggle(ctx, "channeldelete", setting)

    @antinuke.command(name="rolecreate", help="Protect against mass role creation.")
    @require_perms(administrator=True)
    async def antinuke_rolecreate(self, ctx, setting: str): await self._module_toggle(ctx, "rolecreate", setting)

    @antinuke.command(name="roledelete", help="Protect against mass role deletion.")
    @require_perms(administrator=True)
    async def antinuke_roledelete(self, ctx, setting: str): await self._module_toggle(ctx, "roledelete", setting)

    @antinuke.command(name="webhook", help="Punish unauthorized webhook creation.")
    @require_perms(administrator=True)
    async def antinuke_webhook(self, ctx, setting: str): await self._module_toggle(ctx, "webhook", setting)

    @antinuke.command(name="admin", help="Punish unauthorized administrator grants.")
    @require_perms(administrator=True)
    async def antinuke_admin(self, ctx, setting: str): await self._module_toggle(ctx, "admin", setting)

    async def _module_on(self, guild, module):
        an = (await get_cfg(guild.id)).get("antinuke", {})
        if not an.get("enabled"):
            return False
        return an.get("modules", {}).get(module, True)

    async def _antinuke_punish(self, guild, user_id, action_name):
        cfg = await get_cfg(guild.id); an = cfg.get("antinuke", {})
        if not an.get("enabled"):
            return
        if str(user_id) in an.get("whitelist", []) or user_id == guild.owner_id or user_id == self.bot.user.id:
            return
        member = guild.get_member(user_id)
        if not member:
            return
        punishment = an.get("punishment", "ban")
        try:
            if punishment == "ban":
                await member.ban(reason=f"Anti-Nuke: {action_name}")
            elif punishment == "kick":
                await member.kick(reason=f"Anti-Nuke: {action_name}")
            else:
                await member.edit(roles=[], reason=f"Anti-Nuke: {action_name}")
        except discord.HTTPException:
            return
        ch_id = cfg.get("modlog")
        if ch_id and (ch := guild.get_channel(int(ch_id))):
            try: await ch.send(embed=info_embed("🛡️ Anti-Nuke", f"**{member}** was **{punishment}**ed for: {action_name}"))
            except discord.HTTPException: pass

    async def _audit_actor(self, guild, action):
        try:
            async for entry in guild.audit_logs(limit=3, action=action):
                return entry.user.id
        except (discord.Forbidden, discord.HTTPException):
            return None

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        if await self._module_on(channel.guild, "channeldelete"):
            actor = await self._audit_actor(channel.guild, discord.AuditLogAction.channel_delete)
            if actor:
                await self._antinuke_punish(channel.guild, actor, "channel deletion")

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel):
        if await self._module_on(channel.guild, "channelcreate"):
            actor = await self._audit_actor(channel.guild, discord.AuditLogAction.channel_create)
            if actor:
                await self._antinuke_punish(channel.guild, actor, "channel creation")

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role):
        if await self._module_on(role.guild, "roledelete"):
            actor = await self._audit_actor(role.guild, discord.AuditLogAction.role_delete)
            if actor:
                await self._antinuke_punish(role.guild, actor, "role deletion")

    @commands.Cog.listener()
    async def on_guild_role_create(self, role):
        if await self._module_on(role.guild, "rolecreate"):
            actor = await self._audit_actor(role.guild, discord.AuditLogAction.role_create)
            if actor:
                await self._antinuke_punish(role.guild, actor, "role creation")

    @commands.Cog.listener()
    async def on_webhooks_update(self, channel):
        if await self._module_on(channel.guild, "webhook"):
            actor = await self._audit_actor(channel.guild, discord.AuditLogAction.webhook_create)
            if actor:
                await self._antinuke_punish(channel.guild, actor, "webhook creation")

    @commands.Cog.listener()
    async def on_member_ban(self, guild, user):
        if (await get_cfg(guild.id)).get("antinuke", {}).get("enabled"):
            actor = await self._audit_actor(guild, discord.AuditLogAction.ban)
            if actor:
                await self._antinuke_punish(guild, actor, "member ban")

    @commands.Cog.listener()
    async def on_guild_role_update(self, before, after):
        # admin-grant protection
        if not before.permissions.administrator and after.permissions.administrator:
            if await self._module_on(after.guild, "admin"):
                actor = await self._audit_actor(after.guild, discord.AuditLogAction.role_update)
                if actor:
                    await self._antinuke_punish(after.guild, actor, "granting administrator")


async def setup(bot):
    await bot.add_cog(Configuration(bot))

