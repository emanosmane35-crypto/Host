"""general_cog.py (3/5) — moderation, utility, fun, levels, tickets, voicemaster, music."""
import time
import random
import asyncio
import logging
from datetime import datetime, timezone, timedelta

log = logging.getLogger("general_cog")

import discord
from discord.ext import commands, tasks
import yt_dlp

from core import (require_perms, ok_embed, warn_embed, err_embed, base_embed, info_embed,
                  send_log, send_modlog, parse_duration, get_cfg, set_cfg, push_cfg, pull_cfg, db, BRAND, PREFIX,
                  START_TIME, COLOR, COLOR_OK, COLOR_ERR, COLOR_WARN, build_help_embed, send_help)


# ===================== MODERATION =====================
class ConfirmView(discord.ui.View):
    def __init__(self, author):
        super().__init__(timeout=30)
        self.author = author
        self.value = None

    async def interaction_check(self, interaction):
        if interaction.user.id != self.author.id:
            await interaction.response.send_message("This isn't your prompt.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success)
    async def confirm(self, interaction, button):
        self.value = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction, button):
        self.value = False
        await interaction.response.edit_message(content="Cancelled.", embed=None, view=None)
        self.stop()


async def _is_whitelisted_admin(ctx):
    """True only for the guild owner or an antinuke-whitelisted member."""
    if ctx.author == ctx.guild.owner:
        return True
    an = (await get_cfg(ctx.guild.id)).get("antinuke", {})
    return str(ctx.author.id) in an.get("whitelist", [])


class Moderation(commands.Cog):
    def __init__(self, bot): self.bot = bot

    @commands.command(help="Clone this channel and delete the original (wipes all messages)")
    @require_perms(manage_channels=True)
    @commands.bot_has_permissions(manage_channels=True)
    async def nuke(self, ctx):
        view = ConfirmView(ctx.author)
        e = discord.Embed(color=COLOR_WARN, description=f"⚠️ {ctx.author.mention}: Are you sure that you want to **nuke** this **channel**?")
        await ctx.send(embed=e, view=view)
        await view.wait()
        if not view.value:
            return
        channel = ctx.channel
        position = channel.position
        new = await channel.clone(reason=f"Nuke by {ctx.author}")
        try:
            await channel.delete(reason=f"Nuke by {ctx.author}")
        except discord.HTTPException:
            pass
        await new.edit(position=position)
        msg = await new.send(f"hey {ctx.author.mention}")
        try:
            await msg.add_reaction("💀")
        except discord.HTTPException:
            pass

    @commands.command(help="Ban a user from the server")
    @require_perms(ban_members=True)
    @commands.bot_has_permissions(ban_members=True)
    async def ban(self, ctx, member: discord.Member, *, reason="No reason provided"):
        if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
            return await ctx.send(embed=warn_embed(ctx.author, "you can't ban someone with an equal or higher role."))
        await member.ban(reason=f"{ctx.author}: {reason}")
        await ctx.send(embed=ok_embed(ctx.author, f"**banned** **{member}** — {reason}"))
        await send_modlog(ctx.guild, "ban", user=f"{member} ({member.id})",
                          moderator=f"{ctx.author} ({ctx.author.id})", reason=reason)

    @commands.command(help="Hard-ban (ban + delete 7d of messages). Whitelisted admins only.")
    @require_perms(ban_members=True)
    @commands.bot_has_permissions(ban_members=True)
    async def hardban(self, ctx, member: discord.Member, *, reason="No reason"):
        if not await _is_whitelisted_admin(ctx):
            return await ctx.send(embed=err_embed(ctx.author, "only a **whitelisted admin** can use **hardban**. Ask the owner to add you with `,antinuke whitelist @you`."))
        await member.ban(reason=f"Hardban {ctx.author}: {reason}", delete_message_days=7)
        await ctx.send(embed=ok_embed(ctx.author, f"**hard-banned** **{member}**."))

    @commands.command(help="Softban (ban+unban to purge messages).")
    @require_perms(ban_members=True)
    @commands.bot_has_permissions(ban_members=True)
    async def softban(self, ctx, member: discord.Member, *, reason="No reason"):
        await member.ban(reason=f"Softban {ctx.author}: {reason}", delete_message_days=7)
        await ctx.guild.unban(discord.Object(id=member.id))
        await ctx.send(embed=ok_embed(ctx.author, f"**soft-banned** **{member}**."))

    @commands.command(help="Kick a member from the server")
    @require_perms(kick_members=True)
    @commands.bot_has_permissions(kick_members=True)
    async def kick(self, ctx, member: discord.Member, *, reason="No reason provided"):
        if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
            return await ctx.send(embed=warn_embed(ctx.author, "you can't kick someone with an equal or higher role."))
        await member.kick(reason=f"{ctx.author}: {reason}")
        await ctx.send(embed=ok_embed(ctx.author, f"**kicked** **{member}** — {reason}"))
        await send_modlog(ctx.guild, "kick", user=f"{member} ({member.id})",
                          moderator=f"{ctx.author} ({ctx.author.id})", reason=reason)

    @commands.command(aliases=["timeout"], help="Timeout a member e.g. ,mute @u 10m.")
    @require_perms(moderate_members=True)
    @commands.bot_has_permissions(moderate_members=True)
    async def mute(self, ctx, member: discord.Member, duration: str, *, reason="No reason"):
        secs = parse_duration(duration)
        if secs <= 0 or secs > 2419200:
            return await ctx.send(embed=warn_embed(ctx.author, "invalid duration. Use s/m/h/d e.g. `30m`."))
        await member.timeout(timedelta(seconds=secs), reason=f"{ctx.author}: {reason}")
        await ctx.send(embed=ok_embed(ctx.author, f"**muted** **{member}** for **{duration}** — {reason}"))
        await send_modlog(ctx.guild, "mute", user=f"{member} ({member.id})",
                          moderator=f"{ctx.author} ({ctx.author.id})", reason=reason)

    @commands.command(aliases=["untimeout", "uto"], help="Remove a timeout.")
    @require_perms(moderate_members=True)
    @commands.bot_has_permissions(moderate_members=True)
    async def unmute(self, ctx, member: discord.Member):
        await member.timeout(None)
        await ctx.send(embed=ok_embed(ctx.author, f"**unmuted** **{member}**."))

    @commands.command(help="Temp-ban e.g. ,tempban @u 1d.")
    @require_perms(ban_members=True)
    @commands.bot_has_permissions(ban_members=True)
    async def tempban(self, ctx, member: discord.Member, duration: str, *, reason="No reason"):
        secs = parse_duration(duration)
        if secs <= 0:
            return await ctx.send(embed=warn_embed(ctx.author, "invalid duration."))
        await member.ban(reason=f"Tempban {ctx.author}: {reason}")
        await ctx.send(embed=ok_embed(ctx.author, f"tempbanned **{member}** for {duration}."))
        await asyncio.sleep(secs)
        try: await ctx.guild.unban(discord.Object(id=member.id), reason="Tempban expired")
        except discord.HTTPException: pass

    @commands.command(help="Unban a user by ID.")
    @require_perms(ban_members=True)
    @commands.bot_has_permissions(ban_members=True)
    async def unban(self, ctx, user_id: int):
        try:
            await ctx.guild.unban(discord.Object(id=user_id))
            await ctx.send(embed=ok_embed(ctx.author, f"unbanned <@{user_id}>."))
        except discord.NotFound:
            await ctx.send(embed=warn_embed(ctx.author, "that user is not banned."))

    @commands.command(help="Warn a member.")
    @require_perms(manage_messages=True)
    async def warn(self, ctx, member: discord.Member, *, reason="No reason"):
        await db.warnings.insert_one({"guild": str(ctx.guild.id), "user": str(member.id), "mod": str(ctx.author.id), "reason": reason, "time": datetime.now(timezone.utc).isoformat()})
        count = await db.warnings.count_documents({"guild": str(ctx.guild.id), "user": str(member.id)})
        await ctx.send(embed=ok_embed(ctx.author, f"warned **{member}** ({count} total) — {reason}"))
        await send_modlog(ctx.guild, "warn", user=f"{member} ({member.id})",
                          moderator=f"{ctx.author} ({ctx.author.id})", reason=reason)

    @commands.command(help="List warnings.")
    async def warnings(self, ctx, member: discord.Member = None):
        member = member or ctx.author
        docs = await db.warnings.find({"guild": str(ctx.guild.id), "user": str(member.id)}).to_list(25)
        if not docs:
            return await ctx.send(embed=base_embed(f"**{member}** has no warnings."))
        e = info_embed(f"Warnings — {member}")
        for i, d in enumerate(docs, 1):
            e.add_field(name=f"#{i}", value=d["reason"], inline=False)
        await ctx.send(embed=e)

    @commands.command(aliases=["clear"], help="Bulk delete messages.")
    @require_perms(manage_messages=True)
    @commands.bot_has_permissions(manage_messages=True)
    async def purge(self, ctx, count: int):
        count = max(1, min(count, 100))
        deleted = await ctx.channel.purge(limit=count + 1)
        await ctx.send(embed=ok_embed(ctx.author, f"deleted {len(deleted)-1} messages."), delete_after=4)

    @commands.command(aliases=["lock"], help="Lock a channel.")
    @require_perms(manage_channels=True)
    @commands.bot_has_permissions(manage_channels=True)
    async def lockdown(self, ctx, channel: discord.TextChannel = None):
        channel = channel or ctx.channel
        await channel.set_permissions(ctx.guild.default_role, send_messages=False)
        await ctx.send(embed=ok_embed(ctx.author, f"locked {channel.mention}."))

    @commands.command(help="Unlock a channel.")
    @require_perms(manage_channels=True)
    @commands.bot_has_permissions(manage_channels=True)
    async def unlock(self, ctx, channel: discord.TextChannel = None):
        channel = channel or ctx.channel
        await channel.set_permissions(ctx.guild.default_role, send_messages=None)
        await ctx.send(embed=ok_embed(ctx.author, f"unlocked {channel.mention}."))

    @commands.command(help="Set channel slowmode (seconds).")
    @require_perms(manage_channels=True)
    @commands.bot_has_permissions(manage_channels=True)
    async def slowmode(self, ctx, seconds: int, channel: discord.TextChannel = None):
        channel = channel or ctx.channel
        await channel.edit(slowmode_delay=max(0, min(seconds, 21600)))
        await ctx.send(embed=ok_embed(ctx.author, f"set slowmode to {seconds}s in {channel.mention}."))

    @commands.command(help="Change a member's nickname.")
    @require_perms(manage_nicknames=True)
    @commands.bot_has_permissions(manage_nicknames=True)
    async def nick(self, ctx, member: discord.Member, *, nickname=None):
        await member.edit(nick=nickname)
        await ctx.send(embed=ok_embed(ctx.author, f"changed nickname for **{member}**."))

    @commands.command(help="Add/remove a role: ,role @user @role")
    @require_perms(manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def role(self, ctx, member: discord.Member, *, rolename):
        r = ctx.message.role_mentions[0] if ctx.message.role_mentions else discord.utils.find(lambda x: x.name.lower() == rolename.lower(), ctx.guild.roles)
        if not r:
            return await ctx.send(embed=warn_embed(ctx.author, "role not found."))
        if r in member.roles:
            await member.remove_roles(r); await ctx.send(embed=ok_embed(ctx.author, f"removed **{r.name}** from {member.mention}."))
        else:
            await member.add_roles(r); await ctx.send(embed=ok_embed(ctx.author, f"added **{r.name}** to {member.mention}."))


# ===================== UTILITY + FUN =====================
snipes = {}


class Utility(commands.Cog):
    def __init__(self, bot): self.bot = bot

    @commands.Cog.listener()
    async def on_message_delete(self, message):
        if message.author.bot or not message.guild: return
        snipes[message.channel.id] = {"content": message.content, "author": str(message.author), "avatar": str(message.author.display_avatar.url)}

    @commands.command(aliases=["av"], help="Show a user's avatar.")
    async def avatar(self, ctx, member: discord.Member = None):
        member = member or ctx.author
        await ctx.send(embed=info_embed(f"{member}'s avatar", author=str(member), author_icon=member.display_avatar.url, image=member.display_avatar.url))

    @commands.command(help="Show a user's banner.")
    async def banner(self, ctx, member: discord.Member = None):
        member = member or ctx.author
        user = await self.bot.fetch_user(member.id)
        if not user.banner:
            return await ctx.send(embed=base_embed(f"**{member}** has no banner."))
        await ctx.send(embed=info_embed(f"{member}'s banner", image=user.banner.url))

    @commands.command(aliases=["ui", "whois"], help="User info.")
    async def userinfo(self, ctx, member: discord.Member = None):
        member = member or ctx.author
        e = info_embed(str(member), author=str(member), author_icon=member.display_avatar.url, thumbnail=member.display_avatar.url, fields=[
            ("ID", str(member.id), True),
            ("Joined", member.joined_at.strftime("%b %d, %Y") if member.joined_at else "?", True),
            ("Created", member.created_at.strftime("%b %d, %Y"), True),
            ("Top role", member.top_role.mention, True),
            ("Roles", str(len(member.roles) - 1), True),
            ("Bot", "Yes" if member.bot else "No", True),
        ])
        await ctx.send(embed=e)

    @commands.command(aliases=["si"], help="Show detailed server information.")
    async def serverinfo(self, ctx):
        g = ctx.guild
        created_ts = int(g.created_at.timestamp())
        bots = sum(1 for m in g.members if m.bot)
        humans = (g.member_count or 0) - bots
        shard = f"{g.shard_id or 0}/{self.bot.shard_count or 1}"
        link = lambda asset: f"[Click here]({asset.url})" if asset else "N/A"
        e = discord.Embed(color=COLOR,
            description=f"Server created on <t:{created_ts}:D> (<t:{created_ts}:R>)\n**{g.name}** is on bot shard ID: **{shard}**")
        e.title = g.name
        e.set_author(name=ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
        if g.icon:
            e.set_thumbnail(url=g.icon.url)
        e.add_field(name="Owner", value=str(g.owner), inline=True)
        e.add_field(name="Members", value=f"**Total**: {g.member_count}\n**Humans**: {humans}\n**Bots**: {bots}", inline=True)
        e.add_field(name="Information", value=f"**Verification**: {str(g.verification_level).title()}\n**Boosts**: {g.premium_subscription_count} (level {g.premium_tier})", inline=True)
        e.add_field(name="Design", value=f"**Splash**: {link(g.splash)}\n**Banner**: {link(g.banner)}\n**Icon**: {link(g.icon)}", inline=True)
        e.add_field(name=f"Channels ({len(g.channels)})", value=f"**Text**: {len(g.text_channels)}\n**Voice**: {len(g.voice_channels)}\n**Category**: {len(g.categories)}", inline=True)
        e.add_field(name="Counts", value=f"**Roles**: {len(g.roles)}/250\n**Emojis**: {len(g.emojis)}/{g.emoji_limit}\n**Boosters**: {len(g.premium_subscribers)}", inline=True)
        e.set_footer(text=f"Guild ID: {g.id}")
        e.timestamp = datetime.now(timezone.utc)
        await ctx.send(embed=e)

    @commands.command(help="Show the last deleted message.")
    async def snipe(self, ctx):
        s = snipes.get(ctx.channel.id)
        if not s: return await ctx.send(embed=base_embed("Nothing to snipe."))
        await ctx.send(embed=info_embed(None, description=s["content"] or "*no text*", author=s["author"], author_icon=s["avatar"]))

    @commands.command(help="Bot latency.")
    async def ping(self, ctx):
        await ctx.send(embed=base_embed(f"🏓 Pong! `{round(self.bot.latency*1000)}ms`"))

    @commands.command(aliases=["bi"], help="Bot info.")
    async def botinfo(self, ctx):
        up = int(time.time() - START_TIME)
        e = info_embed(BRAND, thumbnail=self.bot.user.display_avatar.url, fields=[
            ("Servers", str(len(self.bot.guilds)), True),
            ("Users", str(sum(g.member_count or 0 for g in self.bot.guilds)), True),
            ("Latency", f"{round(self.bot.latency*1000)}ms", True),
            ("Uptime", f"{up//3600}h {(up%3600)//60}m", True),
        ])
        await ctx.send(embed=e)

    @commands.command(help="Bot invite link.")
    async def invite(self, ctx):
        url = f"https://discord.com/oauth2/authorize?client_id={self.bot.user.id}&permissions=8&scope=bot%20applications.commands"
        await ctx.send(embed=info_embed("Invite", f"[Invite {BRAND} to your server]({url})"))

    @commands.command(help="Look up a user by ID.")
    async def lookup(self, ctx, user_id: int):
        try: user = await self.bot.fetch_user(user_id)
        except discord.NotFound: return await ctx.send(embed=warn_embed(ctx.author, "user not found."))
        await ctx.send(embed=info_embed(str(user), thumbnail=user.display_avatar.url, fields=[("ID", str(user.id), True), ("Created", user.created_at.strftime("%b %d, %Y"), True)]))

    @commands.command(help="Link to the first message in a channel.")
    async def firstmessage(self, ctx, channel: discord.TextChannel = None):
        channel = channel or ctx.channel
        async for m in channel.history(limit=1, oldest_first=True):
            return await ctx.send(embed=info_embed("First Message", f"[Jump to message]({m.jump_url})"))

    @commands.command(help="Pin the most recent message or by URL.")
    @require_perms(manage_messages=True)
    async def pin(self, ctx, message: discord.Message = None):
        if not message:
            msgs = [m async for m in ctx.channel.history(limit=2)]
            message = msgs[-1] if msgs else None
        if message: await message.pin()
        await ctx.send(embed=ok_embed(ctx.author, "pinned the message."))

    @commands.command(help="Unpin a message by URL.")
    @require_perms(manage_messages=True)
    async def unpin(self, ctx, message: discord.Message):
        await message.unpin()
        await ctx.send(embed=ok_embed(ctx.author, "unpinned the message."))

    # ---- fun ----
    @commands.command(name="8ball", help="Ask the magic 8-ball.")
    async def eightball(self, ctx, *, question):
        await ctx.send(embed=base_embed(f"🎱 {random.choice(['Yes.','No.','Maybe.','Definitely.','Absolutely not.','Ask again later.','It is certain.','Doubtful.'])}"))

    @commands.command(aliases=["cf"], help="Flip a coin.")
    async def coinflip(self, ctx):
        await ctx.send(embed=base_embed(f"🪙 {random.choice(['Heads','Tails'])}!"))

    @commands.command(help="Ship two users.")
    async def ship(self, ctx, a: discord.Member, b: discord.Member):
        await ctx.send(embed=base_embed(f"💞 **{a.display_name}** + **{b.display_name}** = **{random.randint(0,100)}%**"))

    @commands.command(help="Make the bot say something.")
    @require_perms(manage_messages=True)
    async def say(self, ctx, *, message):
        try: await ctx.message.delete()
        except discord.HTTPException: pass
        await ctx.send(message)


# ===================== LEVELS =====================
xp_cd = {}


class Levels(commands.Cog):
    def __init__(self, bot): self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or not message.guild: return
        key = (message.guild.id, message.author.id); now = time.time()
        if now - xp_cd.get(key, 0) < 30: return
        xp_cd[key] = now
        doc = await db.levels.find_one({"guild": str(message.guild.id), "user": str(message.author.id)})
        xp = (doc["xp"] if doc else 0) + random.randint(15, 25)
        level = int((xp / 100) ** 0.5); old = doc["level"] if doc else 0
        await db.levels.update_one({"guild": str(message.guild.id), "user": str(message.author.id)}, {"$set": {"xp": xp, "level": level, "name": str(message.author)}}, upsert=True)
        if level > old:
            rid = (await get_cfg(message.guild.id)).get("levelroles", {}).get(str(level))
            if rid and (r := message.guild.get_role(int(rid))):
                try: await message.author.add_roles(r, reason="Level role")
                except discord.HTTPException: pass

    @commands.command(aliases=["level"], help="Show rank and XP.")
    async def rank(self, ctx, member: discord.Member = None):
        member = member or ctx.author
        doc = await db.levels.find_one({"guild": str(ctx.guild.id), "user": str(member.id)})
        if not doc: return await ctx.send(embed=base_embed(f"**{member}** has no XP yet."))
        await ctx.send(embed=info_embed(f"Rank — {member}", thumbnail=member.display_avatar.url, fields=[("Level", str(doc["level"]), True), ("XP", str(doc["xp"]), True)]))

    @commands.command(aliases=["lb", "top"], help="XP leaderboard.")
    async def leaderboard(self, ctx):
        docs = await db.levels.find({"guild": str(ctx.guild.id)}).sort("xp", -1).to_list(10)
        if not docs: return await ctx.send(embed=base_embed("No one has XP yet."))
        await ctx.send(embed=info_embed("🏆 Leaderboard", "\n".join(f"**{i}.** {d.get('name','?')} — Level {d['level']} ({d['xp']} XP)" for i, d in enumerate(docs, 1))))

    @commands.command(help="Set a level-up role.")
    @require_perms(manage_roles=True)
    async def levelroles(self, ctx, level: int, role: discord.Role):
        await set_cfg(ctx.guild.id, **{f"levelroles.{level}": str(role.id)})
        await ctx.send(embed=ok_embed(ctx.author, f"level {level} → **{role.name}**."))


# ===================== TICKETS =====================
class TicketButton(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)

    @discord.ui.button(label="Open Ticket", style=discord.ButtonStyle.secondary, emoji="🎫", custom_id="canary_open_ticket")
    async def open_ticket(self, interaction, button):
        g = interaction.guild
        if discord.utils.get(g.text_channels, name=f"ticket-{interaction.user.id}"):
            return await interaction.response.send_message("You already have an open ticket.", ephemeral=True)
        ov = {g.default_role: discord.PermissionOverwrite(read_messages=False),
              interaction.user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
              g.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)}
        ch = await g.create_text_channel(f"ticket-{interaction.user.id}", overwrites=ov)
        await ch.send(embed=info_embed("🎫 Ticket", f"{interaction.user.mention} staff will be with you shortly.\nUse `{PREFIX}ticket close` to close."))
        await interaction.response.send_message(f"Ticket created: {ch.mention}", ephemeral=True)


class Tickets(commands.Cog):
    def __init__(self, bot): self.bot = bot

    @commands.group(invoke_without_command=True, help="Ticket system.")
    async def ticket(self, ctx):
        await send_help(ctx, ctx.command, ctx.author)

    @ticket.command(name="setup")
    @require_perms(manage_guild=True)
    async def ticket_setup(self, ctx):
        await ctx.send(embed=info_embed("🎫 Support Tickets", "Click the button below to open a support ticket."), view=TicketButton())

    @ticket.command(name="close")
    async def ticket_close(self, ctx):
        if ctx.channel.name.startswith("ticket-"):
            await ctx.send(embed=base_embed("Closing in 5 seconds...", color=COLOR_WARN)); await asyncio.sleep(5); await ctx.channel.delete()
        else:
            await ctx.send(embed=warn_embed(ctx.author, "this isn't a ticket channel."))

    @ticket.command(name="add")
    async def ticket_add(self, ctx, member: discord.Member):
        if not ctx.channel.name.startswith("ticket-"):
            return await ctx.send(embed=warn_embed(ctx.author, "this isn't a ticket channel."))
        await ctx.channel.set_permissions(member, read_messages=True, send_messages=True)
        await ctx.send(embed=ok_embed(ctx.author, f"**added** {member.mention} to the **ticket.**"))


# ===================== VOICEMASTER =====================
# Global bot application emojis (uploaded in the Discord Developer Portal -> your app -> Emojis).
# They are identical in every server. The bot NEVER uploads per-guild emojis anymore.
APP_EMOJIS = {}


async def load_app_emojis(bot):
    """Load the bot's global application emojis (Developer Portal) into APP_EMOJIS, keyed by name."""
    try:
        emojis = await bot.fetch_application_emojis()
        APP_EMOJIS.clear()
        for e in emojis:
            APP_EMOJIS[e.name] = e
        from core import STATE
        STATE["emojis"] = APP_EMOJIS
        log.info(f"loaded {len(APP_EMOJIS)} application emojis: {', '.join(APP_EMOJIS) or 'none'}")
    except Exception as e:
        log.warning(f"load_app_emojis failed: {e}")
    return APP_EMOJIS


# (action, emoji_name, unicode_fallback, label, description, row, style)
VM_BUTTONS = [
    ("lock", "vmlock", "🔒", "Lock", "the voice channel", 0, "secondary"),
    ("unlock", "vmunlock", "🔓", "Unlock", "the voice channel", 0, "secondary"),
    ("ghost", "vmghost", "👻", "Ghost", "the voice channel", 0, "secondary"),
    ("reveal", "vmreveal", "🔍", "Reveal", "the voice channel", 0, "secondary"),
    ("claim", "vmclaim", "👑", "Claim", "the voice channel", 0, "secondary"),
    ("info", "vminfo", "📋", "View channel information", "", 1, "secondary"),
    ("inc", "vminc", "➕", "Increase", "the user limit", 1, "secondary"),
    ("dec", "vmdec", "➖", "Decrease", "the user limit", 1, "secondary"),
    ("rename", "vmrename", "✏️", "Rename", "", 1, "secondary"),
    ("delete", "vmdelete", "🗑", "Delete", "", 1, "secondary"),
]


async def ensure_vm_emojis(guild):
    """Deprecated: bot no longer uploads per-guild emojis. Returns the global app emojis."""
    return APP_EMOJIS


def _em(emojis, name):
    src = emojis or APP_EMOJIS
    return src.get(name) if src else None


def interface_embed(emojis=None, guild=None, bot_member=None):
    e = discord.Embed(color=COLOR, title="VoiceMaster Interface",
                      description="Manage your voice channel by using the buttons below.")
    if guild:
        e.set_author(name=guild.name, icon_url=guild.icon.url if guild.icon else None)
    if bot_member:
        e.set_thumbnail(url=bot_member.display_avatar.url)
    lines = []
    for action, name, uni, label, desc, row, style in VM_BUTTONS:
        glyph = str(_em(emojis, name) or uni)
        lines.append(f"{glyph} — `{label}`" + (f" {desc}" if desc else ""))
    e.add_field(name="Button Usage", value="\n".join(lines), inline=False)
    return e


async def _resolve_vc(interaction, *, require_owner=True):
    """Returns (vc, doc) or (None, None) after replying with an error."""
    member = interaction.user
    if not (await get_cfg(interaction.guild.id)).get("vm_hub"):
        await interaction.response.send_message(embed=err_embed(member, "**Voicemaster interface** hasn't been **set up**.\nIf you think this is a mistake, run `voicemaster setup` to setup the interface."), ephemeral=True)
        return None, None
    vc = member.voice.channel if member.voice else None
    if not vc:
        await interaction.response.send_message(embed=warn_embed(member, "You're not connected to a **voice channel**."), ephemeral=True)
        return None, None
    doc = await db.temp_vc.find_one({"channel": str(vc.id)})
    if not doc:
        await interaction.response.send_message(embed=warn_embed(member, "You're not in a **VoiceMaster channel**."), ephemeral=True)
        return None, None
    if require_owner and str(doc.get("owner")) != str(member.id):
        await interaction.response.send_message(embed=warn_embed(member, "You don't **own** this channel."), ephemeral=True)
        return None, None
    return vc, doc


def vm_embed(text):
    return discord.Embed(color=COLOR, description=text)


class RenameModal(discord.ui.Modal, title="Rename Channel"):
    new_name = discord.ui.TextInput(label="New name", max_length=100, placeholder="my channel")

    def __init__(self, vc):
        super().__init__()
        self.vc = vc

    async def on_submit(self, interaction):
        await self.vc.edit(name=str(self.new_name))
        await interaction.response.send_message(embed=vm_embed(f"✏️ Renamed to **{self.new_name}**."), ephemeral=True)


async def _resolve_vc_ctx(ctx, require_owner=True):
    vc = ctx.author.voice.channel if ctx.author.voice else None
    doc = await db.temp_vc.find_one({"channel": str(vc.id)}) if vc else None
    if not vc or not doc:
        await ctx.send(embed=warn_embed(ctx.author, "You're not in a **VoiceMaster channel**."))
        return None, None
    if require_owner and str(doc.get("owner")) != str(ctx.author.id):
        await ctx.send(embed=warn_embed(ctx.author, "You don't **own** this channel."))
        return None, None
    return vc, doc


async def _vm_apply(action, guild, member, vc, doc):
    """Core VoiceMaster action — performs it and returns the result text."""
    if action == "lock":
        await vc.set_permissions(guild.default_role, connect=False)
        return "🔒 Channel **locked**."
    if action == "unlock":
        await vc.set_permissions(guild.default_role, connect=None)
        return "🔓 Channel **unlocked**."
    if action == "ghost":
        await vc.set_permissions(guild.default_role, view_channel=False)
        return "👻 Channel **hidden**."
    if action == "reveal":
        await vc.set_permissions(guild.default_role, view_channel=None)
        return "🔍 Channel **revealed**."
    if action == "claim":
        owner_id = int(doc.get("owner")) if doc.get("owner") else None
        if owner_id and any(m.id == owner_id for m in vc.members):
            return "⚠️ The owner is still in the channel."
        await db.temp_vc.update_one({"channel": str(vc.id)}, {"$set": {"owner": str(member.id)}})
        await vc.set_permissions(member, manage_channels=True, move_members=True)
        return "👑 You now **own** this channel."
    if action == "inc":
        await vc.edit(user_limit=min((vc.user_limit or 0) + 1, 99))
        return f"➕ User limit set to **{vc.user_limit}**."
    if action == "dec":
        await vc.edit(user_limit=max((vc.user_limit or 0) - 1, 0))
        return f"➖ User limit set to **{vc.user_limit or 'unlimited'}**."
    if action == "delete":
        await db.temp_vc.delete_one({"channel": str(vc.id)})
        try: await vc.delete()
        except discord.HTTPException: pass
        return "🗑 Channel **deleted**."
    return "Unknown action."


def _vm_info_embed(guild, vc, doc):
    owner = guild.get_member(int(doc.get("owner"))) if doc.get("owner") else None
    locked = vc.overwrites_for(guild.default_role).connect is False
    e = discord.Embed(color=COLOR, title=vc.name)
    e.add_field(name="Owner", value=owner.mention if owner else "unclaimed", inline=True)
    e.add_field(name="Members", value=f"{len(vc.members)}", inline=True)
    e.add_field(name="Limit", value=str(vc.user_limit or "unlimited"), inline=True)
    e.add_field(name="Locked", value="yes" if locked else "no", inline=True)
    e.add_field(name="Bitrate", value=f"{vc.bitrate // 1000}kbps", inline=True)
    e.add_field(name="Created", value=discord.utils.format_dt(vc.created_at, "R"), inline=True)
    return e


def _vm_button_action(action):
    async def handler(interaction):
        vc, doc = await _resolve_vc(interaction, require_owner=action not in ("claim", "info"))
        if not vc:
            return
        if action == "rename":
            return await interaction.response.send_modal(RenameModal(vc))
        if action == "info":
            return await interaction.response.send_message(embed=_vm_info_embed(interaction.guild, vc, doc), ephemeral=True)
        text = await _vm_apply(action, interaction.guild, interaction.user, vc, doc)
        await interaction.response.send_message(embed=vm_embed(text), ephemeral=True)
    return handler


VM_ACTIONS = {a: _vm_button_action(a) for a in
              ("lock", "unlock", "ghost", "reveal", "claim", "inc", "dec", "rename", "info", "delete")}


class VMButton(discord.ui.Button):
    def __init__(self, action, emoji, style, row):
        super().__init__(style=style, emoji=emoji, row=row, custom_id=f"vm:{action}")
        self.action = action

    async def callback(self, interaction):
        await VM_ACTIONS[self.action](interaction)


class VoiceInterface(discord.ui.View):
    def __init__(self, emojis=None):
        super().__init__(timeout=None)
        styles = {"secondary": discord.ButtonStyle.secondary, "danger": discord.ButtonStyle.danger}
        for action, name, uni, label, desc, row, style in VM_BUTTONS:
            emoji = _em(emojis, name) or uni
            self.add_item(VMButton(action, emoji, styles[style], row))


class VoiceMaster(commands.Cog):
    def __init__(self, bot): self.bot = bot

    @commands.group(invoke_without_command=True, aliases=["vm", "vc"], help="Temporary join-to-create voice channels.")
    async def voicemaster(self, ctx):
        await send_help(ctx, ctx.command, ctx.author)

    @voicemaster.command(name="setup", help="Create the J2C lobby, interface and channels category.")
    @require_perms(manage_channels=True)
    @commands.bot_has_permissions(manage_channels=True)
    async def vm_setup(self, ctx):
        cfg0 = await get_cfg(ctx.guild.id)
        if cfg0.get("vm_hub") and ctx.guild.get_channel(int(cfg0["vm_hub"])):
            return await ctx.send(embed=warn_embed(ctx.author, "**VoiceMaster** is **already set up**. Run `voicemaster reset` first to rebuild it."))
        emojis = APP_EMOJIS
        j2c_cat = await ctx.guild.create_category("VoiceMaster")
        vc_cat = await ctx.guild.create_category("Voice Channels")
        hub = await ctx.guild.create_voice_channel("j2c", category=j2c_cat)
        interface = await ctx.guild.create_text_channel("menu", category=j2c_cat)
        await interface.send(embed=interface_embed(emojis, ctx.guild, ctx.guild.me), view=VoiceInterface(emojis))
        await set_cfg(ctx.guild.id, vm_hub=str(hub.id), vm_j2c_category=str(j2c_cat.id),
                      vm_vc_category=str(vc_cat.id), vm_interface=str(interface.id))
        await ctx.send(embed=ok_embed(ctx.author, f"**Voicemaster Menu** has been **setup**"))

    @voicemaster.command(name="reset", aliases=["disable"], help="Remove the VoiceMaster setup (categories, channels, interface).")
    @require_perms(manage_channels=True)
    async def vm_reset(self, ctx):
        cfg = await get_cfg(ctx.guild.id)
        vc_cat = ctx.guild.get_channel(int(cfg["vm_vc_category"])) if cfg.get("vm_vc_category") else None
        if vc_cat:
            for ch in list(vc_cat.channels):
                await db.temp_vc.delete_one({"channel": str(ch.id)})
                try: await ch.delete()
                except discord.HTTPException: pass
        for key in ("vm_interface", "vm_hub", "vm_j2c_category", "vm_vc_category"):
            cid = cfg.get(key)
            if cid:
                ch = ctx.guild.get_channel(int(cid))
                if ch:
                    try: await ch.delete()
                    except discord.HTTPException: pass
        await set_cfg(ctx.guild.id, vm_hub=None, vm_j2c_category=None,
                      vm_vc_category=None, vm_interface=None)
        await ctx.send(embed=ok_embed(ctx.author, "VoiceMaster has been reset."))

    @voicemaster.command(name="lock", help="Lock your voice channel.")
    async def vm_lock(self, ctx):
        vc = await self._owned(ctx)
        if not vc: return
        await vc.set_permissions(ctx.guild.default_role, connect=False)
        await ctx.send(embed=ok_embed(ctx.author, "channel locked."))

    @voicemaster.command(name="unlock", help="Unlock your voice channel.")
    async def vm_unlock(self, ctx):
        vc = await self._owned(ctx)
        if not vc: return
        await vc.set_permissions(ctx.guild.default_role, connect=None)
        await ctx.send(embed=ok_embed(ctx.author, "channel unlocked."))

    @voicemaster.command(name="ghost", help="Hide your voice channel.")
    async def vm_ghost(self, ctx):
        vc = await self._owned(ctx)
        if not vc: return
        await vc.set_permissions(ctx.guild.default_role, view_channel=False)
        await ctx.send(embed=ok_embed(ctx.author, "channel hidden."))

    @voicemaster.command(name="reveal", help="Reveal your voice channel.")
    async def vm_reveal(self, ctx):
        vc = await self._owned(ctx)
        if not vc: return
        await vc.set_permissions(ctx.guild.default_role, view_channel=None)
        await ctx.send(embed=ok_embed(ctx.author, "channel revealed."))

    @voicemaster.command(name="name", aliases=["rename"], help="Rename your voice channel.")
    async def vm_name(self, ctx, *, name: str):
        vc = await self._owned(ctx)
        if not vc: return
        await vc.edit(name=name[:100])
        await ctx.send(embed=ok_embed(ctx.author, f"renamed to **{name[:100]}**."))

    @voicemaster.command(name="limit", help="Set the user limit.")
    async def vm_limit(self, ctx, number: int):
        vc = await self._owned(ctx)
        if not vc: return
        await vc.edit(user_limit=max(0, min(number, 99)))
        await ctx.send(embed=ok_embed(ctx.author, f"user limit set to {max(0, min(number, 99))}."))

    @voicemaster.command(name="permit", aliases=["allow"], help="Allow a member to join.")
    async def vm_permit(self, ctx, member: discord.Member):
        vc = await self._owned(ctx)
        if not vc: return
        await vc.set_permissions(member, connect=True, view_channel=True)
        await ctx.send(embed=ok_embed(ctx.author, f"permitted **{member}**."))

    @voicemaster.command(name="reject", aliases=["deny", "kick"], help="Reject/remove a member.")
    async def vm_reject(self, ctx, member: discord.Member):
        vc = await self._owned(ctx)
        if not vc: return
        await vc.set_permissions(member, connect=False)
        if member.voice and member.voice.channel and member.voice.channel.id == vc.id:
            try: await member.move_to(None)
            except discord.HTTPException: pass
        await ctx.send(embed=ok_embed(ctx.author, f"rejected **{member}**."))

    @voicemaster.command(name="info", aliases=["information"], help="View channel information.")
    async def vm_info(self, ctx):
        vc = ctx.author.voice.channel if ctx.author.voice else None
        doc = await db.temp_vc.find_one({"channel": str(vc.id)}) if vc else None
        if not vc or not doc:
            return await ctx.send(embed=warn_embed(ctx.author, "you're not in a VoiceMaster channel."))
        await ctx.send(embed=_vm_info_embed(ctx.guild, vc, doc))

    @voicemaster.command(name="claim", help="Claim an ownerless channel.")
    async def vm_claim(self, ctx):
        vc = ctx.author.voice.channel if ctx.author.voice else None
        doc = await db.temp_vc.find_one({"channel": str(vc.id)}) if vc else None
        if not vc or not doc:
            return await ctx.send(embed=warn_embed(ctx.author, "you're not in a VoiceMaster channel."))
        owner_id = int(doc.get("owner")) if doc.get("owner") else None
        if owner_id and any(m.id == owner_id for m in vc.members):
            return await ctx.send(embed=warn_embed(ctx.author, "the owner is still in the channel."))
        await db.temp_vc.update_one({"channel": str(vc.id)}, {"$set": {"owner": str(ctx.author.id)}})
        await vc.set_permissions(ctx.author, manage_channels=True, move_members=True)
        await ctx.send(embed=ok_embed(ctx.author, "you now own this channel."))

    @voicemaster.command(name="delete", help="Delete your voice channel.")
    async def vm_delete(self, ctx):
        vc = await self._owned(ctx)
        if not vc: return
        await db.temp_vc.delete_one({"channel": str(vc.id)})
        await ctx.send(embed=ok_embed(ctx.author, "channel deleted."))
        try: await vc.delete()
        except discord.HTTPException: pass

    async def _owned(self, ctx):
        if not (await get_cfg(ctx.guild.id)).get("vm_hub"):
            await ctx.send(embed=err_embed(ctx.author, "**Voicemaster interface** hasn't been **set up**.\nIf you think this is a mistake, run `voicemaster setup` to setup the interface."))
            return None
        vc = ctx.author.voice.channel if ctx.author.voice else None
        doc = await db.temp_vc.find_one({"channel": str(vc.id)}) if vc else None
        if not vc or not doc:
            await ctx.send(embed=warn_embed(ctx.author, "You're not in your **VoiceMaster channel**."))
            return None
        if str(doc.get("owner")) != str(ctx.author.id):
            await ctx.send(embed=warn_embed(ctx.author, "You don't **own** this channel."))
            return None
        return vc

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        cfg = await get_cfg(member.guild.id); hub = cfg.get("vm_hub")
        if not hub: return
        if after.channel and str(after.channel.id) == hub:
            vc_cat = member.guild.get_channel(int(cfg["vm_vc_category"])) if cfg.get("vm_vc_category") else None
            overwrites = {member: discord.PermissionOverwrite(manage_channels=True, move_members=True, connect=True)}
            temp = await member.guild.create_voice_channel(f"{member.display_name}'s channel",
                                                           category=vc_cat, overwrites=overwrites)
            await member.move_to(temp)
            await db.temp_vc.insert_one({"channel": str(temp.id), "owner": str(member.id)})
        if before.channel:
            doc = await db.temp_vc.find_one({"channel": str(before.channel.id)})
            if doc and len(before.channel.members) == 0:
                try: await before.channel.delete()
                except discord.HTTPException: pass
                await db.temp_vc.delete_one({"channel": str(before.channel.id)})


# ===================== MUSIC =====================
YTDL = yt_dlp.YoutubeDL({"format": "bestaudio/best", "noplaylist": True, "quiet": True, "no_warnings": True, "default_search": "ytsearch", "source_address": "0.0.0.0"})
FFMPEG = {"before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5", "options": "-vn"}
queues, loops, current = {}, {}, {}


async def _spotify_query(url):
    """Resolve a Spotify track/playlist link to a searchable title via public oEmbed (no API key)."""
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get("https://open.spotify.com/oembed", params={"url": url}, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    j = await r.json()
                    return j.get("title")
    except Exception:
        pass
    return None


async def fetch_track(query):
    query = query.strip()
    if "open.spotify.com" in query:
        title = await _spotify_query(query)
        if not title:
            raise ValueError("spotify resolve failed")
        query = f"ytsearch1:{title}"
    data = await asyncio.get_event_loop().run_in_executor(None, lambda: YTDL.extract_info(query, download=False))
    if "entries" in data:
        data = data["entries"][0]
    return {"title": data.get("title", "Unknown"), "url": data["url"]}


class Music(commands.Cog):
    def __init__(self, bot): self.bot = bot

    def play_next(self, ctx):
        gid = ctx.guild.id; q = queues.get(gid, []); vc = ctx.guild.voice_client
        if not vc: return
        if loops.get(gid) and current.get(gid): q.insert(0, current[gid])
        if q:
            t = q.pop(0); current[gid] = t
            try:
                source = discord.FFmpegPCMAudio(t["url"], **FFMPEG)
                vc.play(source, after=lambda e: self.play_next(ctx))
                asyncio.run_coroutine_threadsafe(ctx.send(embed=info_embed("Now Playing", f"🎵 **{t['title']}**")), self.bot.loop)
            except Exception as e:
                asyncio.run_coroutine_threadsafe(
                    ctx.send(embed=err_embed(ctx.author, f"playback failed: `{type(e).__name__}`. I'll stay connected.")),
                    self.bot.loop)
        else:
            current.pop(gid, None)

    @commands.command(aliases=["j"], help="Join your voice channel.")
    async def join(self, ctx):
        if not ctx.author.voice: return await ctx.send(embed=warn_embed(ctx.author, "join a voice channel first."))
        ch = ctx.author.voice.channel
        await (ctx.voice_client.move_to(ch) if ctx.voice_client else ch.connect())
        await ctx.send(embed=ok_embed(ctx.author, f"joined **{ch.name}**."))

    @commands.command(aliases=["p"], help="Play a song by name or URL.")
    async def play(self, ctx, *, query):
        if not ctx.author.voice: return await ctx.send(embed=warn_embed(ctx.author, "join a voice channel first."))
        if not ctx.voice_client:
            try: await ctx.author.voice.channel.connect()
            except Exception: return await ctx.send(embed=err_embed(ctx.author, "**couldn't connect to voice.**"))
        async with ctx.typing():
            try: t = await fetch_track(query)
            except Exception: return await ctx.send(embed=err_embed(ctx.author, "couldn't load that track."))
        queues.setdefault(ctx.guild.id, []).append(t)
        if not ctx.voice_client.is_playing(): self.play_next(ctx)
        else: await ctx.send(embed=base_embed(f"➕ Queued: **{t['title']}**"))

    @commands.command(aliases=["s"], help="Skip the current song.")
    async def skip(self, ctx):
        if ctx.voice_client and ctx.voice_client.is_playing(): ctx.voice_client.stop(); await ctx.send(embed=ok_embed(ctx.author, "skipped."))
        else: await ctx.send(embed=warn_embed(ctx.author, "nothing is playing."))

    @commands.command(aliases=["q"], help="Show the music queue.")
    async def queue(self, ctx):
        q = queues.get(ctx.guild.id, [])
        if not q: return await ctx.send(embed=base_embed("The queue is empty."))
        await ctx.send(embed=info_embed("🎶 Queue", "\n".join(f"**{i}.** {t['title']}" for i, t in enumerate(q[:10], 1))))

    @commands.command(help="Pause playback.")
    async def pause(self, ctx):
        if ctx.voice_client and ctx.voice_client.is_playing(): ctx.voice_client.pause(); await ctx.send(embed=ok_embed(ctx.author, "paused."))

    @commands.command(help="Resume playback.")
    async def resume(self, ctx):
        if ctx.voice_client and ctx.voice_client.is_paused(): ctx.voice_client.resume(); await ctx.send(embed=ok_embed(ctx.author, "resumed."))

    @commands.command(aliases=["np"], help="Now playing.")
    async def nowplaying(self, ctx):
        cur = current.get(ctx.guild.id)
        await ctx.send(embed=info_embed("Now Playing", f"🎵 **{cur['title']}**") if cur else base_embed("Nothing playing."))

    @commands.command(help="Toggle queue loop.")
    async def loop(self, ctx, mode: str = "toggle"):
        loops[ctx.guild.id] = not loops.get(ctx.guild.id, False)
        await ctx.send(embed=ok_embed(ctx.author, f"loop {'enabled' if loops[ctx.guild.id] else 'disabled'}."))

    @commands.command(aliases=["leave", "dc"], help="Stop and disconnect.")
    async def stop(self, ctx):
        if ctx.voice_client:
            queues[ctx.guild.id] = []; await ctx.voice_client.disconnect(); await ctx.send(embed=ok_embed(ctx.author, "stopped and left."))
        else: await ctx.send(embed=warn_embed(ctx.author, "I'm not in a voice channel."))


class Giveaway(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.check_giveaways.start()

    def cog_unload(self):
        self.check_giveaways.cancel()

    async def _update_entries(self, doc):
        """Refresh the live 'Entries' count on a VC giveaway embed."""
        guild = self.bot.get_guild(int(doc["guild"]))
        channel = guild.get_channel(int(doc["channel"])) if guild else None
        if not channel:
            return
        try:
            msg = await channel.fetch_message(int(doc["message"]))
        except discord.HTTPException:
            return
        fresh = await db.giveaways.find_one({"_id": doc["_id"]})
        count = len(fresh.get("entrants", [])) if fresh else 0
        if msg.embeds:
            e = msg.embeds[0]
            for i, f in enumerate(e.fields):
                if f.name == "Entries":
                    e.set_field_at(i, name="Entries", value=str(count), inline=False)
                    break
            try: await msg.edit(embed=e)
            except discord.HTTPException: pass

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        if not payload.guild_id or str(payload.emoji) != "🎉":
            return
        if payload.member and payload.member.bot:
            return
        doc = await db.giveaways.find_one({"message": str(payload.message_id), "ended": False, "vc_only": True})
        if not doc:
            return
        guild = self.bot.get_guild(payload.guild_id)
        member = payload.member or (guild.get_member(payload.user_id) if guild else None)
        if not member:
            return
        bl = (await get_cfg(payload.guild_id)).get("giveaway_blacklist", [])
        in_vc = bool(member.voice and member.voice.channel)
        if str(member.id) in bl or not in_vc:
            channel = guild.get_channel(payload.channel_id)
            if channel:
                try:
                    msg = await channel.fetch_message(payload.message_id)
                    await msg.remove_reaction(payload.emoji, member)
                    if not in_vc:
                        await member.send("🔇 You must be **in a voice channel** to enter that giveaway.")
                except discord.HTTPException:
                    pass
            return
        await db.giveaways.update_one({"_id": doc["_id"]}, {"$addToSet": {"entrants": str(member.id)}})
        await self._update_entries(doc)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload):
        if not payload.guild_id or str(payload.emoji) != "🎉":
            return
        doc = await db.giveaways.find_one({"message": str(payload.message_id), "ended": False, "vc_only": True})
        if not doc:
            return
        await db.giveaways.update_one({"_id": doc["_id"]}, {"$pull": {"entrants": str(payload.user_id)}})
        await self._update_entries(doc)

    @commands.group(invoke_without_command=True, aliases=["gw"], help="Host giveaways in your server.")
    async def giveaway(self, ctx):
        await send_help(ctx, ctx.command, ctx.author)

    @giveaway.command(name="start", help="Start a giveaway: ,giveaway start <duration> [winners] <prize>")
    @require_perms(manage_guild=True)
    async def gw_start(self, ctx, duration: str, *, prize: str):
        secs = parse_duration(duration)
        if not secs:
            return await ctx.send(embed=warn_embed(ctx.author, "invalid duration — try `10m`, `24h`, `2d`."))
        winners = 1
        parts = prize.split()
        if parts and parts[0].isdigit():
            winners = max(1, int(parts[0])); prize = " ".join(parts[1:]) or "Giveaway"
        end = datetime.now(timezone.utc) + timedelta(seconds=secs)
        e = discord.Embed(color=COLOR, title=prize,
            description=("React with 🎉 to enter the giveaway.\n\n"
                         f"**Ends:** {discord.utils.format_dt(end, 'R')} ({discord.utils.format_dt(end, 'f')})\n"
                         f"**Hosted by:** {ctx.author.mention}\n"
                         f"**Winners:** {winners}"))
        e.set_author(name=ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
        msg = await ctx.send(embed=e)
        await msg.add_reaction("🎉")
        await db.giveaways.insert_one({
            "message": str(msg.id), "channel": str(ctx.channel.id), "guild": str(ctx.guild.id),
            "prize": prize, "winners": winners, "host": str(ctx.author.id),
            "end": end.isoformat(), "ended": False,
        })

    @giveaway.command(name="vc", help="Start a VC-only giveaway (must be in a voice channel to enter): ,giveaway vc <duration> [winners] <prize>")
    @require_perms(manage_guild=True)
    async def gw_vc(self, ctx, duration: str, *, prize: str):
        secs = parse_duration(duration)
        if not secs:
            return await ctx.send(embed=warn_embed(ctx.author, "invalid duration — try `10m`, `24h`, `2d`."))
        winners = 1
        parts = prize.split()
        if parts and parts[0].isdigit():
            winners = max(1, int(parts[0])); prize = " ".join(parts[1:]) or "Giveaway"
        end = datetime.now(timezone.utc) + timedelta(seconds=secs)
        e = discord.Embed(color=COLOR, title=prize,
            description=("🔊 **Voice Giveaway** — you must be **in a voice channel** to enter.\n"
                         "React with 🎉 to enter.\n\n"
                         f"**Ends:** {discord.utils.format_dt(end, 'R')} ({discord.utils.format_dt(end, 'f')})\n"
                         f"**Hosted by:** {ctx.author.mention}\n"
                         f"**Winners:** {winners}"))
        e.add_field(name="Entries", value="0", inline=False)
        e.set_author(name=ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
        msg = await ctx.send(embed=e)
        await msg.add_reaction("🎉")
        await db.giveaways.insert_one({
            "message": str(msg.id), "channel": str(ctx.channel.id), "guild": str(ctx.guild.id),
            "prize": prize, "winners": winners, "host": str(ctx.author.id),
            "end": end.isoformat(), "ended": False, "vc_only": True, "entrants": [],
        })

    @giveaway.command(name="end", help="End a giveaway early by its message ID.")
    @require_perms(manage_guild=True)
    async def gw_end(self, ctx, message_id: str):
        doc = await db.giveaways.find_one({"message": message_id, "ended": False})
        if not doc:
            return await ctx.send(embed=warn_embed(ctx.author, "no active giveaway with that message ID."))
        await self._end_giveaway(doc)
        await ctx.send(embed=ok_embed(ctx.author, "giveaway ended."))

    @giveaway.command(name="reroll", help="Reroll the winners by message ID.")
    @require_perms(manage_guild=True)
    async def gw_reroll(self, ctx, message_id: str):
        doc = await db.giveaways.find_one({"message": message_id})
        if not doc:
            return await ctx.send(embed=warn_embed(ctx.author, "no giveaway with that message ID."))
        await self._end_giveaway(doc, reroll=True)

    @giveaway.group(name="blacklist", aliases=["bl"], invoke_without_command=True,
                    help="Block members from entering giveaways.")
    @require_perms(manage_guild=True)
    async def gw_blacklist(self, ctx):
        await send_help(ctx, ctx.command, ctx.author)

    @gw_blacklist.command(name="add", help="Blacklist a member from giveaways.")
    @require_perms(manage_guild=True)
    async def gw_bl_add(self, ctx, member: discord.Member):
        await push_cfg(ctx.guild.id, "giveaway_blacklist", str(member.id))
        await ctx.send(embed=ok_embed(ctx.author, f"**{member}** can no longer enter giveaways."))

    @gw_blacklist.command(name="remove", help="Remove a member from the giveaway blacklist.")
    @require_perms(manage_guild=True)
    async def gw_bl_remove(self, ctx, member: discord.Member):
        await pull_cfg(ctx.guild.id, "giveaway_blacklist", str(member.id))
        await ctx.send(embed=ok_embed(ctx.author, f"**{member}** can enter giveaways again."))

    @gw_blacklist.command(name="list", help="List blacklisted members.")
    @require_perms(manage_guild=True)
    async def gw_bl_list(self, ctx):
        bl = (await get_cfg(ctx.guild.id)).get("giveaway_blacklist", [])
        await ctx.send(embed=info_embed("Giveaway Blacklist",
                       "\n".join(f"<@{uid}>" for uid in bl) or "no blacklisted members."))

    @tasks.loop(seconds=30)
    async def check_giveaways(self):
        now = datetime.now(timezone.utc)
        async for doc in db.giveaways.find({"ended": False}):
            try:
                if now >= datetime.fromisoformat(doc["end"]):
                    await self._end_giveaway(doc)
            except Exception:
                pass

    @check_giveaways.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    async def _end_giveaway(self, doc, reroll=False):
        await db.giveaways.update_one({"_id": doc["_id"]}, {"$set": {"ended": True}})
        guild = self.bot.get_guild(int(doc["guild"]))
        channel = guild.get_channel(int(doc["channel"])) if guild else None
        if not channel:
            return
        try:
            msg = await channel.fetch_message(int(doc["message"]))
        except discord.HTTPException:
            return
        entrants = []
        bl = (await get_cfg(int(doc["guild"]))).get("giveaway_blacklist", [])
        if doc.get("vc_only"):
            for uid in doc.get("entrants", []):
                m = guild.get_member(int(uid))
                if m and not m.bot and uid not in bl:
                    entrants.append(m)
        else:
            for reaction in msg.reactions:
                if str(reaction.emoji) == "🎉":
                    async for u in reaction.users():
                        if not u.bot and str(u.id) not in bl:
                            entrants.append(u)
        if not entrants:
            return await channel.send(embed=info_embed(doc["prize"], "No valid entries — no winner could be drawn."))
        winners = random.sample(entrants, min(doc["winners"], len(entrants)))
        mention = ", ".join(w.mention for w in winners)
        verb = "rerolled" if reroll else "ended"
        await channel.send(f"🎉 Congratulations {mention}! You won **{doc['prize']}**!")
        if msg.embeds:
            e = msg.embeds[0]
            e.description = f"Giveaway {verb}.\n\n**Winners:** {mention}\n**Hosted by:** <@{doc['host']}>"
            try: await msg.edit(embed=e)
            except discord.HTTPException: pass


async def setup(bot):
    bot.add_view(TicketButton())
    bot.add_view(VoiceInterface())
    for cog in (Moderation, Utility, Levels, Tickets, VoiceMaster, Music, Giveaway):
        await bot.add_cog(cog(bot))
