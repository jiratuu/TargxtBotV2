import discord
from discord.ext import commands
from discord.ui import View, Select, ChannelSelect, RoleSelect, TextInput, Button
import json
import os
import asyncio
import random
from datetime import datetime, timedelta

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

def run_server():
    server = HTTPServer(("0.0.0.0", 10000), BaseHTTPRequestHandler)
    server.serve_forever()

threading.Thread(target=run_server).start()

# ============================================================
# CONFIG
# ============================================================
PREFIX = "+"
TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.all()
bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)

# Couleurs (inchangées)
COLORS = {
    "warn": discord.Color.orange(),
    "tempmute": discord.Color.dark_grey(),
    "unmute": discord.Color.green(),
    "ban": discord.Color.red(),
    "help": discord.Color.blue(),
    "sanctions": discord.Color.purple(),
    "lock": discord.Color.dark_red(),
    "unlock": discord.Color.green(),
    "disconnect": discord.Color.gold(),
    "ticket": discord.Color.teal(),
    "giveaway": discord.Color.from_str("#ff6b6b"),
    "giveaway_end": discord.Color.from_str("#2ecc71"),
    "ticket_pending": discord.Color.orange(),
    "ticket_accepted": discord.Color.green(),
    "ticket_denied": discord.Color.red(),
}

TIME_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(value: str):
    """Convertit '10m', '2h', etc. en secondes. Renvoie None si invalide."""
    value = value.strip().lower()
    if not value or value[-1] not in TIME_UNITS:
        return None
    try:
        amount = int(value[:-1])
    except ValueError:
        return None
    if amount < 1:
        return None
    return amount * TIME_UNITS[value[-1]]


# ============================================================
# GESTIONNAIRE DE STOCKAGE
# ============================================================
class Store:
    def __init__(self, path: str):
        self.path = path

    def read(self) -> dict:
        if not os.path.exists(self.path):
            return {}
        with open(self.path, "r", encoding="utf-8") as f:
            return json.load(f)

    def write(self, data: dict) -> None:
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)

    def update(self, mutator):
        data = self.read()
        result = mutator(data)
        self.write(data)
        return result


sanctions_store = Store("sanctions.json")
ticket_store = Store("ticket_config.json")
giveaway_store = Store("giveaways.json")


# ============================================================
# SANCTIONS — logique métier
# ============================================================
class SanctionManager:
    @staticmethod
    def _key(guild_id: int, user_id: int) -> str:
        return f"{guild_id}_{user_id}"

    @classmethod
    def add(cls, guild_id: int, user_id: int, action: str, reason: str, moderator: str):
        key = cls._key(guild_id, user_id)

        def mutate(data):
            data.setdefault(key, []).append({
                "action": action,
                "reason": reason,
                "moderator": moderator,
                "date": datetime.now().strftime("%d/%m/%Y %H:%M"),
            })

        sanctions_store.update(mutate)

    @classmethod
    def get_all(cls, guild_id: int, user_id: int) -> list:
        return sanctions_store.read().get(cls._key(guild_id, user_id), [])

    @classmethod
    def reset(cls, guild_id: int, user_id: int) -> bool:
        key = cls._key(guild_id, user_id)
        found = {"value": False}

        def mutate(data):
            if key in data:
                del data[key]
                found["value"] = True

        sanctions_store.update(mutate)
        return found["value"]

    @classmethod
    def remove_one(cls, guild_id: int, user_id: int, index: int):
        key = cls._key(guild_id, user_id)
        removed = {"value": None}

        def mutate(data):
            entries = data.get(key)
            if entries and 0 < index <= len(entries):
                removed["value"] = entries.pop(index - 1)
                if not entries:
                    del data[key]

        sanctions_store.update(mutate)
        return removed["value"]


# ============================================================
# TICKETS — Configuration (comme DraftBot)
# ============================================================
class TicketConfigManager:
    @staticmethod
    def get(guild_id: int) -> dict:
        return ticket_store.read().get(str(guild_id), {})

    @staticmethod
    def set(guild_id: int, key: str, value):
        gid = str(guild_id)
        def mutate(data):
            data.setdefault(gid, {})[key] = value
        ticket_store.update(mutate)

    @staticmethod
    def get_all(guild_id: int) -> dict:
        return ticket_store.read().get(str(guild_id), {})

    @staticmethod
    def set_multi(guild_id: int, values: dict):
        gid = str(guild_id)
        def mutate(data):
            data.setdefault(gid, {}).update(values)
        ticket_store.update(mutate)

    @staticmethod
    def add_reason(guild_id: int, reason_data: dict):
        """Ajoute une raison d'ouverture (label, emoji, description)"""
        gid = str(guild_id)
        def mutate(data):
            data.setdefault(gid, {}).setdefault("reasons", []).append(reason_data)
        ticket_store.update(mutate)

    @staticmethod
    def remove_reason(guild_id: int, index: int):
        gid = str(guild_id)
        def mutate(data):
            reasons = data.get(gid, {}).get("reasons", [])
            if 0 <= index < len(reasons):
                removed = reasons.pop(index)
                data[gid]["reasons"] = reasons
                return removed
            return None
        return ticket_store.update(mutate)

    @staticmethod
    def set_reasons(guild_id: int, reasons: list):
        gid = str(guild_id)
        def mutate(data):
            data.setdefault(gid, {})["reasons"] = reasons
        ticket_store.update(mutate)


async def send_dm(user: discord.abc.User, embed: discord.Embed):
    try:
        await user.send(embed=embed)
    except discord.HTTPException:
        pass


# ============================================================
# GIVEAWAYS
# ============================================================
class GiveawayManager:
    @staticmethod
    def all_running():
        return {
            gid: g for gid, g in giveaway_store.read().items()
            if g.get("status") == "running"
        }

    @staticmethod
    def save(message_id: int, data: dict):
        def mutate(store):
            store[str(message_id)] = data
        giveaway_store.update(mutate)

    @staticmethod
    def mark_ended(message_id: int):
        key = str(message_id)
        def mutate(store):
            if key in store:
                store[key]["status"] = "ended"
        giveaway_store.update(mutate)

    @staticmethod
    def delete(message_id: int):
        key = str(message_id)
        def mutate(store):
            store.pop(key, None)
        giveaway_store.update(mutate)

    @staticmethod
    def get(message_id: int):
        return giveaway_store.read().get(str(message_id))

    @classmethod
    async def loop(cls):
        await bot.wait_until_ready()
        while not bot.is_closed():
            now = datetime.now().timestamp()
            for gid, g in cls.all_running().items():
                if now >= g["end_timestamp"]:
                    await cls.finish(int(gid), g)
            await asyncio.sleep(5)

    @classmethod
    async def draw_winners(cls, message: discord.Message, count: int):
        reaction = discord.utils.get(message.reactions, emoji="🎉")
        if not reaction:
            return []
        participants = [u async for u in reaction.users() if u != bot.user]
        if not participants:
            return []
        return participants if len(participants) <= count else random.sample(participants, count)

    @classmethod
    async def finish(cls, message_id: int, g: dict):
        guild = bot.get_guild(g["guild_id"])
        if not guild:
            return
        channel = guild.get_channel(g["channel_id"])
        if not channel:
            return

        try:
            message = await channel.fetch_message(message_id)
        except discord.NotFound:
            cls.delete(message_id)
            return

        winners = await cls.draw_winners(message, g["winners"])

        embed = discord.Embed(
            title="🎉 **GIVEAWAY TERMINÉ** 🎉",
            description=f"**{g['prize']}**\n\nHébergé par : {g['host']}",
            color=COLORS["giveaway_end"],
        )
        if winners:
            mentions = ", ".join(w.mention for w in winners)
            embed.add_field(name="🏆 Gagnant(s)", value=mentions, inline=False)
            embed.set_footer(text="Félicitations aux gagnants !")
        else:
            embed.add_field(name="🏆 Gagnant(s)", value="Aucun participant 😢", inline=False)
        embed.timestamp = datetime.fromtimestamp(g["end_timestamp"])

        await message.edit(embed=embed, view=None)
        if winners:
            mentions = ", ".join(w.mention for w in winners)
            await channel.send(f"🎉 **Félicitations {mentions} !** Vous avez gagné **{g['prize']}** !")

        cls.mark_ended(message_id)


# ============================================================
# UI — GIVEAWAY
# ============================================================
class GiveawayDureeModal(discord.ui.Modal, title="⏳ Durée du giveaway"):
    duree = TextInput(label="Durée (ex: 30s, 5m, 2h, 1d)", placeholder="30s, 5m, 2h, 1d...", max_length=10)

    def __init__(self, parent: "GiveawaySetupView"):
        super().__init__()
        self.parent = parent

    async def on_submit(self, interaction: discord.Interaction):
        seconds = parse_duration(self.duree.value)
        if seconds is None:
            await interaction.response.send_message("❌ Durée invalide. Utilise s, m, h, d.", ephemeral=True)
            return
        self.parent.duree = self.duree.value.strip().lower()
        self.parent.duree_seconds = seconds
        await interaction.response.edit_message(embed=self.parent.build_embed(), view=self.parent)


class GiveawayGagnantsModal(discord.ui.Modal, title="👥 Nombre de gagnants"):
    gagnants = TextInput(label="Nombre de gagnants", placeholder="1, 2, 3, 5...", max_length=3)

    def __init__(self, parent: "GiveawaySetupView"):
        super().__init__()
        self.parent = parent

    async def on_submit(self, interaction: discord.Interaction):
        try:
            nb = int(self.gagnants.value.strip())
        except ValueError:
            await interaction.response.send_message("❌ Nombre invalide.", ephemeral=True)
            return
        if not (1 <= nb <= 100):
            await interaction.response.send_message("❌ Entre 1 et 100 gagnants.", ephemeral=True)
            return
        self.parent.gagnants = nb
        await interaction.response.edit_message(embed=self.parent.build_embed(), view=self.parent)


class GiveawayPrixModal(discord.ui.Modal, title="🎁 Prix du giveaway"):
    prix = TextInput(label="Prix à gagner", placeholder="Nitro Classic, 50€, Role exclusif...", max_length=100)

    def __init__(self, parent: "GiveawaySetupView"):
        super().__init__()
        self.parent = parent

    async def on_submit(self, interaction: discord.Interaction):
        self.parent.prix = self.prix.value.strip()
        await interaction.response.edit_message(embed=self.parent.build_embed(), view=self.parent)


class GiveawaySetupView(discord.ui.View):
    def __init__(self, ctx: commands.Context):
        super().__init__(timeout=300)
        self.ctx = ctx
        self.salon: discord.TextChannel | None = None
        self.duree: str | None = None
        self.duree_seconds = 0
        self.gagnants = 1
        self.prix = "Non défini"

    def _guard(self, interaction: discord.Interaction) -> bool:
        return interaction.user == self.ctx.author

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="🎉 Configuration du Giveaway",
            description="Configure ton giveaway avec les boutons ci-dessous.",
            color=COLORS["giveaway"],
        )
        embed.add_field(name="📢 Salon", value=self.salon.mention if self.salon else "❌ Non défini", inline=False)
        embed.add_field(name="⏳ Durée", value=self.duree or "❌ Non définie", inline=False)
        embed.add_field(name="👥 Gagnant(s)", value=str(self.gagnants), inline=False)
        embed.add_field(name="🎁 Prix", value=self.prix, inline=False)
        embed.set_footer(text="Configure tous les champs puis clique sur Lancer")
        return embed

    @discord.ui.button(label="📢 Salon", style=discord.ButtonStyle.secondary, row=0)
    async def set_salon(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not self._guard(interaction):
            return await interaction.response.send_message("❌ Ce n'est pas ton panneau.", ephemeral=True)

        await interaction.response.send_message("📢 **Mentionne le salon** où envoyer le giveaway :", ephemeral=True)

        def check(m):
            return m.author == self.ctx.author and m.channel == self.ctx.channel

        try:
            msg = await self.ctx.bot.wait_for("message", timeout=30, check=check)
        except asyncio.TimeoutError:
            await interaction.edit_original_response(content="⏰ Temps écoulé.", embed=self.build_embed(), view=self)
            return

        if msg.channel_mentions:
            self.salon = msg.channel_mentions[0]
            await msg.delete()
            await interaction.edit_original_response(content="✅ Salon défini !", embed=self.build_embed(), view=self)
        else:
            await interaction.edit_original_response(content="❌ Salon invalide.", embed=self.build_embed(), view=self)

    @discord.ui.button(label="⏳ Durée", style=discord.ButtonStyle.secondary, row=0)
    async def set_duree(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not self._guard(interaction):
            return await interaction.response.send_message("❌ Ce n'est pas ton panneau.", ephemeral=True)
        await interaction.response.send_modal(GiveawayDureeModal(self))

    @discord.ui.button(label="👥 Gagnants", style=discord.ButtonStyle.secondary, row=1)
    async def set_gagnants(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not self._guard(interaction):
            return await interaction.response.send_message("❌ Ce n'est pas ton panneau.", ephemeral=True)
        await interaction.response.send_modal(GiveawayGagnantsModal(self))

    @discord.ui.button(label="🎁 Prix", style=discord.ButtonStyle.secondary, row=1)
    async def set_prix(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not self._guard(interaction):
            return await interaction.response.send_message("❌ Ce n'est pas ton panneau.", ephemeral=True)
        await interaction.response.send_modal(GiveawayPrixModal(self))

    @discord.ui.button(label="🚀 Lancer le giveaway", style=discord.ButtonStyle.success, row=2)
    async def lancer(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not self._guard(interaction):
            return await interaction.response.send_message("❌ Ce n'est pas ton panneau.", ephemeral=True)
        if not self.salon:
            return await interaction.response.send_message("❌ Configure d'abord le salon.", ephemeral=True)
        if not self.duree:
            return await interaction.response.send_message("❌ Configure d'abord la durée.", ephemeral=True)
        if self.prix == "Non défini":
            return await interaction.response.send_message("❌ Configure d'abord le prix.", ephemeral=True)

        await interaction.response.defer()

        end_timestamp = (datetime.now() + timedelta(seconds=self.duree_seconds)).timestamp()
        embed = discord.Embed(
            title="🎉 **GIVEAWAY** 🎉",
            description=(
                f"**{self.prix}**\n\n"
                f"🎁 **Gagnant(s) :** {self.gagnants}\n"
                f"⏳ **Se termine :** <t:{int(end_timestamp)}:R>\n"
                f"🛡️ **Hébergé par :** {self.ctx.author.mention}"
            ),
            color=COLORS["giveaway"],
        )
        embed.set_footer(text="Clique sur 🎉 pour participer !")
        embed.timestamp = datetime.fromtimestamp(end_timestamp)

        msg = await self.salon.send(embed=embed)
        await msg.add_reaction("🎉")

        GiveawayManager.save(msg.id, {
            "guild_id": self.ctx.guild.id,
            "channel_id": self.salon.id,
            "host": self.ctx.author.mention,
            "prize": self.prix,
            "winners": self.gagnants,
            "end_timestamp": end_timestamp,
            "status": "running",
        })

        confirm = discord.Embed(
            title="✅ Giveaway lancé !",
            description=f"Giveaway envoyé dans {self.salon.mention}",
            color=discord.Color.green(),
        )
        confirm.add_field(name="🎁 Prix", value=self.prix, inline=True)
        confirm.add_field(name="⏳ Durée", value=self.duree, inline=True)
        confirm.add_field(name="👥 Gagnants", value=str(self.gagnants), inline=True)
        await self.ctx.send(embed=confirm)
        await interaction.delete_original_response()

    @discord.ui.button(label="❌ Annuler", style=discord.ButtonStyle.danger, row=2)
    async def annuler(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not self._guard(interaction):
            return await interaction.response.send_message("❌ Ce n'est pas ton panneau.", ephemeral=True)
        await interaction.response.send_message("❌ Giveaway annulé.", ephemeral=True)
        await interaction.delete_original_response()


# ============================================================
# TICKETS — Système complet style DraftBot
# ============================================================

class TicketReasonSelect(View):
    """Menu de sélection de raisons pour l'ouverture de ticket"""
    def __init__(self, reasons: list):
        super().__init__(timeout=300)
        self.reasons = reasons
        options = []
        for i, r in enumerate(reasons):
            emoji = r.get("emoji") or None
            options.append(
                discord.SelectOption(
                    label=r["label"],
                    description=r.get("description", ""),
                    emoji=emoji,
                    value=str(i)
                )
            )
        select = Select(placeholder="Choisis une raison...", options=options)
        select.callback = self.select_callback
        self.add_item(select)

    async def select_callback(self, interaction: discord.Interaction):
        index = int(interaction.data["values"][0])
        reason = self.reasons[index]
        await interaction.response.send_modal(TicketDescriptionModal(reason["label"], reason.get("description", "")))


class TicketButtonView(View):
    """Bouton unique pour ouvrir un ticket (mode bouton DraftBot)"""
    def __init__(self, reason_label: str, reason_description: str = ""):
        super().__init__(timeout=None)
        self.reason_label = reason_label
        self.reason_description = reason_description

    @discord.ui.button(label="🎫 Ouvrir un ticket", style=discord.ButtonStyle.primary, custom_id="ticket_open_btn")
    async def open_ticket_btn(self, interaction: discord.Interaction, button: Button):
        cfg = TicketConfigManager.get(interaction.guild_id)
        motif_obligatoire = cfg.get("motif_obligatoire", False)
        
        if motif_obligatoire or self.reason_description:
            await interaction.response.send_modal(TicketDescriptionModal(self.reason_label, self.reason_description))
        else:
            await create_ticket(interaction, self.reason_label, "")


class TicketDescriptionModal(discord.ui.Modal, title="📝 Décris ta demande"):
    def __init__(self, sujet: str, description_hint: str = ""):
        super().__init__()
        self.sujet = sujet
        placeholder = description_hint or "Explique brièvement ta demande..."
        self.desc_input = TextInput(
            label="Description",
            placeholder=placeholder,
            style=discord.TextStyle.long,
            max_length=2000,
            required=False
        )
        self.add_item(self.desc_input)

    async def on_submit(self, interaction: discord.Interaction):
        await create_ticket(interaction, self.sujet, self.desc_input.value or "")


class TicketAcceptDenyView(View):
    """Boutons Accepter/Refuser dans le salon de réception (DraftBot style)"""
    def __init__(self, ticket_owner: discord.Member, reason: str, description: str):
        super().__init__(timeout=None)
        self.ticket_owner = ticket_owner
        self.reason = reason
        self.description = description

    @discord.ui.button(label="✅ Accepter", style=discord.ButtonStyle.success, custom_id="ticket_accept")
    async def accept(self, interaction: discord.Interaction, button: Button):
        guild = interaction.guild
        cfg = TicketConfigManager.get(guild.id)
        
        # Vérifier que l'utilisateur a le rôle staff ou admin
        if not await is_ticket_mod(interaction):
            return await interaction.response.send_message("❌ Tu n'as pas la permission.", ephemeral=True)

        category = discord.utils.get(guild.categories, id=cfg.get("category_id"))
        if not category:
            return await interaction.response.send_message("❌ Catégorie des tickets introuvable.", ephemeral=True)

        staff_roles = cfg.get("staff_roles", [])
        chan_name = f"ticket-{self.ticket_owner.name.lower().replace(' ', '-')}-{self.ticket_owner.discriminator}"

        # Permissions
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            self.ticket_owner: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        }
        for role_id in staff_roles:
            role = guild.get_role(role_id)
            if role:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

        chan = await guild.create_text_channel(
            name=chan_name, category=category, overwrites=overwrites,
            reason=f"Ticket accepté par {interaction.user}"
        )

        # Message de bienvenue
        embed = discord.Embed(
            title="🎫 Ticket ouvert",
            description=cfg.get("ticket_message", "🎫 **Bienvenue !** Un membre du staff va vous répondre."),
            color=COLORS["ticket"]
        )
        embed.add_field(name="👤 Utilisateur", value=self.ticket_owner.mention, inline=True)
        embed.add_field(name="🎯 Sujet", value=self.reason, inline=True)
        if self.description:
            embed.add_field(name="📝 Description", value=self.description, inline=False)
        
        staff_mention = ""
        if cfg.get("mention_moderators", False) and staff_roles:
            mentions = [guild.get_role(rid).mention for rid in staff_roles if guild.get_role(rid)]
            staff_mention = ", ".join(mentions) + " — "

        await chan.send(
            content=f"{self.ticket_owner.mention} — {staff_mention}",
            embed=embed,
            view=TicketCloseView()
        )

        # Notification à l'utilisateur
        notif = discord.Embed(
            title="✅ Ticket accepté",
            description=f"Ton ticket a été accepté par {interaction.user.mention}.",
            color=COLORS["ticket_accepted"]
        )
        notif.add_field(name="Salon", value=chan.mention, inline=False)
        await send_dm(self.ticket_owner, notif)

        # Mise à jour du message de réception
        await interaction.message.edit(
            embed=discord.Embed(
                title="✅ Ticket accepté",
                description=f"Ticket de {self.ticket_owner.mention} accepté par {interaction.user.mention}",
                color=COLORS["ticket_accepted"]
            ),
            view=None
        )
        await interaction.response.send_message(f"✅ Ticket créé : {chan.mention}", ephemeral=True)

    @discord.ui.button(label="❌ Refuser", style=discord.ButtonStyle.danger, custom_id="ticket_deny")
    async def deny(self, interaction: discord.Interaction, button: Button):
        if not await is_ticket_mod(interaction):
            return await interaction.response.send_message("❌ Tu n'as pas la permission.", ephemeral=True)

        notif = discord.Embed(
            title="❌ Ticket refusé",
            description=f"Ta demande de ticket a été refusée par {interaction.user.mention}.",
            color=COLORS["ticket_denied"]
        )
        notif.add_field(name="Sujet", value=self.reason, inline=False)
        if self.description:
            notif.add_field(name="Description", value=self.description, inline=False)
        await send_dm(self.ticket_owner, notif)

        await interaction.message.edit(
            embed=discord.Embed(
                title="❌ Ticket refusé",
                description=f"Ticket de {self.ticket_owner.mention} refusé par {interaction.user.mention}",
                color=COLORS["ticket_denied"]
            ),
            view=None
        )
        await interaction.response.send_message("❌ Ticket refusé.", ephemeral=True)


class TicketCloseView(View):
    """Bouton pour fermer un ticket"""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔒 Fermer le ticket", style=discord.ButtonStyle.danger, custom_id="close_ticket_btn")
    async def close_ticket(self, interaction: discord.Interaction, button: Button):
        if not interaction.channel.name.startswith("ticket-"):
            return await interaction.response.send_message("❌ Ce n'est pas un ticket.", ephemeral=True)

        guild = interaction.guild
        cfg = TicketConfigManager.get(guild.id)
        suppression_auto = cfg.get("suppression_auto", False)
        
        # Vérifier si l'utilisateur peut fermer
        has_perm = interaction.user.guild_permissions.administrator or \
                   any(role.id in cfg.get("staff_roles", []) for role in interaction.user.roles) or \
                   interaction.channel.name.endswith(interaction.user.discriminator)

        if not has_perm:
            return await interaction.response.send_message("❌ Tu n'as pas la permission.", ephemeral=True)

        embed = discord.Embed(
            title="🔒 Fermeture du ticket",
            description=f"Fermeture par {interaction.user.mention}",
            color=discord.Color.red()
        )
        await interaction.response.send_message(embed=embed)

        if suppression_auto and interaction.user.guild_permissions.administrator:
            await asyncio.sleep(3)
            await interaction.channel.delete(reason=f"Ticket fermé par {interaction.user}")
        else:
            # Sinon on envoie un message avec confirmation
            confirm_view = TicketConfirmCloseView(interaction.channel)
            await interaction.channel.send(
                embed=discord.Embed(
                    title="⚠️ Confirmation",
                    description="Clique sur **Confirmer** pour fermer définitivement ce ticket.",
                    color=discord.Color.orange()
                ),
                view=confirm_view
            )


class TicketConfirmCloseView(View):
    """Confirmation de fermeture"""
    def __init__(self, channel: discord.TextChannel):
        super().__init__(timeout=60)
        self.channel = channel

    @discord.ui.button(label="✅ Confirmer la fermeture", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_message("🔒 Fermeture dans 5 secondes...")
        await asyncio.sleep(5)
        await self.channel.delete(reason=f"Ticket fermé par {interaction.user}")


async def is_ticket_mod(interaction: discord.Interaction) -> bool:
    """Vérifie si un utilisateur peut modérer les tickets"""
    if interaction.user.guild_permissions.administrator:
        return True
    cfg = TicketConfigManager.get(interaction.guild_id)
    staff_roles = cfg.get("staff_roles", [])
    return any(role.id in staff_roles for role in interaction.user.roles)


async def create_ticket(interaction: discord.Interaction, reason: str, description: str):
    """Crée ou envoie une demande de ticket"""
    guild = interaction.guild
    cfg = TicketConfigManager.get(guild.id)
    
    if not cfg.get("category_id"):
        return await interaction.response.send_message("❌ Tickets non configurés.", ephemeral=True)

    validation = cfg.get("validation", True)  # DraftBot: validation activée par défaut
    reception_channel_id = cfg.get("reception_channel_id")

    # Vérifier si l'utilisateur a déjà un ticket
    existing = discord.utils.get(guild.text_channels, name=f"ticket-{interaction.user.name.lower().replace(' ', '-')}-{interaction.user.discriminator}")
    if existing:
        return await interaction.response.send_message("❌ Tu as déjà un ticket ouvert.", ephemeral=True)

    # Mode validation (DraftBot style) : demande envoyée dans le salon de réception
    if validation and reception_channel_id:
        reception_channel = guild.get_channel(reception_channel_id)
        if reception_channel:
            embed = discord.Embed(
                title="🎫 Nouvelle demande de ticket",
                description=f"**{interaction.user.mention}** a demandé un ticket.",
                color=COLORS["ticket_pending"]
            )
            embed.add_field(name="🎯 Sujet", value=reason, inline=True)
            if description:
                embed.add_field(name="📝 Description", value=description, inline=False)
            embed.add_field(name="👤 Utilisateur", value=f"{interaction.user} ({interaction.user.id})", inline=False)
            embed.set_thumbnail(url=interaction.user.display_avatar.url)
            embed.set_footer(text="Un modérateur va traiter ta demande")

            # Mention des modérateurs si activé
            content = None
            if cfg.get("mention_moderators", False):
                staff_roles = cfg.get("staff_roles", [])
                mentions = [guild.get_role(rid).mention for rid in staff_roles if guild.get_role(rid)]
                if mentions:
                    content = " ".join(mentions)

            await reception_channel.send(
                content=content,
                embed=embed,
                view=TicketAcceptDenyView(interaction.user, reason, description)
            )

            confirm = discord.Embed(
                title="✅ Demande envoyée",
                description="Ta demande de ticket a été envoyée. Un modérateur va la traiter.",
                color=COLORS["ticket_pending"]
            )
            await interaction.response.send_message(embed=confirm, ephemeral=True)
            return

    # Mode direct (sans validation) : création immédiate
    if validation and not reception_channel_id:
        return await interaction.response.send_message(
            "❌ Le salon de réception n'est pas configuré. Contacte un administrateur.",
            ephemeral=True
        )

    # Création directe (validation désactivée)
    category = discord.utils.get(guild.categories, id=cfg.get("category_id"))
    if not category:
        return await interaction.response.send_message("❌ Catégorie introuvable.", ephemeral=True)

    staff_roles = cfg.get("staff_roles", [])
    chan_name = f"ticket-{interaction.user.name.lower().replace(' ', '-')}-{interaction.user.discriminator}"

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
    }
    for role_id in staff_roles:
        role = guild.get_role(role_id)
        if role:
            overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

    chan = await guild.create_text_channel(
        name=chan_name, category=category, overwrites=overwrites,
        reason=f"Ticket de {interaction.user}"
    )

    embed = discord.Embed(
        title="🎫 Ticket ouvert",
        description=cfg.get("ticket_message", "🎫 **Bienvenue !** Un membre du staff va vous répondre."),
        color=COLORS["ticket"]
    )
    embed.add_field(name="👤 Utilisateur", value=interaction.user.mention, inline=True)
    embed.add_field(name="🎯 Sujet", value=reason, inline=True)
    if description:
        embed.add_field(name="📝 Description", value=description, inline=False)

    staff_mention = ""
    if cfg.get("mention_moderators", False) and staff_roles:
        mentions = [guild.get_role(rid).mention for rid in staff_roles if guild.get_role(rid)]
        staff_mention = ", ".join(mentions) + " — "

    await chan.send(
        content=f"{interaction.user.mention} — {staff_mention}",
        embed=embed,
        view=TicketCloseView()
    )

    await interaction.response.send_message(f"✅ Ticket créé : {chan.mention}", ephemeral=True)


# ============================================================
# TICKETS — UI Configuration (Panel style DraftBot)
# ============================================================

class TicketReasonConfigModal(discord.ui.Modal, title="✏️ Ajouter une raison"):
    label = TextInput(label="Nom de la raison", placeholder="Support, Partenariat, Plainte...", max_length=80)
    emoji = TextInput(label="Emoji (optionnel)", placeholder="🎫, 🤝, ⚠️...", max_length=10, required=False)
    description = TextInput(label="Description (optionnel)", placeholder="Décris cette raison...", max_length=200, required=False, style=discord.TextStyle.short)

    async def on_submit(self, interaction: discord.Interaction):
        cfg = TicketConfigManager.get(interaction.guild_id)
        reasons = cfg.get("reasons", [])
        reasons.append({
            "label": self.label.value.strip(),
            "emoji": self.emoji.value.strip() if self.emoji.value.strip() else "",
            "description": self.description.value.strip() if self.description.value.strip() else ""
        })
        TicketConfigManager.set_reasons(interaction.guild_id, reasons)
        await interaction.response.send_message(f"✅ Raison **{self.label.value.strip()}** ajoutée !", ephemeral=True)


class TicketChooseMessageTypeView(View):
    """Choix entre Bouton et Sélecteur pour le message d'ouverture (DraftBot style)"""
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label="🔘 Bouton (1 raison)", style=discord.ButtonStyle.primary)
    async def btn_mode(self, interaction: discord.Interaction, button: Button):
        modal = TicketSingleReasonModal()
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="📋 Sélecteur (plusieurs raisons)", style=discord.ButtonStyle.secondary)
    async def select_mode(self, interaction: discord.Interaction, button: Button):
        cfg = TicketConfigManager.get(interaction.guild_id)
        reasons = cfg.get("reasons", [])
        
        # Si déjà des raisons, envoyer le message directement
        if reasons:
            await send_ticket_panel(interaction, reasons, "select")
            await interaction.response.send_message("✅ Message d'ouverture (sélecteur) envoyé !", ephemeral=True)
        else:
            await interaction.response.send_message(
                "📝 Tu n'as pas encore de raisons. Ajoutes-en d'abord avec `+ticketconfig reasons`.",
                ephemeral=True
            )


class TicketSingleReasonModal(discord.ui.Modal, title="🔘 Raison du bouton"):
    label = TextInput(label="Nom du bouton", placeholder="Contacter le support", max_length=80)
    description = TextInput(label="Description (optionnel)", placeholder="Description pour le modal...", max_length=200, required=False, style=discord.TextStyle.short)

    async def on_submit(self, interaction: discord.Interaction):
        reasons = [{
            "label": self.label.value.strip(),
            "emoji": "",
            "description": self.description.value.strip() or ""
        }]
        TicketConfigManager.set_reasons(interaction.guild_id, reasons)
        
        await send_ticket_panel(interaction, reasons, "button")
        await interaction.response.send_message("✅ Message d'ouverture (bouton) envoyé !", ephemeral=True)


class TicketConfigPanelView(View):
    """Panneau de configuration complet style DraftBot"""
    def __init__(self, guild: discord.Guild):
        super().__init__(timeout=300)
        self.guild = guild

    @discord.ui.select(cls=ChannelSelect, channel_types=[discord.ChannelType.category],
                        placeholder="📂 Catégorie des tickets")
    async def select_category(self, interaction: discord.Interaction, select: ChannelSelect):
        category = select.values[0]
        TicketConfigManager.set(interaction.guild_id, "category_id", category.id)
        await interaction.response.send_message(f"✅ Catégorie définie : {category.mention}", ephemeral=True)

    @discord.ui.select(cls=ChannelSelect, channel_types=[discord.ChannelType.text],
                        placeholder="📥 Salon de réception des demandes")
    async def select_reception(self, interaction: discord.Interaction, select: ChannelSelect):
        channel = select.values[0]
        TicketConfigManager.set(interaction.guild_id, "reception_channel_id", channel.id)
        await interaction.response.send_message(f"✅ Salon de réception : {channel.mention}", ephemeral=True)

    @discord.ui.select(cls=RoleSelect, placeholder="👥 Rôles modérateurs (max 5)",
                        min_values=1, max_values=5)
    async def select_roles(self, interaction: discord.Interaction, select: RoleSelect):
        role_ids = [r.id for r in select.values]
        TicketConfigManager.set(interaction.guild_id, "staff_roles", role_ids)
        roles_mention = ", ".join(r.mention for r in select.values)
        await interaction.response.send_message(f"✅ Rôles staff : {roles_mention}", ephemeral=True)

    @discord.ui.button(label="✅ Validation des tickets", style=discord.ButtonStyle.secondary, row=2)
    async def toggle_validation(self, interaction: discord.Interaction, button: Button):
        cfg = TicketConfigManager.get(interaction.guild_id)
        current = cfg.get("validation", True)
        TicketConfigManager.set(interaction.guild_id, "validation", not current)
        status = "activée" if not current else "désactivée"
        await interaction.response.send_message(f"✅ Validation {status}.", ephemeral=True)

    @discord.ui.button(label="📢 Mention des modérateurs", style=discord.ButtonStyle.secondary, row=2)
    async def toggle_mention(self, interaction: discord.Interaction, button: Button):
        cfg = TicketConfigManager.get(interaction.guild_id)
        current = cfg.get("mention_moderators", False)
        TicketConfigManager.set(interaction.guild_id, "mention_moderators", not current)
        status = "activée" if not current else "désactivée"
        await interaction.response.send_message(f"✅ Mention des modos {status}.", ephemeral=True)

    @discord.ui.button(label="🗑️ Suppression auto (admin)", style=discord.ButtonStyle.secondary, row=2)
    async def toggle_auto_delete(self, interaction: discord.Interaction, button: Button):
        cfg = TicketConfigManager.get(interaction.guild_id)
        current = cfg.get("suppression_auto", False)
        TicketConfigManager.set(interaction.guild_id, "suppression_auto", not current)
        status = "activée" if not current else "désactivée"
        await interaction.response.send_message(f"✅ Suppression auto {status}.", ephemeral=True)

    @discord.ui.button(label="📝 Motif obligatoire", style=discord.ButtonStyle.secondary, row=3)
    async def toggle_motif(self, interaction: discord.Interaction, button: Button):
        cfg = TicketConfigManager.get(interaction.guild_id)
        current = cfg.get("motif_obligatoire", False)
        TicketConfigManager.set(interaction.guild_id, "motif_obligatoire", not current)
        status = "obligatoire" if not current else "optionnel"
        await interaction.response.send_message(f"✅ Motif {status}.", ephemeral=True)

    @discord.ui.button(label="✏️ Message du ticket", style=discord.ButtonStyle.secondary, row=3)
    async def edit_message(self, interaction: discord.Interaction, button: Button):
        modal = TicketMessageEditModal()
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="➕ Ajouter une raison", style=discord.ButtonStyle.success, row=3)
    async def add_reason(self, interaction: discord.Interaction, button: Button):
        modal = TicketReasonConfigModal()
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="📤 Envoyer message d'ouverture", style=discord.ButtonStyle.primary, row=4)
    async def send_panel(self, interaction: discord.Interaction, button: Button):
        cfg = TicketConfigManager.get(interaction.guild_id)
        reasons = cfg.get("reasons", [])
        
        if not reasons:
            await interaction.response.send_message(
                "📝 Choisis d'abord le type de message :",
                view=TicketChooseMessageTypeView(),
                ephemeral=True
            )
            return

        # Demander le type si plusieurs raisons existent
        view = View(timeout=120)
        view.add_item(Button(label="🔘 Bouton (1ère raison)", style=discord.ButtonStyle.primary))
        view.add_item(Button(label="📋 Sélecteur (toutes)", style=discord.ButtonStyle.secondary))

        async def btn_cb(inter: discord.Interaction):
            await send_ticket_panel(inter, [reasons[0]], "button")

        async def select_cb(inter: discord.Interaction):
            await send_ticket_panel(inter, reasons, "select")

        view.children[0].callback = btn_cb
        view.children[1].callback = select_cb

        await interaction.response.send_message("Choisis le type de message d'ouverture :", view=view, ephemeral=True)

    @discord.ui.button(label="📊 Voir la config", style=discord.ButtonStyle.secondary, row=4)
    async def show_config(self, interaction: discord.Interaction, button: Button):
        cfg = TicketConfigManager.get(interaction.guild_id)
        category = discord.utils.get(self.guild.categories, id=cfg.get("category_id"))
        reception = self.guild.get_channel(cfg.get("reception_channel_id")) if cfg.get("reception_channel_id") else None
        roles = [self.guild.get_role(rid) for rid in cfg.get("staff_roles", []) if self.guild.get_role(rid)]
        reasons = cfg.get("reasons", [])

        embed = discord.Embed(
            title="📊 Configuration des tickets",
            color=COLORS["ticket"]
        )
        embed.add_field(name="📂 Catégorie", value=category.mention if category else "❌", inline=False)
        embed.add_field(name="📥 Salon réception", value=reception.mention if reception else "❌", inline=False)
        embed.add_field(name="👥 Rôles staff", value=", ".join(r.mention for r in roles) or "❌", inline=False)
        embed.add_field(name="✅ Validation", value="✅" if cfg.get("validation", True) else "❌", inline=True)
        embed.add_field(name="📢 Mention modos", value="✅" if cfg.get("mention_moderators", False) else "❌", inline=True)
        embed.add_field(name="🗑️ Suppression auto", value="✅" if cfg.get("suppression_auto", False) else "❌", inline=True)
        embed.add_field(name="📝 Motif obligatoire", value="✅" if cfg.get("motif_obligatoire", False) else "❌", inline=True)
        
        if reasons:
            reasons_text = "\n".join(f"{i+1}. {r.get('emoji','')} {r['label']}" for i, r in enumerate(reasons))
            embed.add_field(name="📋 Raisons", value=reasons_text, inline=False)
        
        embed.set_footer(text="Utilise +ticketconfig pour modifier")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="❌ Fermer", style=discord.ButtonStyle.danger, row=4)
    async def close_panel(self, interaction: discord.Interaction, button: Button):
        await interaction.message.delete()
        await interaction.response.send_message("🔧 Panneau fermé.", ephemeral=True)


class TicketMessageEditModal(discord.ui.Modal, title="✏️ Message du ticket"):
    message = TextInput(
        label="Message de bienvenue",
        style=discord.TextStyle.long,
        placeholder="🎫 Bienvenue dans votre ticket ! Un membre du staff va vous répondre.",
        max_length=4000
    )

    async def on_submit(self, interaction: discord.Interaction):
        TicketConfigManager.set(interaction.guild_id, "ticket_message", self.message.value)
        await interaction.response.send_message("✅ Message mis à jour !", ephemeral=True)


async def send_ticket_panel(interaction_or_ctx, reasons: list, mode: str):
    """Envoie le message d'ouverture de ticket dans le salon actuel"""
    if isinstance(interaction_or_ctx, discord.Interaction):
        channel = interaction_or_ctx.channel
        send = interaction_or_ctx.channel.send
    else:
        channel = interaction_or_ctx.channel
        send = channel.send

    embed = discord.Embed(
        title="🎫 TARGXT • Support",
        description=(
            "Clique ci-dessous pour contacter un membre du staff\n\n"
            "> Ne pas abuser du système de tickets. :warning:\n-# Besoin d'aide ?"
        ),
        color=COLORS["ticket"]
    )
    embed.set_footer(text="TARGXT • Support")

    if mode == "button" and reasons:
        reason = reasons[0]
        view = TicketButtonView(reason["label"], reason.get("description", ""))
        await send(embed=embed, view=view)
    elif mode == "select" and reasons:
        view = TicketReasonSelect(reasons)
        await send(embed=embed, view=view)


# ============================================================
# HELP
# ============================================================
@bot.command(name="help")
async def help_command(ctx: commands.Context):
    embed = discord.Embed(
        title="🛡️ Bot Modération — Aide",
        description=f"Préfixe : {PREFIX}\nToutes les commandes utilisent le préfixe +.",
        color=COLORS["help"],
    )
    embed.set_thumbnail(url=ctx.bot.user.display_avatar.url)

    sections = {
        "⬇️ Sanctions": (
            f"{PREFIX}warn <membre> [raison] — Avertir un membre\n"
            f"{PREFIX}tempmute <membre> <durée> [raison] — Mute temporaire\n"
            f"{PREFIX}unmute <membre> — Enlever le mute\n"
            f"{PREFIX}ban <membre> [raison] — Bannir un membre\n"
            f"{PREFIX}unban <utilisateur#1234> — Débannir\n"
            f"{PREFIX}unbanall — Débannir tous les bannis"
        ),
        "📋 Gestion des sanctions": (
            f"{PREFIX}sanctions <membre> — Voir les sanctions d'un membre\n"
            f"{PREFIX}resetsanctions <membre> — Supprimer TOUTES les sanctions\n"
            f"{PREFIX}removesanction <membre> <numéro> — Retirer une sanction spécifique"
        ),
        "🎉 Giveaways": (
            f"{PREFIX}giveaway — Ouvrir le panneau interactif de giveaway\n"
            f"{PREFIX}gend <id_message> — Terminer un giveaway prématurément\n"
            f"{PREFIX}reroll <id_message> — Re-tirer un giveaway terminé"
        ),
        "🔒 Salon": f"{PREFIX}lock — Verrouiller le salon\n{PREFIX}unlock — Déverrouiller le salon",
        "🔊 Vocal": f"{PREFIX}disconnectall — Déconnecter tout le monde du vocal",
        "🎫 Tickets (style DraftBot)": (
            f"{PREFIX}ticketconfig — Panneau de configuration complet\n"
            f"{PREFIX}ticket [raison] — Créer un ticket (slash +ticket)\n"
            f"{PREFIX}ticketmod ouvrir <membre> [raison] — Ouvrir un ticket pour quelqu'un\n"
            f"{PREFIX}ticketmod ajouter <membre> — Ajouter un membre à un ticket\n"
            f"{PREFIX}ticketmod retirer <membre> — Retirer un membre d'un ticket\n"
            f"{PREFIX}sendticket [#salon] — Envoyer le message d'ouverture\n"
            f"{PREFIX}close — Fermer le ticket"
        ),
        "📋 Divers": f"{PREFIX}clear <nb> — Supprimer des messages",
    }
    for name, value in sections.items():
        embed.add_field(name=name, value=value, inline=False)

    embed.set_footer(text="Bot Modération • Taper +help pour ce message")
    await ctx.send(embed=embed)


# ============================================================
# SANCTIONS — commandes
# ============================================================
@bot.command()
@commands.has_permissions(kick_members=True)
async def warn(ctx: commands.Context, member: discord.Member, *, reason="Aucune raison fournie"):
    SanctionManager.add(ctx.guild.id, member.id, "⚠️ Warn", reason, str(ctx.author))

    embed = discord.Embed(title="⚠️ Avertissement", description=f"{member.mention} a été averti.", color=COLORS["warn"])
    embed.add_field(name="Raison", value=reason, inline=False)
    embed.add_field(name="Modérateur", value=ctx.author.mention, inline=False)
    embed.set_footer(text=datetime.now().strftime("%d/%m/%Y %H:%M"))
    await ctx.send(embed=embed)

    dm = discord.Embed(title=f"⚠️ Avertissement — {ctx.guild.name}", description="Tu as reçu un avertissement.", color=COLORS["warn"])
    dm.add_field(name="Raison", value=reason, inline=False)
    dm.add_field(name="Modérateur", value=str(ctx.author), inline=False)
    dm.set_footer(text=datetime.now().strftime("%d/%m/%Y %H:%M"))
    await send_dm(member, dm)


@bot.command()
@commands.has_permissions(moderate_members=True)
async def tempmute(ctx: commands.Context, member: discord.Member, duration: str, *, reason="Aucune raison fournie"):
    seconds = parse_duration(duration)
    if seconds is None:
        await ctx.send("❌ Durée invalide. Utilise s, m, h, d. Ex: +tempmute @user 10m")
        return

    until = discord.utils.utcnow() + timedelta(seconds=seconds)
    await member.timeout(until, reason=reason)
    SanctionManager.add(ctx.guild.id, member.id, "🔇 Tempmute", f"{duration} — {reason}", str(ctx.author))

    embed = discord.Embed(title="🔇 Mute temporaire", description=f"{member.mention} est réduit au silence.", color=COLORS["tempmute"])
    embed.add_field(name="Durée", value=duration, inline=True)
    embed.add_field(name="Raison", value=reason, inline=True)
    embed.add_field(name="Modérateur", value=ctx.author.mention, inline=False)
    await ctx.send(embed=embed)

    dm = discord.Embed(title=f"🔇 Mute — {ctx.guild.name}", description="Tu es réduit au silence.", color=COLORS["tempmute"])
    dm.add_field(name="Durée", value=duration, inline=True)
    dm.add_field(name="Raison", value=reason, inline=True)
    dm.add_field(name="Expire", value=f"<t:{int((datetime.now() + timedelta(seconds=seconds)).timestamp())}>", inline=False)
    await send_dm(member, dm)


@bot.command()
@commands.has_permissions(moderate_members=True)
async def unmute(ctx: commands.Context, member: discord.Member):
    if not member.is_timed_out():
        await ctx.send(f"ℹ️ {member.mention} n'est pas muet.")
        return

    await member.timeout(None)
    embed = discord.Embed(title="✅ Unmute", description=f"{member.mention} n'est plus muet.", color=COLORS["unmute"])
    embed.add_field(name="Modérateur", value=ctx.author.mention, inline=False)
    await ctx.send(embed=embed)
    await send_dm(member, discord.Embed(
        title=f"✅ Unmute — {ctx.guild.name}", description="Tu n'es plus réduit au silence.", color=COLORS["unmute"],
    ))


@bot.command()
@commands.has_permissions(ban_members=True)
async def ban(ctx: commands.Context, member: discord.Member, *, reason="Aucune raison fournie"):
    await member.ban(reason=reason)
    SanctionManager.add(ctx.guild.id, member.id, "🔨 Ban", reason, str(ctx.author))

    embed = discord.Embed(title="🔨 Bannissement", description=f"{member.mention} a été banni.", color=COLORS["ban"])
    embed.add_field(name="Raison", value=reason, inline=False)
    embed.add_field(name="Modérateur", value=ctx.author.mention, inline=False)
    embed.set_footer(text=datetime.now().strftime("%d/%m/%Y %H:%M"))
    await ctx.send(embed=embed)

    dm = discord.Embed(title=f"🔨 Bannissement — {ctx.guild.name}", description="Tu as été banni.", color=COLORS["ban"])
    dm.add_field(name="Raison", value=reason, inline=False)
    dm.add_field(name="Modérateur", value=str(ctx.author), inline=False)
    dm.set_footer(text=datetime.now().strftime("%d/%m/%Y %H:%M"))
    await send_dm(member, dm)


@bot.event
async def on_member_ban(guild: discord.Guild, user: discord.abc.User):
    reason, moderator = "Aucune raison fournie", "Inconnu"
    try:
        async for entry in guild.audit_logs(action=discord.AuditLogAction.ban, limit=5):
            if entry.target.id == user.id:
                reason = entry.reason or reason
                moderator = str(entry.user)
                break
    except discord.Forbidden:
        pass

    SanctionManager.add(guild.id, user.id, "🔨 Ban (externe)", reason, moderator)

    dm = discord.Embed(title=f"🔨 Bannissement — {guild.name}", description="Tu as été banni.", color=COLORS["ban"])
    dm.add_field(name="Raison", value=reason, inline=False)
    dm.add_field(name="Modérateur", value=moderator, inline=False)
    dm.set_footer(text=datetime.now().strftime("%d/%m/%Y %H:%M"))
    await send_dm(user, dm)


@bot.command()
@commands.has_permissions(ban_members=True)
async def unban(ctx: commands.Context, *, user_input: str):
    async for entry in ctx.guild.bans():
        u = entry.user
        if user_input in (str(u), u.name, str(u.id)):
            await ctx.guild.unban(u)
            await ctx.send(f"✅ {u} débanni.")
            return
    await ctx.send("❌ Utilisateur non trouvé.")


@bot.command()
@commands.has_permissions(ban_members=True)
async def unbanall(ctx: commands.Context):
    banned = [entry async for entry in ctx.guild.bans()]
    if not banned:
        await ctx.send("ℹ️ Aucun banni.")
        return

    count = 0
    for entry in banned:
        try:
            await ctx.guild.unban(entry.user)
            count += 1
        except discord.HTTPException:
            pass
    await ctx.send(f"✅ {count} débanni(s).")


@bot.command()
@commands.has_permissions(move_members=True)
async def disconnectall(ctx: commands.Context):
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send("❌ Tu dois être dans un salon vocal.")
        return

    channel = ctx.author.voice.channel
    count = 0
    for member in channel.members:
        if member != ctx.bot.user:
            try:
                await member.move_to(None)
                count += 1
            except discord.HTTPException:
                pass

    await ctx.send(embed=discord.Embed(
        title="🔊 Déconnexion", description=f"{count} membre(s) déconnecté(s).", color=COLORS["disconnect"],
    ))


@bot.command()
@commands.has_permissions(manage_channels=True)
async def lock(ctx: commands.Context):
    overwrite = ctx.channel.overwrites_for(ctx.guild.default_role)
    overwrite.send_messages = False
    await ctx.channel.set_permissions(ctx.guild.default_role, overwrite=overwrite)
    await ctx.send(embed=discord.Embed(
        title="🔒 Verrouillé", description=f"{ctx.channel.mention} est verrouillé.", color=COLORS["lock"],
    ))


@bot.command()
@commands.has_permissions(manage_channels=True)
async def unlock(ctx: commands.Context):
    overwrite = ctx.channel.overwrites_for(ctx.guild.default_role)
    overwrite.send_messages = None
    await ctx.channel.set_permissions(ctx.guild.default_role, overwrite=overwrite)
    await ctx.send(embed=discord.Embed(
        title="🔓 Déverrouillé", description=f"{ctx.channel.mention} est déverrouillé.", color=COLORS["unlock"],
    ))


@bot.command()
async def sanctions(ctx: commands.Context, member: discord.Member):
    entries = SanctionManager.get_all(ctx.guild.id, member.id)
    if not entries:
        await ctx.send(f"✅ {member.mention} : aucune sanction.")
        return

    embed = discord.Embed(title=f"📋 Sanctions de {member.display_name}", description=f"Total : {len(entries)}", color=COLORS["sanctions"])
    embed.set_thumbnail(url=member.display_avatar.url)
    for i, s in enumerate(entries, 1):
        embed.add_field(
            name=f"{i}. {s['action']} — {s['date']}",
            value=f"Raison : {s['reason']}\nModérateur : {s['moderator']}",
            inline=False,
        )
    await ctx.send(embed=embed)


@bot.command()
@commands.has_permissions(administrator=True)
async def resetsanctions(ctx: commands.Context, member: discord.Member):
    if SanctionManager.reset(ctx.guild.id, member.id):
        await ctx.send(embed=discord.Embed(
            title="🗑️ Sanctions réinitialisées",
            description=f"Toutes les sanctions de {member.mention} ont été supprimées.",
            color=COLORS["sanctions"],
        ))
    else:
        await ctx.send(f"ℹ️ {member.mention} n'a aucune sanction.")


@bot.command()
@commands.has_permissions(administrator=True)
async def removesanction(ctx: commands.Context, member: discord.Member, numero: int):
    removed = SanctionManager.remove_one(ctx.guild.id, member.id, numero)
    if not removed:
        await ctx.send(f"❌ Sanction #{numero} introuvable pour {member.mention}. Utilise `{PREFIX}sanctions {member.display_name}` pour voir les numéros.")
        return

    embed = discord.Embed(title="✅ Sanction retirée", description=f"Sanction #{numero} retirée pour {member.mention}", color=COLORS["sanctions"])
    embed.add_field(name="Action", value=removed["action"], inline=True)
    embed.add_field(name="Raison", value=removed["reason"], inline=True)
    embed.add_field(name="Date", value=removed["date"], inline=True)
    await ctx.send(embed=embed)


@bot.command()
@commands.has_permissions(manage_messages=True)
async def clear(ctx: commands.Context, amount: int):
    if not (1 <= amount <= 100):
        await ctx.send("❌ Entre 1 et 100.")
        return
    deleted = await ctx.channel.purge(limit=amount + 1)
    await ctx.send(f"🗑️ {len(deleted) - 1} supprimé(s).", delete_after=3)


# ============================================================
# GIVEAWAYS — commandes
# ============================================================
@bot.command()
@commands.has_permissions(administrator=True)
async def giveaway(ctx: commands.Context):
    view = GiveawaySetupView(ctx)
    await ctx.send(embed=view.build_embed(), view=view)


@bot.command()
@commands.has_permissions(administrator=True)
async def gend(ctx: commands.Context, message_id: int = None):
    if message_id is None:
        await ctx.send(f"❌ Utilisation : `{PREFIX}gend <id_du_message_giveaway>`")
        return

    g = GiveawayManager.get(message_id)
    if not g:
        await ctx.send("❌ Giveaway introuvable ou déjà terminé.")
        return
    if g["status"] != "running":
        await ctx.send("❌ Ce giveaway est déjà terminé.")
        return

    await GiveawayManager.finish(message_id, g)
    await ctx.send(f"✅ Giveaway terminé ! Résultats dans <#{g['channel_id']}>.")


@bot.command()
@commands.has_permissions(administrator=True)
async def reroll(ctx: commands.Context, message_id: int = None):
    if message_id is None:
        await ctx.send(f"❌ Utilisation : `{PREFIX}reroll <id_du_message_giveaway>`")
        return

    g = GiveawayManager.get(message_id)
    if not g:
        await ctx.send("❌ Giveaway introuvable.")
        return

    channel = ctx.guild.get_channel(g["channel_id"])
    if not channel:
        await ctx.send("❌ Salon du giveaway introuvable.")
        return

    try:
        message = await channel.fetch_message(message_id)
    except discord.NotFound:
        await ctx.send("❌ Message introuvable.")
        return

    winners = await GiveawayManager.draw_winners(message, g["winners"])
    if not winners:
        await ctx.send("❌ Aucun participant à ce giveaway.")
        return

    mentions = ", ".join(w.mention for w in winners)
    await ctx.send(f"🎉 **Nouveau tirage !** Félicitations {mentions} ! Vous avez gagné **{g['prize']}** !")


# ============================================================
# TICKETS — Commandes style DraftBot
# ============================================================
@bot.command()
async def ticket(ctx: commands.Context, *, raison: str = None):
    """Crée un ticket (commande utilisateur)"""
    cfg = TicketConfigManager.get(ctx.guild.id)
    if not cfg.get("category_id"):
        await ctx.send("❌ Tickets non configurés sur ce serveur.")
        return

    motif_obligatoire = cfg.get("motif_obligatoire", False)
    if motif_obligatoire and not raison:
        await ctx.send("❌ Un motif est obligatoire. Utilise : `+ticket <raison>`")
        return

    reason_label = raison or "Demande de support"
    
    # Simule un interaction pour create_ticket
    class FakeInteraction:
        def __init__(self, user, guild, channel):
            self.user = user
            self.guild = guild
            self.channel = channel
            self.response = FakeResponse(ctx)
            self.data = {}
        async def response_send(self, *args, **kwargs):
            pass

    class FakeResponse:
        def __init__(self, ctx):
            self.ctx = ctx
        async def send_message(self, *args, **kwargs):
            pass
        async def defer(self):
            pass

    # On utilise directement la logique sans interaction
    await create_ticket_from_ctx(ctx, reason_label, raison or "")


async def create_ticket_from_ctx(ctx: commands.Context, reason: str, description: str):
    """Version pour commande prefix (+ticket)"""
    guild = ctx.guild
    cfg = TicketConfigManager.get(guild.id)
    
    if not cfg.get("category_id"):
        await ctx.send("❌ Tickets non configurés.")
        return

    validation = cfg.get("validation", True)
    reception_channel_id = cfg.get("reception_channel_id")

    existing = discord.utils.get(guild.text_channels, name=f"ticket-{ctx.author.name.lower().replace(' ', '-')}-{ctx.author.discriminator}")
    if existing:
        await ctx.send("❌ Tu as déjà un ticket ouvert.")
        return

    if validation and reception_channel_id:
        reception_channel = guild.get_channel(reception_channel_id)
        if reception_channel:
            embed = discord.Embed(
                title="🎫 Nouvelle demande de ticket",
                description=f"**{ctx.author.mention}** a demandé un ticket.",
                color=COLORS["ticket_pending"]
            )
            embed.add_field(name="🎯 Sujet", value=reason, inline=True)
            if description:
                embed.add_field(name="📝 Description", value=description, inline=False)
            embed.add_field(name="👤 Utilisateur", value=f"{ctx.author} ({ctx.author.id})", inline=False)
            embed.set_thumbnail(url=ctx.author.display_avatar.url)
            embed.set_footer(text="Un modérateur va traiter ta demande")

            content = None
            if cfg.get("mention_moderators", False):
                staff_roles = cfg.get("staff_roles", [])
                mentions = [guild.get_role(rid).mention for rid in staff_roles if guild.get_role(rid)]
                if mentions:
                    content = " ".join(mentions)

            await reception_channel.send(
                content=content,
                embed=embed,
                view=TicketAcceptDenyView(ctx.author, reason, description)
            )

            await ctx.send(embed=discord.Embed(
                title="✅ Demande envoyée",
                description="Ta demande de ticket a été envoyée. Un modérateur va la traiter.",
                color=COLORS["ticket_pending"]
            ))
            return

    if validation and not reception_channel_id:
        await ctx.send("❌ Le salon de réception n'est pas configuré.")
        return

    # Création directe
    category = discord.utils.get(guild.categories, id=cfg.get("category_id"))
    if not category:
        await ctx.send("❌ Catégorie introuvable.")
        return

    staff_roles = cfg.get("staff_roles", [])
    chan_name = f"ticket-{ctx.author.name.lower().replace(' ', '-')}-{ctx.author.discriminator}"

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        ctx.author: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
    }
    for role_id in staff_roles:
        role = guild.get_role(role_id)
        if role:
            overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

    chan = await guild.create_text_channel(
        name=chan_name, category=category, overwrites=overwrites,
        reason=f"Ticket de {ctx.author}"
    )

    embed = discord.Embed(
        title="🎫 Ticket ouvert",
        description=cfg.get("ticket_message", "🎫 **Bienvenue !** Un membre du staff va vous répondre."),
        color=COLORS["ticket"]
    )
    embed.add_field(name="👤 Utilisateur", value=ctx.author.mention, inline=True)
    embed.add_field(name="🎯 Sujet", value=reason, inline=True)
    if description:
        embed.add_field(name="📝 Description", value=description, inline=False)

    staff_mention = ""
    if cfg.get("mention_moderators", False) and staff_roles:
        mentions = [guild.get_role(rid).mention for rid in staff_roles if guild.get_role(rid)]
        staff_mention = ", ".join(mentions) + " — "

    await chan.send(
        content=f"{ctx.author.mention} — {staff_mention}",
        embed=embed,
        view=TicketCloseView()
    )

    await ctx.send(f"✅ Ticket créé : {chan.mention}")


@bot.command()
@commands.has_permissions(manage_channels=True)
async def ticketmod(ctx: commands.Context, action: str, member: discord.Member = None):
    """Modération des tickets : ouvrir, ajouter, retirer (style DraftBot)"""
    if action == "ouvrir":
        if not member:
            await ctx.send("❌ Utilisation : `+ticketmod ouvrir <membre>`")
            return
        
        # Stocke le membre pour le modal
        ctx._ticketmod_member = member
        
        class TicketModModal(discord.ui.Modal, title="📝 Ouvrir un ticket"):
            raison = TextInput(label="Raison", placeholder="Support technique...", max_length=100)
            description = TextInput(label="Description (optionnel)", placeholder="Détails...", max_length=500, required=False, style=discord.TextStyle.short)
            
            async def on_submit(self, interaction: discord.Interaction):
                reason = self.raison.value.strip() or "Demande de support"
                desc = self.description.value.strip() or ""
                await create_ticket_for_user(interaction, member, reason, desc)
        
        await ctx.send_modal(TicketModModal())
        
    elif action == "ajouter":
        if not member:
            await ctx.send("❌ Utilisation : `+ticketmod ajouter <membre>`")
            return
        if not ctx.channel.name.startswith("ticket-"):
            await ctx.send("❌ Utilise cette commande dans un salon de ticket.")
            return
        
        await ctx.channel.set_permissions(member, view_channel=True, send_messages=True, read_message_history=True)
        await ctx.send(f"✅ {member.mention} a été ajouté au ticket.")
        
        embed = discord.Embed(
            title="👤 Membre ajouté",
            description=f"{member.mention} a été ajouté par {ctx.author.mention}",
            color=COLORS["ticket"]
        )
        await ctx.channel.send(embed=embed)
        
    elif action == "retirer":
        if not member:
            await ctx.send("❌ Utilisation : `+ticketmod retirer <membre>`")
            return
        if not ctx.channel.name.startswith("ticket-"):
            await ctx.send("❌ Utilise cette commande dans un salon de ticket.")
            return
        
        await ctx.channel.set_permissions(member, overwrite=None)
        await ctx.send(f"✅ {member.mention} a été retiré du ticket.")
        
        embed = discord.Embed(
            title="👤 Membre retiré",
            description=f"{member.mention} a été retiré par {ctx.author.mention}",
            color=COLORS["ticket"]
        )
        await ctx.channel.send(embed=embed)
    else:
        await ctx.send("❌ Actions : `ouvrir`, `ajouter`, `retirer`")


async def create_ticket_for_user(interaction: discord.Interaction, target: discord.Member, reason: str, description: str):
    """Ouvre un ticket pour un membre spécifique (commande mod)"""
    guild = interaction.guild
    cfg = TicketConfigManager.get(guild.id)
    
    if not cfg.get("category_id"):
        await interaction.response.send_message("❌ Tickets non configurés.", ephemeral=True)
        return

    existing = discord.utils.get(guild.text_channels, name=f"ticket-{target.name.lower().replace(' ', '-')}-{target.discriminator}")
    if existing:
        await interaction.response.send_message("❌ Ce membre a déjà un ticket.", ephemeral=True)
        return

    category = discord.utils.get(guild.categories, id=cfg.get("category_id"))
    if not category:
        await interaction.response.send_message("❌ Catégorie introuvable.", ephemeral=True)
        return

    staff_roles = cfg.get("staff_roles", [])
    chan_name = f"ticket-{target.name.lower().replace(' ', '-')}-{target.discriminator}"

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        target: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
    }
    for role_id in staff_roles:
        role = guild.get_role(role_id)
        if role:
            overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

    chan = await guild.create_text_channel(
        name=chan_name, category=category, overwrites=overwrites,
        reason=f"Ticket ouvert par {interaction.user} pour {target}"
    )

    embed = discord.Embed(
        title="🎫 Ticket ouvert (par le staff)",
        description=cfg.get("ticket_message", "🎫 **Bienvenue !** Un membre du staff va vous répondre."),
        color=COLORS["ticket"]
    )
    embed.add_field(name="👤 Utilisateur", value=target.mention, inline=True)
    embed.add_field(name="🎯 Sujet", value=reason, inline=True)
    embed.add_field(name="🛡️ Ouvert par", value=interaction.user.mention, inline=True)
    if description:
        embed.add_field(name="📝 Description", value=description, inline=False)

    staff_mention = ""
    if cfg.get("mention_moderators", False) and staff_roles:
        mentions = [guild.get_role(rid).mention for rid in staff_roles if guild.get_role(rid)]
        staff_mention = ", ".join(mentions) + " — "

    await chan.send(
        content=f"{target.mention} — {staff_mention}",
        embed=embed,
        view=TicketCloseView()
    )

    await interaction.response.send_message(f"✅ Ticket créé pour {target.mention} : {chan.mention}", ephemeral=True)


# ============================================================
# TICKETS — Commandes utilisateur
# ============================================================
@bot.command()
@commands.has_permissions(manage_channels=True)
async def close(ctx: commands.Context):
    """Ferme le ticket actuel"""
    if not ctx.channel.name.startswith("ticket-"):
        await ctx.send("❌ Cette commande doit être utilisée dans un salon de ticket.")
        return
    
    cfg = TicketConfigManager.get(ctx.guild.id)
    has_perm = ctx.author.guild_permissions.administrator or \
               any(role.id in cfg.get("staff_roles", []) for role in ctx.author.roles) or \
               ctx.channel.name.endswith(ctx.author.discriminator)

    if not has_perm:
        await ctx.send("❌ Tu n'as pas la permission.")
        return
    
    embed = discord.Embed(
        title="🔒 Fermeture",
        description="Fermeture dans 5 secondes...",
        color=discord.Color.red()
    )
    await ctx.send(embed=embed)
    await asyncio.sleep(5)
    await ctx.channel.delete(reason=f"Fermé par {ctx.author}")


@bot.command()
@commands.has_permissions(administrator=True)
async def ticketconfig(ctx: commands.Context):
    """Panneau de configuration des tickets (style DraftBot)"""
    embed = discord.Embed(
        title="🛠️ Configuration des tickets",
        description="Configure le système de tickets comme DraftBot.",
        color=COLORS["ticket"]
    )
    embed.add_field(name="📂 Catégorie", value="Où les tickets sont créés", inline=False)
    embed.add_field(name="📥 Salon réception", value="Où les demandes arrivent (validation)", inline=False)
    embed.add_field(name="👥 Rôles staff", value="Qui peut gérer les tickets", inline=False)
    embed.add_field(name="⚙️ Options", value="Validation, mentions, suppression auto, motif obligatoire", inline=False)
    embed.add_field(name="📋 Raisons", value="Ajoute des raisons pour le sélecteur", inline=False)
    embed.set_footer(text="DraftBot style • +help pour l'aide")
    
    await ctx.send(embed=embed, view=TicketConfigPanelView(ctx.guild))


@bot.command()
@commands.has_permissions(administrator=True)
async def sendticket(ctx: commands.Context, channel: discord.TextChannel = None):
    """Envoie le message d'ouverture de ticket"""
    channel = channel or ctx.channel
    cfg = TicketConfigManager.get(ctx.guild.id)
    
    if not cfg.get("category_id"):
        await ctx.send("❌ Configure d'abord avec `+ticketconfig`.")
        return

    reasons = cfg.get("reasons", [])
    if not reasons:
        await ctx.send("📝 Ajoute d'abord des raisons avec `+ticketconfig` (bouton 'Ajouter une raison').")
        return

    # Par défaut : sélecteur si plusieurs raisons, bouton si une seule
    if len(reasons) == 1:
        view = TicketButtonView(reasons[0]["label"], reasons[0].get("description", ""))
        await channel.send(embed=build_ticket_intro_embed(), view=view)
    else:
        view = TicketReasonSelect(reasons)
        await channel.send(embed=build_ticket_intro_embed(), view=view)

    if channel != ctx.channel:
        await ctx.send(f"✅ Message d'ouverture envoyé dans {channel.mention}.")


def build_ticket_intro_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🎫 TARGXT • Support",
        description=(
            "Clique ci-dessous pour contacter un membre du staff\n\n"
            "> Ne pas abuser du système de tickets. :warning:\n-# Besoin d'aide ?"
        ),
        color=COLORS["ticket"]
    )
    embed.set_footer(text="TARGXT • Support")
    return embed


# ============================================================
# ERREURS
# ============================================================
@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ Tu n'as pas les permissions nécessaires.")
    elif isinstance(error, commands.MemberNotFound):
        await ctx.send("❌ Membre introuvable.")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("❌ Argument invalide.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Argument manquant. Utilise `{PREFIX}help` pour voir la syntaxe.")
    else:
        await ctx.send(f"❌ Erreur : {error}")


# ============================================================
# READY / LANCEMENT
# ============================================================
@bot.event
async def on_ready():
    print(f"✅ Bot connecté : {bot.user} ({bot.user.id})")
    print(f"📡 Serveurs : {len(bot.guilds)}")
    print(f"⚡ Préfixe : {PREFIX}")

    # Re-enregistrer les vues persistantes
    bot.add_view(TicketCloseView())
    bot.add_view(TicketAcceptDenyView(None, "", ""))  # Views génériques
    bot.add_view(TicketButtonView("Support", ""))
    
    bot.loop.create_task(GiveawayManager.loop())


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit(
            "❌ Aucun token trouvé. Définis la variable d'environnement DISCORD_TOKEN "
            "avant de lancer le bot (et régénère ton token s'il a déjà été exposé)."
        )
    bot.run(TOKEN)