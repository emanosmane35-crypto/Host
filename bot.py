"""bot.py (2/5) — entry point. Loads config/roles/general cogs, heartbeat,
error handler (amber 'missing permission' embed), help/setup directory."""
import time
import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks

import core
from core import (TOKEN, PREFIX, db, BRAND, COLOR, COLOR_WARN, START_TIME,
                  command_counter, base_embed, info_embed, warn_embed, err_embed, ok_embed, get_cfg, set_cfg,
                  build_help_embed, send_help)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - bot - %(levelname)s - %(message)s")
log = logging.getLogger("canary")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True
intents.voice_states = True

EXTENSIONS = ["config_cog", "roles_cog", "general_cog"]


async def get_prefix(bot, message):
    prefixes = [PREFIX]
    if message.guild:
        cfg = await db.guild_config.find_one({"_id": str(message.guild.id)})
        if cfg and cfg.get("prefix"):
            prefixes = [cfg["prefix"]]
    up = await db.user_prefix.find_one({"_id": str(message.author.id)})
    if up and up.get("prefix"):
        prefixes.append(up["prefix"])
    return commands.when_mentioned_or(*prefixes)(bot, message)


bot = commands.Bot(command_prefix=get_prefix, intents=intents, help_command=None, case_insensitive=True)


@bot.event
async def setup_hook():
    for ext in EXTENSIONS:
        try:
            await bot.load_extension(ext)
            log.info(f"loaded {ext}")
        except Exception as e:
            log.exception(f"failed to load {ext}: {e}")
    bot.add_view(WelcomeView())


@bot.event
async def on_ready():
    core.STATE["icon"] = str(bot.user.display_avatar.url)
    core.STATE["name"] = bot.user.name
    from general_cog import load_app_emojis
    await load_app_emojis(bot)
    log.info(f"Logged in as {bot.user} ({bot.user.id}) | guilds={len(bot.guilds)}")
    if not heartbeat.is_running():
        heartbeat.start()
    if not control_loop.is_running():
        control_loop.start()
    await apply_presence()


async def apply_presence():
    ctrl = await db.bot_control.find_one({"_id": "control"}) or {}
    text = ctrl.get("presence_text") or f"{PREFIX}help · {len(bot.guilds)} servers"
    type_map = {"watching": discord.ActivityType.watching, "playing": discord.ActivityType.playing,
                "listening": discord.ActivityType.listening, "competing": discord.ActivityType.competing}
    act = type_map.get(ctrl.get("presence_type", "watching"), discord.ActivityType.watching)
    status_map = {"online": discord.Status.online, "idle": discord.Status.idle,
                  "dnd": discord.Status.dnd, "invisible": discord.Status.invisible}
    await bot.change_presence(activity=discord.Activity(type=act, name=text),
                              status=status_map.get(ctrl.get("status", "online"), discord.Status.online))


@tasks.loop(seconds=8)
async def control_loop():
    """Apply admin-panel actions written to db.bot_control / db.bot_tasks."""
    try:
        await apply_presence()
        # process queued admin tasks (announcements / leave guild)
        tasks_pending = await db.bot_tasks.find({"status": "pending"}).to_list(20)
        for t in tasks_pending:
            try:
                if t["type"] == "announce":
                    g = bot.get_guild(int(t["guild_id"]))
                    if g:
                        ch = g.get_channel(int(t["channel_id"])) if t.get("channel_id") else (g.system_channel or next((c for c in g.text_channels if c.permissions_for(g.me).send_messages), None))
                        if ch:
                            await ch.send(embed=info_embed(t.get("title") or "Announcement", t["message"]))
                elif t["type"] == "leave":
                    g = bot.get_guild(int(t["guild_id"]))
                    if g:
                        await g.leave()
                await db.bot_tasks.update_one({"_id": t["_id"]}, {"$set": {"status": "done"}})
            except Exception as e:
                await db.bot_tasks.update_one({"_id": t["_id"]}, {"$set": {"status": "error", "error": str(e)}})
    except Exception as e:
        log.warning(f"control_loop failed: {e}")


@tasks.loop(seconds=10)
async def heartbeat():
    try:
        guilds = [{"id": str(g.id), "name": g.name, "members": g.member_count or 0,
                   "icon": str(g.icon.url) if g.icon else None} for g in bot.guilds]
        await db.bot_status.update_one({"_id": "live"}, {"$set": {
            "online": True, "servers": len(bot.guilds),
            "members": sum(g.member_count or 0 for g in bot.guilds),
            "latency": round(bot.latency * 1000) if bot.latency else 0,
            "uptime_seconds": int(time.time() - START_TIME),
            "started_at": datetime.fromtimestamp(START_TIME, tz=timezone.utc).isoformat(),
            "commands_run": command_counter["count"],
            "bot_name": str(bot.user.name) if bot.user else BRAND,
            "bot_avatar": str(bot.user.display_avatar.url) if bot.user else None,
            "guilds": guilds,
            "last_ping": datetime.now(timezone.utc).isoformat(),
        }}, upsert=True)
    except Exception as e:
        log.warning(f"heartbeat failed: {e}")


# ---- alias + disabled-command resolution ----
@bot.event
async def on_message(message):
    if message.author.bot or not message.guild:
        return await bot.process_commands(message)
    # bare @mention -> reply with help + support links (bleed style)
    if message.content.strip() in (f"<@{bot.user.id}>", f"<@!{bot.user.id}>"):
        return await message.channel.send(
            f"{message.author.mention}: https://canary.website/, join the discord server @ https://discord.gg/bhUgXtQFYG")
    cfg = await db.guild_config.find_one({"_id": str(message.guild.id)}) or {}
    aliases = cfg.get("aliases", {})
    if aliases:
        prefix_used = None
        for p in ([cfg.get("prefix", PREFIX)]):
            if message.content.startswith(p):
                prefix_used = p
                break
        if prefix_used:
            body = message.content[len(prefix_used):].strip()
            short = body.split(" ")[0].lower()
            if short in aliases:
                rest = body[len(short):]
                message.content = f"{prefix_used}{aliases[short]}{rest}"
    await bot.process_commands(message)


@bot.check
async def globally_block_disabled(ctx):
    if not ctx.guild:
        return True
    cfg = await get_cfg(ctx.guild.id)
    name = ctx.command.qualified_name.lower()
    if name in cfg.get("disabled_commands", []):
        return False
    if str(ctx.author.id) in cfg.get("ignored", []) or str(ctx.channel.id) in cfg.get("ignored", []):
        if not ctx.author.guild_permissions.administrator:
            return False
    return True


@bot.event
async def on_command_completion(ctx):
    command_counter["count"] += 1


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingPermissions):
        perm = error.missing_permissions[0] if error.missing_permissions else "permission"
        return await ctx.send(embed=discord.Embed(color=COLOR_WARN, description=f"⚠️ {ctx.author.mention}: You're **missing** permission: `{perm}`"))
    if isinstance(error, commands.BotMissingPermissions):
        perm = error.missing_permissions[0] if error.missing_permissions else "permission"
        return await ctx.send(embed=discord.Embed(color=COLOR_WARN, description=f"⚠️ {ctx.author.mention}: I'm **missing** permission: `{perm}`"))
    if isinstance(error, commands.MissingRequiredArgument):
        return await send_help(ctx, ctx.command, ctx.author)
    if isinstance(error, (commands.MemberNotFound, commands.UserNotFound)):
        return await ctx.send(embed=warn_embed(ctx.author, "I couldn't find that user."))
    if isinstance(error, commands.ChannelNotFound):
        return await ctx.send(embed=warn_embed(ctx.author, "I couldn't find that channel."))
    if isinstance(error, commands.RoleNotFound):
        return await ctx.send(embed=warn_embed(ctx.author, "I couldn't find that role."))
    if isinstance(error, commands.BadArgument):
        return await ctx.send(embed=warn_embed(ctx.author, "invalid argument provided."))
    if isinstance(error, commands.CommandOnCooldown):
        return await ctx.send(embed=warn_embed(ctx.author, f"slow down — try again in {error.retry_after:.1f}s."))
    if isinstance(error, commands.CheckFailure):
        return await ctx.send(embed=warn_embed(ctx.author, "you can't use this command."))
    if isinstance(error, commands.CommandInvokeError):
        original = error.original
        if isinstance(original, discord.Forbidden):
            return await ctx.send(embed=warn_embed(ctx.author, "I don't have permission to do that — check my role is **above** the target and has the needed permission."))
        if isinstance(original, discord.HTTPException):
            return await ctx.send(embed=err_embed(ctx.author, "Discord rejected that request. Please try again."))
    log.exception("Command error", exc_info=error)
    try:
        await ctx.send(embed=err_embed(ctx.author, f"an error occurred: `{type(error).__name__}`"))
    except discord.HTTPException:
        pass


def _module_of(cmd):
    return (cmd.cog.qualified_name.lower() if cmd.cog else "general")


@bot.command(name="help", aliases=["commands", "h"])
async def help_cmd(ctx, *, query: str = None):
    if query:
        cmd = bot.get_command(query.lower())
        if not cmd:
            return await ctx.send(embed=warn_embed(ctx.author, f"no command named `{query}`."))
        return await send_help(ctx, cmd, ctx.author)
    await ctx.send(
        f"{ctx.author.mention}: https://canary.bot/help, "
        f"join the discord server @ https://discord.gg/bhUgXtQFYG"
    )


@bot.command(name="reloademojis", aliases=["remojis"])
@commands.is_owner()
async def reloademojis(ctx):
    from general_cog import load_app_emojis
    em = await load_app_emojis(ctx.bot)
    await ctx.send(embed=ok_embed(ctx.author, f"Reloaded **{len(em)}** application emojis: {', '.join(em) or 'none'}"))


async def _do_autosetup(guild, member):
    """Full server setup used by the welcome 'Automatic Setup' button."""
    from general_cog import interface_embed, VoiceInterface
    cfg = await get_cfg(guild.id)
    created = []
    try:
        if not cfg.get("logs_channel"):
            logch = await guild.create_text_channel("mod-logs", reason="canary setup")
            await set_cfg(guild.id, logs_channel=str(logch.id), modlog=str(logch.id))
            created.append(logch.mention)

        async def _gor(name):
            r = discord.utils.get(guild.roles, name=name)
            if not r:
                r = await guild.create_role(name=name, reason="canary setup")
            return r
        jailed = await _gor("")
        await _gor("imute")
        await _gor("rmute")
        await set_cfg(guild.id, jail_role=str(jailed.id))

        if not (cfg.get("vm_hub") and guild.get_channel(int(cfg["vm_hub"]))):
            from general_cog import APP_EMOJIS
            emojis = APP_EMOJIS
            j2c_cat = await guild.create_category("VoiceMaster")
            vc_cat = await guild.create_category("Voice Channels")
            hub = await guild.create_voice_channel("j2c", category=j2c_cat)
            interface = await guild.create_text_channel("menu", category=j2c_cat)
            await interface.send(embed=interface_embed(emojis, guild, guild.me), view=VoiceInterface(emojis))
            await set_cfg(guild.id, vm_hub=str(hub.id), vm_j2c_category=str(j2c_cat.id),
                          vm_vc_category=str(vc_cat.id), vm_interface=str(interface.id))
            created.append("VoiceMaster (j2c + menu)")
    except discord.Forbidden:
        return "forbidden"
    return created


def _welcome_embed(guild):
    name = bot.user.name if bot.user else BRAND
    e = discord.Embed(color=COLOR, description=(
        f"Thank you for adding **{name}** to **{guild.name}**. {name} is a Discord bot "
        f"with over **150** commands aimed at making your Discord experience seamless, hassle-free and fun. "
        f"We are committed to resolving any issues that you face, instead of removing the bot,"
        f"[contact our support server](https://discord.gg/bhUgXtQFYG) to receive further help.\n\n"
        f"**{name}'s default prefix is set to:** `{PREFIX}` If you would like to change this prefix, "
        f"simply run `{PREFIX}prefix set (prefix)` and **ensure** that the bot has the necessary permissions."))
    e.add_field(name="Start Guide:", value=(
        f"`{PREFIX}setup` — Creates a log channel along with the jail role\n"
        f"`{PREFIX}voicemaster setup` — Creates join to create voice channels\n"
        f"`{PREFIX}filter setup` — Initializes a setup for automod to moderate\n"
        f"`{PREFIX}antinuke ` — Creates the antinuke setup to keep your server safe"), inline=False)
    if guild.me.display_avatar:
        e.set_author(name=name, icon_url=guild.me.display_avatar.url)
    return e


class WelcomeView(discord.ui.View):
    def __init__(self, guild_id=None):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.add_item(discord.ui.Button(label="Docs", emoji="",
                                        url="https://canary.bot/commands", row=0))
        self.add_item(discord.ui.Button(label="Support",
                                        url="https://discord.gg/bhUgXtQFYG", row=0))

    @discord.ui.button(label="Setup", style=discord.ButtonStyle.primary,
                       custom_id="welcome_autosetup", row=1)
    async def autosetup(self, interaction, button):
        guild = interaction.guild or (interaction.client.get_guild(self.guild_id) if self.guild_id else None)
        if not guild:
            return await interaction.response.send_message(
                "Run this inside the server to set it up.", ephemeral=True)
        member = guild.get_member(interaction.user.id)
        if not member or not member.guild_permissions.administrator:
            return await interaction.response.send_message(
                "You need **Administrator** to run automatic setup.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        result = await _do_autosetup(guild, member)
        if result == "forbidden":
            return await interaction.followup.send(
                "I'm missing permissions to create channels/roles. Give me **Administrator** and retry.",
                ephemeral=True)
        msg = ("✓ **Setup complete** — created: " + ", ".join(result)) if result else "Everything is **already set up**."
        await interaction.followup.send(msg, ephemeral=True)


@bot.event
async def on_guild_join(guild):
    ctrl = await db.bot_control.find_one({"_id": "control"}) or {}
    wl = [str(x).strip() for x in (ctrl.get("server_whitelist") or [])]
    enabled = bool(ctrl.get("whitelist_enabled"))
    log.info(f"on_guild_join {guild.name} ({guild.id}); whitelist_enabled={enabled}; whitelist={wl}")
    if enabled and str(guild.id) not in wl:
        ch = guild.system_channel
        if not ch or not ch.permissions_for(guild.me).send_messages:
            ch = next((c for c in guild.text_channels if c.permissions_for(guild.me).send_messages), None)
        if ch:
            try:
                await ch.send(embed=discord.Embed(
                    color=COLOR_WARN,
                    description=(f"⚠️ This server is **not whitelisted** to use **{bot.user.name}**. "
                                 f"Leaving now — request access at https://discord.gg/bhUgXtQFYG")))
            except discord.HTTPException:
                pass
        await guild.leave()
        log.info(f"left non-whitelisted guild {guild.name} ({guild.id})")
        return

    log.info(f"joined guild {guild.name} ({guild.id})")
    e = _welcome_embed(guild)

    # find who added the bot and DM them the welcome (DM only — no server message)
    inviter = None
    try:
        async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.bot_add):
            if entry.target and entry.target.id == bot.user.id:
                inviter = entry.user
                break
    except (discord.Forbidden, discord.HTTPException):
        pass
    if inviter and not inviter.bot:
        try:
            await inviter.send(embed=e, view=WelcomeView(guild.id))
        except discord.HTTPException:
            pass


if __name__ == "__main__":
    bot.run(TOKEN, log_handler=None)
