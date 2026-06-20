"""roles_cog.py (5/5) — boosterrole suite (color, name, icon, list, limit, etc.)."""
import random

import aiohttp
import discord
from discord.ext import commands

from core import require_perms, ok_embed, warn_embed, info_embed, get_cfg, set_cfg, push_cfg, db, PREFIX


def booster_only():
    async def pred(ctx):
        if not getattr(ctx.author, "premium_since", None) and not ctx.author.guild_permissions.manage_guild:
            raise commands.CheckFailure("Booster only")
        return True
    return commands.check(pred)


class BoosterRoles(commands.Cog):
    def __init__(self, bot): self.bot = bot

    async def _get_role(self, ctx, create=True):
        name = f"booster-{ctx.author.id}"
        role = discord.utils.get(ctx.guild.roles, name=name)
        if not role and create:
            base_id = (await get_cfg(ctx.guild.id)).get("baserole")
            role = await ctx.guild.create_role(name=name, reason="Booster role")
            await ctx.author.add_roles(role)
            if base_id and (base := ctx.guild.get_role(int(base_id))):
                try: await role.edit(position=max(1, base.position - 1))
                except discord.HTTPException: pass
        return role

    @commands.group(invoke_without_command=True, aliases=["br"], help="Custom booster color role.")
    @booster_only()
    @commands.bot_has_permissions(manage_roles=True)
    async def boosterrole(self, ctx, color: str = None, *, name: str = None):
        if not color:
            return await ctx.send(embed=info_embed("Booster Role", f"`{PREFIX}boosterrole <hex> [name]` · `rename` · `remove` · `list`"))
        try: c = discord.Color(int(color.strip("#"), 16))
        except ValueError: return await ctx.send(embed=warn_embed(ctx.author, "invalid hex color."))
        role = await self._get_role(ctx)
        await role.edit(color=c, name=name or role.name)
        await ctx.send(embed=ok_embed(ctx.author, f"booster role set to `{color}`."))

    @boosterrole.command(name="rename")
    @booster_only()
    async def br_rename(self, ctx, *, new_name):
        bad = (await get_cfg(ctx.guild.id)).get("boosterrole_filter", [])
        if any(w in new_name.lower() for w in bad):
            return await ctx.send(embed=warn_embed(ctx.author, "that name contains a blacklisted word."))
        role = await self._get_role(ctx); await role.edit(name=new_name)
        await ctx.send(embed=ok_embed(ctx.author, f"renamed to **{new_name}**."))

    @boosterrole.command(name="remove")
    @booster_only()
    async def br_remove(self, ctx):
        role = await self._get_role(ctx, create=False)
        if role: await role.delete()
        await ctx.send(embed=ok_embed(ctx.author, "booster role removed."))

    @boosterrole.command(name="list")
    @require_perms(manage_guild=True)
    async def br_list(self, ctx):
        roles = [r for r in ctx.guild.roles if r.name.startswith("booster-")]
        await ctx.send(embed=info_embed("Booster Roles", "\n".join(r.mention for r in roles) or "none"))

    @boosterrole.command(name="base")
    @require_perms(manage_guild=True)
    async def br_base(self, ctx, role: discord.Role):
        await set_cfg(ctx.guild.id, baserole=str(role.id)); await ctx.send(embed=ok_embed(ctx.author, f"base role → **{role.name}**."))

    @boosterrole.command(name="limit")
    @require_perms(manage_guild=True)
    async def br_limit(self, ctx, limit: int):
        await set_cfg(ctx.guild.id, boosterrole_limit=limit); await ctx.send(embed=ok_embed(ctx.author, f"limit set to {limit}."))

    @boosterrole.command(name="cleanup")
    @require_perms(manage_guild=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def br_cleanup(self, ctx):
        removed = 0
        for r in [r for r in ctx.guild.roles if r.name.startswith("booster-")]:
            if not r.members:
                try: await r.delete(); removed += 1
                except discord.HTTPException: pass
        await ctx.send(embed=ok_embed(ctx.author, f"cleaned up {removed} unused booster roles."))

    @boosterrole.group(name="filter", invoke_without_command=True)
    @require_perms(manage_guild=True)
    async def br_filter(self, ctx, *, word):
        await push_cfg(ctx.guild.id, "boosterrole_filter", word.lower()); await ctx.send(embed=ok_embed(ctx.author, f"blacklisted `{word}`."))

    @br_filter.command(name="list")
    async def br_filter_list(self, ctx):
        words = (await get_cfg(ctx.guild.id)).get("boosterrole_filter", [])
        await ctx.send(embed=info_embed("Booster Role Filter", ", ".join(f"`{w}`" for w in words) or "none"))

    @boosterrole.command(name="award")
    @require_perms(manage_guild=True, manage_roles=True)
    async def br_award(self, ctx, role: discord.Role):
        await set_cfg(ctx.guild.id, boost_award=str(role.id)); await ctx.send(embed=ok_embed(ctx.author, f"boosters will be awarded **{role.name}**."))

    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        if not before.premium_since and after.premium_since:
            rid = (await get_cfg(after.guild.id)).get("boost_award")
            if rid and (r := after.guild.get_role(int(rid))):
                try: await after.add_roles(r, reason="Boost award")
                except discord.HTTPException: pass


async def setup(bot):
    await bot.add_cog(BoosterRoles(bot))
