import discord
from discord import app_commands
from discord.ext import commands
import os
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# Stockage en mémoire des sondages : {message_id: poll_data}
polls = {}


# ---------- VUE / BOUTONS ----------

class PollButton(discord.ui.Button):
    def __init__(self, key: str):
        super().__init__(
            label=key,
            style=discord.ButtonStyle.primary,
            custom_id=f"poll_{key}"
        )
        self.key = key

    async def callback(self, interaction: discord.Interaction):
        message_id = interaction.message.id
        poll = polls.get(message_id)

        if not poll:
            await interaction.response.send_message(
                "❌ Ce sondage n'est plus disponible (le bot a peut-être redémarré).",
                ephemeral=True
            )
            return

        user_id = interaction.user.id

        # On retire le vote précédent de l'utilisateur (s'il existe)
        for opt in poll["options"].values():
            opt["votes"].discard(user_id)

        # On ajoute le nouveau vote
        poll["options"][self.key]["votes"].add(user_id)

        embeds = build_embeds(poll)
        await interaction.response.edit_message(embeds=embeds, view=self.view)


class PollView(discord.ui.View):
    def __init__(self, option_keys):
        super().__init__(timeout=None)
        for key in option_keys:
            self.add_item(PollButton(key))


# ---------- CONSTRUCTION DES EMBEDS ----------

def build_embeds(poll):
    total_votes = sum(len(opt["votes"]) for opt in poll["options"].values())

    main_embed = discord.Embed(title=f"📊 {poll['title']}", color=discord.Color.blurple())

    lines = []
    for key, opt in poll["options"].items():
        votes = len(opt["votes"])
        percent = (votes / total_votes * 100) if total_votes > 0 else 0
        bar_len = int(percent // 5)  # barre sur 20 blocs
        bar = "█" * bar_len + "░" * (20 - bar_len)
        lines.append(
            f"**{key}. {opt['label']}**\n{bar} `{percent:.1f}%` ({votes} vote{'s' if votes != 1 else ''})"
        )

    main_embed.description = "\n\n".join(lines)
    main_embed.set_footer(text=f"Total : {total_votes} vote(s) • Cliquez sur un bouton pour voter")

    embeds = [main_embed]

    # Un embed supplémentaire par option qui a une image
    for key, opt in poll["options"].items():
        if opt["image"]:
            img_embed = discord.Embed(
                title=f"Option {key} : {opt['label']}",
                color=discord.Color.blurple()
            )
            img_embed.set_image(url=opt["image"])
            embeds.append(img_embed)

    return embeds


# ---------- COMMANDE SLASH ----------

@bot.tree.command(name="sondage", description="Créer un sondage avec jusqu'à 7 options")
@app_commands.describe(
    titre="Titre du sondage",
    option_a="Option A (obligatoire)",
    option_b="Option B (obligatoire)",
    option_c="Option C (optionnelle)",
    option_d="Option D (optionnelle)",
    option_e="Option E (optionnelle)",
    option_f="Option F (optionnelle)",
    option_g="Option G (optionnelle)",
    image_a="Image pour l'option A",
    image_b="Image pour l'option B",
    image_c="Image pour l'option C",
    image_d="Image pour l'option D",
    image_e="Image pour l'option E",
    image_f="Image pour l'option F",
    image_g="Image pour l'option G",
)
async def sondage(
    interaction: discord.Interaction,
    titre: str,
    option_a: str,
    option_b: str,
    option_c: str = None,
    option_d: str = None,
    option_e: str = None,
    option_f: str = None,
    option_g: str = None,
    image_a: discord.Attachment = None,
    image_b: discord.Attachment = None,
    image_c: discord.Attachment = None,
    image_d: discord.Attachment = None,
    image_e: discord.Attachment = None,
    image_f: discord.Attachment = None,
    image_g: discord.Attachment = None,
):
    raw_options = {
        "A": (option_a, image_a),
        "B": (option_b, image_b),
        "C": (option_c, image_c),
        "D": (option_d, image_d),
        "E": (option_e, image_e),
        "F": (option_f, image_f),
        "G": (option_g, image_g),
    }

    options = {}
    for key, (label, image) in raw_options.items():
        if label:  # on ignore les options non remplies
            options[key] = {
                "label": label,
                "votes": set(),
                "image": image.url if image else None
            }

    poll = {"title": titre, "options": options}
    embeds = build_embeds(poll)
    view = PollView(options.keys())

    await interaction.response.send_message(embeds=embeds, view=view)
    message = await interaction.original_response()

    polls[message.id] = poll


# ---------- ÉVÉNEMENTS ----------

@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"✅ Connecté en tant que {bot.user}")


bot.run(TOKEN)