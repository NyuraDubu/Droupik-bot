import os
import math
import os
import asyncpg
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
load_dotenv()

GUILD_ROLES_CAN_EDIT_OTHERS = {"Lead", "Murmureur"}
DASHBOARD_TITLE = "⚒️ Métiers & Niveaux de la Guilde"
CARDS_PER_PAGE = 6  # nb de cartes par page
EMOJI_BY_METIER = {
    "alchimiste": "🟢", "bûcheron": "🟢", "chasseur": "🟢", "mineur": "🟢", "paysan": "🟢", "pêcheur": "🟢",
    "bijoutier": "🔵", "joaillomage": "🔴", "cordonnier": "🔵", "cordomage": "🔴", "tailleur": "🔵", "costumage": "🔴",
    "forgeron": "🔵", "forgemage": "🔴", "façonneur": "🔵", "façomage": "🔴", "sculpteur": "🔵", "sculptemage": "🔴", "bricoleur": "🔵"
}
ACCENT_MAP = {"é":"e","è":"e","ê":"e","à":"a","ù":"u","ô":"o","û":"u","î":"i","ï":"i","ç":"c","ä":"a","ë":"e","ö":"o","ü":"u"}

def norm(s: str) -> str:
    s = s.lower().strip()
    for a,b in ACCENT_MAP.items(): s = s.replace(a,b)
    return s

def display_metier(name: str) -> str:
    n = norm(name)
    emoji = EMOJI_BY_METIER.get(n, "🛠️")
    return f"{emoji} {name.capitalize()}"

# INTENTS
INTENTS = discord.Intents.default()
INTENTS.guilds = True
INTENTS.members = True
INTENTS.message_content = True

class DB:
    def __init__(self, dsn: str | None = None):
        self.dsn = dsn or os.getenv("DATABASE_URL")
        if not self.dsn:
            raise RuntimeError("DATABASE_URL manquante")
        self.pool: asyncpg.Pool | None = None

    async def setup(self):
        self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=5)
        async with self.pool.acquire() as conn:
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS profiles(
                guild_id BIGINT NOT NULL,
                user_id  BIGINT NOT NULL,
                dofus_name TEXT,
                PRIMARY KEY (guild_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS jobs(
                guild_id BIGINT NOT NULL,
                user_id  BIGINT NOT NULL,
                job_name TEXT NOT NULL,
                level    INT   NOT NULL,
                PRIMARY KEY (guild_id, user_id, job_name)
            );
            CREATE TABLE IF NOT EXISTS settings(
                guild_id BIGINT PRIMARY KEY,
                dashboard_channel_id BIGINT,
                dashboard_message_id BIGINT
            );
            """)

    async def set_profile_name(self, guild_id: int, user_id: int, name: str):
        async with self.pool.acquire() as conn:
            await conn.execute("""
            INSERT INTO profiles(guild_id,user_id,dofus_name)
            VALUES($1,$2,$3)
            ON CONFLICT (guild_id,user_id) DO UPDATE SET dofus_name=EXCLUDED.dofus_name
            """, guild_id, user_id, name)

    async def get_profile_name(self, guild_id: int, user_id: int):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
            SELECT dofus_name FROM profiles WHERE guild_id=$1 AND user_id=$2
            """, guild_id, user_id)
            return row["dofus_name"] if row else None

    async def set_job(self, guild_id: int, user_id: int, job: str, level: int):
        job = norm(job)
        async with self.pool.acquire() as conn:
            await conn.execute("""
            INSERT INTO jobs(guild_id,user_id,job_name,level)
            VALUES($1,$2,$3,$4)
            ON CONFLICT (guild_id,user_id,job_name) DO UPDATE SET level=EXCLUDED.level
            """, guild_id, user_id, job, level)

    async def remove_job(self, guild_id: int, user_id: int, job: str):
        job = norm(job)
        async with self.pool.acquire() as conn:
            await conn.execute("""
            DELETE FROM jobs WHERE guild_id=$1 AND user_id=$2 AND job_name=$3
            """, guild_id, user_id, job)

    async def list_user_jobs(self, guild_id: int, user_id: int):
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
            SELECT job_name, level FROM jobs
            WHERE guild_id=$1 AND user_id=$2
            """, guild_id, user_id)
            out = [(r["job_name"], r["level"]) for r in rows]
            return sorted(out, key=lambda r: (-r[1], r[0]))

    async def roster(self, guild_id: int):
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
            SELECT j.user_id, p.dofus_name, j.job_name, j.level
            FROM jobs j
            LEFT JOIN profiles p ON p.guild_id=j.guild_id AND p.user_id=j.user_id
            WHERE j.guild_id=$1
            """, guild_id)
        data = {}
        for r in rows:
            lst = data.setdefault(r["user_id"], {"name": r["dofus_name"], "jobs": []})
            lst["jobs"].append((r["job_name"], r["level"]))
        result = []
        for uid, info in data.items():
            jobs = sorted(info["jobs"], key=lambda r: (-r[1], r[0]))
            avg = sum(l for _, l in jobs) / len(jobs)
            result.append((uid, info["name"], jobs, avg))
        result.sort(key=lambda x: (-x[3], x[0]))
        return result

    async def get_dashboard(self, guild_id: int):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
            SELECT dashboard_channel_id, dashboard_message_id
            FROM settings WHERE guild_id=$1
            """, guild_id)
            return (row["dashboard_channel_id"], row["dashboard_message_id"]) if row else (None, None)

    async def set_dashboard(self, guild_id: int, channel_id: int, message_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute("""
            INSERT INTO settings(guild_id, dashboard_channel_id, dashboard_message_id)
            VALUES($1,$2,$3)
            ON CONFLICT (guild_id) DO UPDATE SET
              dashboard_channel_id=EXCLUDED.dashboard_channel_id,
              dashboard_message_id=EXCLUDED.dashboard_message_id
            """, guild_id, channel_id, message_id)

db = DB()

class DashboardView(discord.ui.View):
    def __init__(self, bot: commands.Bot, guild_id: int, total_pages: int, current_page: int = 0, selected_filter: str | None = None):
        super().__init__(timeout=86400)
        self.bot = bot
        self.guild_id = guild_id
        self.total_pages = max(1, total_pages)
        self.current_page = max(0, min(current_page, self.total_pages - 1))
        self.selected_filter = selected_filter

        self.prev_btn = discord.ui.Button(emoji="◀️", style=discord.ButtonStyle.secondary)
        self.next_btn = discord.ui.Button(emoji="▶️", style=discord.ButtonStyle.secondary)
        self.refresh_btn = discord.ui.Button(emoji="🔄", style=discord.ButtonStyle.secondary)
        self.add_item(self.prev_btn); self.add_item(self.refresh_btn); self.add_item(self.next_btn)

        options = [discord.SelectOption(label="Tous les métiers", value="__all")]
        metiers = sorted({m for m in EMOJI_BY_METIER.keys()})
        seen = set()
        for m in metiers:
            base = m.replace("û","u").replace("â","a")
            if base in seen: 
                continue
            seen.add(base)
            options.append(discord.SelectOption(label=m.capitalize(), value=m, emoji=EMOJI_BY_METIER.get(m,"🛠️")))
        self.select = discord.ui.Select(placeholder="Filtrer par métier…", min_values=1, max_values=1, options=options)
        self.add_item(self.select)

        @self.prev_btn.callback
        async def prev_callback(interaction: discord.Interaction):
            self.current_page = (self.current_page - 1) % self.total_pages
            await update_dashboard_message(self.bot, interaction.guild_id, interaction.message, self.current_page, self.selected_filter, via_interaction=interaction)

        @self.next_btn.callback
        async def next_callback(interaction: discord.Interaction):
            self.current_page = (self.current_page + 1) % self.total_pages
            await update_dashboard_message(self.bot, interaction.guild_id, interaction.message, self.current_page, self.selected_filter, via_interaction=interaction)

        @self.refresh_btn.callback
        async def refresh_callback(interaction: discord.Interaction):
            await update_dashboard_message(self.bot, interaction.guild_id, interaction.message, self.current_page, self.selected_filter, via_interaction=interaction, force_reload=True)

        @self.select.callback
        async def select_callback(interaction: discord.Interaction):
            val = self.select.values[0]
            self.selected_filter = None if val == "__all" else val
            self.current_page = 0
            await update_dashboard_message(self.bot, interaction.guild_id, interaction.message, self.current_page, self.selected_filter, via_interaction=interaction)

async def build_dashboard_embed(guild: discord.Guild, page: int = 0, job_filter: str | None = None):
    roster = await db.roster(guild.id)

    if job_filter:
        roster = [r for r in roster if any(norm(j)==job_filter for j,_ in r[2])]

    total_pages = max(1, math.ceil(len(roster) / CARDS_PER_PAGE))
    page = max(0, min(page, total_pages - 1))
    start = page * CARDS_PER_PAGE
    chunk = roster[start:start + CARDS_PER_PAGE]

    embed = discord.Embed(
        title=DASHBOARD_TITLE if not job_filter else f"{DASHBOARD_TITLE} • Filtre: {job_filter.capitalize()}",
        description=f"**{len(roster)}** profils • Page **{page+1}/{total_pages}**",
        color=discord.Color.purple()
    )

    if not chunk:
        embed.description += "\n\n*Aucun profil pour l’instant.*"
        return embed, total_pages

    for user_id, dofus_name, jobs, avg in chunk:
        member = guild.get_member(user_id)
        name_line = member.display_name if member else f"Utilisateur {user_id}"
        if dofus_name:
            name_line += f" *(aka {dofus_name})*"
        lines = [f"{display_metier(j)} : **{lvl}**" for j, lvl in jobs]
        embed.add_field(name=f"👤 {name_line}", value="\n".join(lines), inline=False)

    return embed, total_pages

async def update_dashboard_message(bot: commands.Bot, guild_id: int, message: discord.Message, page: int = 0, job_filter: str | None = None, via_interaction: discord.Interaction | None = None, force_reload: bool=False):
    guild = bot.get_guild(guild_id)
    embed, total_pages = await build_dashboard_embed(guild, page, job_filter)
    view = DashboardView(bot, guild_id, total_pages, page, job_filter)
    if via_interaction:
        await via_interaction.response.edit_message(embed=embed, view=view)
    else:
        await message.edit(embed=embed, view=view)

class MetiersBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=INTENTS)
        self.synced = False

    async def setup_hook(self):
        await db.setup()
        # NOTE:

    async def on_ready(self):
        if not self.synced:
            await self.tree.sync()
            self.synced = True
        print(f"Connecté en tant que {self.user} (ID: {self.user.id})")

bot = MetiersBot()

def can_edit_others(member: discord.Member) -> bool:
    return any(r.name in GUILD_ROLES_CAN_EDIT_OTHERS for r in member.roles)

@bot.tree.command(description="Définir ce salon comme Dashboard Métiers (ou republier).")
@app_commands.checks.has_permissions(manage_guild=True)
async def dashboard(interaction: discord.Interaction, action: str | None = "setchannel"):
    if action != "setchannel":
        return await interaction.response.send_message("Usage: /dashboard setchannel", ephemeral=True)

    await interaction.response.defer(thinking=True, ephemeral=True)
    guild = interaction.guild
    channel = interaction.channel

    embed, total_pages = await build_dashboard_embed(guild, page=0, job_filter=None)
    view = DashboardView(bot, guild.id, total_pages, 0, None)

    ch_id, msg_id = await db.get_dashboard(guild.id)
    posted = None
    if ch_id and msg_id:
        try:
            ch = guild.get_channel(ch_id) or await guild.fetch_channel(ch_id)
            msg = await ch.fetch_message(msg_id)
            await msg.edit(embed=embed, view=view)
            posted = msg
        except Exception:
            posted = None

    if not posted:
        posted = await channel.send(embed=embed, view=view)

    await db.set_dashboard(guild.id, channel.id, posted.id)
    await interaction.followup.send(f"Dashboard publié dans {channel.mention}.", ephemeral=True)

@bot.tree.command(description="Définir/mettre à jour ton pseudo Dofus affiché sur ta fiche.")
async def profil_setname(interaction: discord.Interaction, pseudo_dofus: str):
    await db.set_profile_name(interaction.guild_id, interaction.user.id, pseudo_dofus.strip())
    await interaction.response.send_message(f"Ton pseudo Dofus est maintenant **{pseudo_dofus}**.", ephemeral=True)
    ch_id, msg_id = await db.get_dashboard(interaction.guild_id)
    if ch_id and msg_id:
        try:
            ch = interaction.guild.get_channel(ch_id) or await interaction.guild.fetch_channel(ch_id)
            msg = await ch.fetch_message(msg_id)
            await update_dashboard_message(bot, interaction.guild_id, msg)
        except Exception:
            pass

@bot.tree.command(description="Ajouter/mettre à jour un métier (ex: /metier_set paysan 200).")
async def metier_set(interaction: discord.Interaction, metier: str, niveau: app_commands.Range[int, 1, 200], membre: discord.Member | None = None):
    target = membre or interaction.user
    if membre and (target.id != interaction.user.id) and not can_edit_others(interaction.user):
        return await interaction.response.send_message("Tu ne peux modifier que **tes** métiers.", ephemeral=True)

    await db.set_job(interaction.guild_id, target.id, metier, niveau)
    await interaction.response.send_message(f"{display_metier(metier)} de {target.mention} → **{niveau}**.", ephemeral=True)

    ch_id, msg_id = await db.get_dashboard(interaction.guild_id)
    if ch_id and msg_id:
        try:
            ch = interaction.guild.get_channel(ch_id) or await interaction.guild.fetch_channel(ch_id)
            msg = await ch.fetch_message(msg_id)
            await update_dashboard_message(bot, interaction.guild_id, msg)
        except Exception:
            pass

@bot.tree.command(description="Retirer un métier (ex: /metier_remove paysan).")
async def metier_remove(interaction: discord.Interaction, metier: str, membre: discord.Member | None = None):
    target = membre or interaction.user
    if membre and (target.id != interaction.user.id) and not can_edit_others(interaction.user):
        return await interaction.response.send_message("Tu ne peux modifier que **tes** métiers.", ephemeral=True)

    await db.remove_job(interaction.guild_id, target.id, metier)
    await interaction.response.send_message(f"{display_metier(metier)} retiré pour {target.mention}.", ephemeral=True)

    ch_id, msg_id = await db.get_dashboard(interaction.guild_id)
    if ch_id and msg_id:
        try:
            ch = interaction.guild.get_channel(ch_id) or await interaction.guild.fetch_channel(ch_id)
            msg = await ch.fetch_message(msg_id)
            await update_dashboard_message(bot, interaction.guild_id, msg)
        except Exception:
            pass

@bot.tree.command(description="Afficher la fiche métiers d'un membre.")
async def metier_list(interaction: discord.Interaction, membre: discord.Member | None = None):
    member = membre or interaction.user
    jobs = await db.list_user_jobs(interaction.guild_id, member.id)
    if not jobs:
        return await interaction.response.send_message(f"Aucun métier pour {member.mention}.", ephemeral=True)
    dofus_name = await db.get_profile_name(interaction.guild_id, member.id)
    embed = discord.Embed(
        title=f"Fiche Métiers — {member.display_name}" + (f" (aka {dofus_name})" if dofus_name else ""),
        color=discord.Color.blurple()
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    for j, lvl in jobs:
        embed.add_field(name=display_metier(j), value=f"Niveau **{lvl}**", inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(description="Re-rendre le dashboard (si souci d’affichage).")
@app_commands.checks.has_permissions(manage_guild=True)
async def dashboard_refresh(interaction: discord.Interaction):
    ch_id, msg_id = await db.get_dashboard(interaction.guild_id)
    if not (ch_id and msg_id):
        return await interaction.response.send_message("Dashboard non configuré. Utilise `/dashboard setchannel` dans le salon voulu.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    ch = interaction.guild.get_channel(ch_id) or await interaction.guild.fetch_channel(ch_id)
    msg = await ch.fetch_message(msg_id)
    await update_dashboard_message(bot, interaction.guild_id, msg)
    await interaction.followup.send("Dashboard rafraîchi.", ephemeral=True)

TOKEN = os.getenv("DISCORD_TOKEN") or "PUT_TOKEN_HERE"
bot.run(TOKEN)
