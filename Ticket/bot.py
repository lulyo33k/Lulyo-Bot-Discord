import discord
from discord import app_commands
from discord.ext import commands
import asyncio

# ==================== CONFIGURATION ====================
TOKEN = "TON_BOT_DISCORD"  # Remplace par ton token

HELPER_ROLE_NAME = "Helper"          # Nom exact du rôle Helper sur ton serveur
ADMIN_ROLE_NAME = "Admin"            # Nom exact du rôle Admin sur ton serveur
TICKET_CATEGORY_NAME = "Tickets"     # Catégorie où seront créés les salons
# =========================================================

intents = discord.Intents.default()
intents.guilds = True
intents.members = True


class TicketBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        # Enregistrement des vues persistantes (survivent au redémarrage du bot)
        self.add_view(TicketView())
        self.add_view(CloseTicketView())
        await self.tree.sync()
        print("✅ Commandes slash synchronisées.")


bot = TicketBot()


# ==================== VUE : BOUTON DE CRÉATION ====================
class TicketView(discord.ui.View):
    def __init__(self, label: str = "🎫 Créer un ticket"):
        super().__init__(timeout=None)
        button = discord.ui.Button(
            label=label,
            style=discord.ButtonStyle.green,
            custom_id="create_ticket_button"  # ID fixe -> indispensable pour la persistance
        )
        button.callback = self.create_ticket
        self.add_item(button)

    async def create_ticket(self, interaction: discord.Interaction):
        guild = interaction.guild
        member = interaction.user

        # Vérifie si l'utilisateur a déjà un ticket ouvert
        existing = discord.utils.get(guild.text_channels, name=f"ticket-{member.name.lower()}")
        if existing:
            await interaction.response.send_message(
                f"❌ Tu as déjà un ticket ouvert : {existing.mention}", ephemeral=True
            )
            return

        # Récupère ou crée la catégorie
        category = discord.utils.get(guild.categories, name=TICKET_CATEGORY_NAME)
        if category is None:
            category = await guild.create_category(TICKET_CATEGORY_NAME)

        helper_role = discord.utils.get(guild.roles, name=HELPER_ROLE_NAME)
        admin_role = discord.utils.get(guild.roles, name=ADMIN_ROLE_NAME)
        owner = guild.owner

        # Permissions du salon : everyone bloqué, seuls les concernés voient le salon
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True, attach_files=True
            ),
        }
        if helper_role:
            overwrites[helper_role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            )
        if admin_role:
            overwrites[admin_role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            )
        if owner:
            overwrites[owner] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            )

        channel = await guild.create_text_channel(
            name=f"ticket-{member.name}",
            category=category,
            overwrites=overwrites,
            topic=f"Ticket de {member} | ID: {member.id}"
        )

        embed = discord.Embed(
            title="🎫 Nouveau ticket",
            description=(
                f"Bienvenue {member.mention} !\n"
                "Un membre du staff va bientôt te répondre.\n\n"
                "Clique sur le bouton ci-dessous pour fermer ce ticket."
            ),
            color=discord.Color.blurple()
        )

        await channel.send(content=member.mention, embed=embed, view=CloseTicketView())

        await interaction.response.send_message(
            f"✅ Ton ticket a été créé : {channel.mention}", ephemeral=True
        )


# ==================== VUE : BOUTON DE FERMETURE ====================
class CloseTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        button = discord.ui.Button(
            label="🔒 Fermer le ticket",
            style=discord.ButtonStyle.red,
            custom_id="close_ticket_button"
        )
        button.callback = self.close_ticket
        self.add_item(button)

    async def close_ticket(self, interaction: discord.Interaction):
        await interaction.response.send_message("🔒 Fermeture du ticket dans 5 secondes...")
        await asyncio.sleep(5)
        await interaction.channel.delete()


# ==================== VÉRIFICATION ADMIN/PROPRIO ====================
def is_admin_or_owner():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.id == interaction.guild.owner_id:
            return True
        if interaction.user.guild_permissions.administrator:
            return True
        return False
    return app_commands.check(predicate)


# ==================== COMMANDE /setup-ticket ====================
@bot.tree.command(name="setup-ticket", description="Configurer le message de création de ticket")
@is_admin_or_owner()
@app_commands.describe(
    titre="Titre de l'embed",
    description="Description de l'embed",
    texte_bouton="Texte affiché sur le bouton",
    image_url="URL d'une image à afficher (optionnel)"
)
async def setup_ticket(
    interaction: discord.Interaction,
    titre: str,
    description: str,
    texte_bouton: str,
    image_url: str = None
):
    embed = discord.Embed(title=titre, description=description, color=discord.Color.green())
    if image_url:
        embed.set_image(url=image_url)

    view = TicketView(label=texte_bouton)
    await interaction.channel.send(embed=embed, view=view)
    await interaction.response.send_message("✅ Message de ticket envoyé avec succès !", ephemeral=True)


@setup_ticket.error
async def setup_ticket_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message(
            "❌ Tu dois être administrateur ou propriétaire du serveur pour utiliser cette commande.",
            ephemeral=True
        )
    else:
        await interaction.response.send_message(f"❌ Une erreur est survenue : {error}", ephemeral=True)


# ==================== EVENT ====================
@bot.event
async def on_ready():
    print(f"✅ Connecté en tant que {bot.user} (ID: {bot.user.id})")


bot.run(TOKEN)