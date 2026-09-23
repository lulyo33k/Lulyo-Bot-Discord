import os
import re
import json
import asyncio
import xml.etree.ElementTree as ET

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Configuration / variables d'environnement
# ---------------------------------------------------------------------------
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID")
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET")
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")  # optionnelle mais recommandée

CONFIG_FILE = "config.json"
config_lock = asyncio.Lock()

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

YT_COOKIES = {"CONSENT": "YES+cb.20220419-08-p0.fr+FX+410"}

DEFAULT_YT_MESSAGE = "📢 Une nouvelle vidéo vient de sortir sur **{channel}** !"
DEFAULT_TWITCH_MESSAGE = "🔴 **{channel}** est en live sur Twitch, viens jeter un œil !"


# ---------------------------------------------------------------------------
# Gestion du fichier de configuration (persistance par serveur)
# ---------------------------------------------------------------------------
def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_config(data: dict) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)


config = load_config()


def get_guild_conf(guild_id: int) -> dict:
    gid = str(guild_id)
    if gid not in config:
        config[gid] = {
            "announce_channel": None,
            "youtube": {
                "channel_id": None,
                "url": None,
                "last_video_id": None,
                "channel_title": None,
                "avatar_url": None,
                "banner_url": None,
                "message_template": DEFAULT_YT_MESSAGE,
            },
            "twitch": {
                "username": None,
                "is_live": False,
                "display_name": None,
                "avatar_url": None,
                "offline_image_url": None,
                "message_template": DEFAULT_TWITCH_MESSAGE,
            },
        }
    else:
        # Rétro-compatibilité si des champs manquent dans un ancien config.json
        config[gid].setdefault("youtube", {})
        config[gid]["youtube"].setdefault("channel_title", None)
        config[gid]["youtube"].setdefault("avatar_url", None)
        config[gid]["youtube"].setdefault("banner_url", None)
        config[gid]["youtube"].setdefault("message_template", DEFAULT_YT_MESSAGE)

        config[gid].setdefault("twitch", {})
        config[gid]["twitch"].setdefault("display_name", None)
        config[gid]["twitch"].setdefault("avatar_url", None)
        config[gid]["twitch"].setdefault("offline_image_url", None)
        config[gid]["twitch"].setdefault("message_template", DEFAULT_TWITCH_MESSAGE)

    return config[gid]


# ---------------------------------------------------------------------------
# Discord bot
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

twitch_token_cache = {"access_token": None}


class LinkView(discord.ui.View):
    """Vue contenant un unique bouton lien."""

    def __init__(self, url: str, label: str, emoji: str | None = None):
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Button(
                label=label,
                url=url,
                style=discord.ButtonStyle.link,
                emoji=emoji,
            )
        )


def safe_format(template: str, **kwargs) -> str:
    """Formate un template en ignorant les placeholders inconnus / cassés."""
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError, ValueError):
        return template


# ---------------------------------------------------------------------------
# Fonctions utilitaires : détection de lien
# ---------------------------------------------------------------------------
def detect_link_type(url: str) -> str | None:
    """Détecte si l'URL est une vidéo YouTube, une chaîne Twitch, ou inconnue."""
    url = url.strip()
    if re.search(r"(youtube\.com/watch\?v=|youtu\.be/)", url):
        return "youtube"
    if re.search(r"twitch\.tv/", url):
        return "twitch"
    return None


def extract_youtube_video_id(url: str) -> str | None:
    """Extrait l'ID de vidéo depuis n'importe quel format d'URL YouTube."""
    match = re.search(r"(?:v=|youtu\.be/)([\w-]{11})", url)
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# Fonctions utilitaires : Twitch
# ---------------------------------------------------------------------------
async def get_twitch_token(session: aiohttp.ClientSession) -> str | None:
    if twitch_token_cache["access_token"]:
        return twitch_token_cache["access_token"]

    url = "https://id.twitch.tv/oauth2/token"
    params = {
        "client_id": TWITCH_CLIENT_ID,
        "client_secret": TWITCH_CLIENT_SECRET,
        "grant_type": "client_credentials",
    }
    try:
        async with session.post(url, params=params) as resp:
            if resp.status != 200:
                print(f"[Twitch] Erreur récupération token : {resp.status}")
                return None
            data = await resp.json()
            twitch_token_cache["access_token"] = data.get("access_token")
            return twitch_token_cache["access_token"]
    except Exception as e:
        print(f"[Twitch] Exception récupération token : {e}")
        return None


async def is_twitch_live(session: aiohttp.ClientSession, username: str) -> dict | None:
    if not TWITCH_CLIENT_ID or not TWITCH_CLIENT_SECRET:
        return None

    token = await get_twitch_token(session)
    if not token:
        return None

    headers = {
        "Client-ID": TWITCH_CLIENT_ID,
        "Authorization": f"Bearer {token}",
    }
    url = f"https://api.twitch.tv/helix/streams?user_login={username}"

    try:
        async with session.get(url, headers=headers) as resp:
            if resp.status == 401:
                twitch_token_cache["access_token"] = None
                return await is_twitch_live(session, username)
            if resp.status != 200:
                print(f"[Twitch] Erreur API streams : {resp.status}")
                return None
            data = await resp.json()
            streams = data.get("data", [])
            return streams[0] if streams else None
    except Exception as e:
        print(f"[Twitch] Exception vérification live : {e}")
        return None


async def get_twitch_user_info(session: aiohttp.ClientSession, username: str) -> dict | None:
    """Récupère avatar, bannière hors-ligne et nom d'affichage."""
    token = await get_twitch_token(session)
    if not token:
        return None

    headers = {
        "Client-ID": TWITCH_CLIENT_ID,
        "Authorization": f"Bearer {token}",
    }
    url = f"https://api.twitch.tv/helix/users?login={username}"

    try:
        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            items = data.get("data", [])
            if not items:
                return None
            user = items[0]
            return {
                "display_name": user.get("display_name"),
                "avatar_url": user.get("profile_image_url"),
                "offline_image_url": user.get("offline_image_url") or None,
            }
    except Exception as e:
        print(f"[Twitch] Exception récupération user info : {e}")
        return None


def extract_twitch_username(url: str) -> str | None:
    url = url.strip()
    match = re.search(r"twitch\.tv/([a-zA-Z0-9_]+)", url)
    if match:
        return match.group(1)
    if re.fullmatch(r"[a-zA-Z0-9_]+", url):
        return url
    return None


async def get_twitch_stream_by_url(session: aiohttp.ClientSession, url: str):
    """Retourne (username, user_info, stream_info) à partir d'une URL Twitch."""
    username = extract_twitch_username(url)
    if not username:
        return None, None, None

    user_info = await get_twitch_user_info(session, username)
    stream = await is_twitch_live(session, username)

    return username, user_info, stream


# ---------------------------------------------------------------------------
# Fonctions utilitaires : YouTube
# ---------------------------------------------------------------------------
def extract_youtube_handle_or_id(url: str) -> tuple[str | None, str | None]:
    url = url.strip()

    channel_match = re.search(r"channel/(UC[\w-]+)", url)
    if channel_match:
        return None, channel_match.group(1)

    handle_match = re.search(r"youtube\.com/@([\w.-]+)", url)
    if handle_match:
        return handle_match.group(1), None

    custom_match = re.search(r"youtube\.com/c/([\w.-]+)", url)
    if custom_match:
        return custom_match.group(1), None

    user_match = re.search(r"youtube\.com/user/([\w.-]+)", url)
    if user_match:
        return user_match.group(1), None

    if url.startswith("@"):
        return url[1:], None

    if re.fullmatch(r"[\w.-]+", url):
        return url, None

    return None, None


async def get_channel_id_via_api(session: aiohttp.ClientSession, handle: str) -> str | None:
    if not YOUTUBE_API_KEY:
        return None

    api_url = "https://www.googleapis.com/youtube/v3/channels"

    params = {"part": "id", "forHandle": handle, "key": YOUTUBE_API_KEY}
    try:
        async with session.get(api_url, params=params) as resp:
            if resp.status == 200:
                data = await resp.json()
                items = data.get("items", [])
                if items:
                    return items[0]["id"]
    except Exception as e:
        print(f"[YouTube API] Erreur forHandle : {e}")

    params = {"part": "id", "forUsername": handle, "key": YOUTUBE_API_KEY}
    try:
        async with session.get(api_url, params=params) as resp:
            if resp.status == 200:
                data = await resp.json()
                items = data.get("items", [])
                if items:
                    return items[0]["id"]
    except Exception as e:
        print(f"[YouTube API] Erreur forUsername : {e}")

    search_url = "https://www.googleapis.com/youtube/v3/search"
    params = {
        "part": "snippet",
        "q": handle,
        "type": "channel",
        "maxResults": 1,
        "key": YOUTUBE_API_KEY,
    }
    try:
        async with session.get(search_url, params=params) as resp:
            if resp.status == 200:
                data = await resp.json()
                items = data.get("items", [])
                if items:
                    return items[0]["snippet"]["channelId"]
    except Exception as e:
        print(f"[YouTube API] Erreur search : {e}")

    return None


async def get_channel_id_via_scraping(session: aiohttp.ClientSession, handle_or_url: str) -> str | None:
    url = handle_or_url
    if not url.startswith("http"):
        if not url.startswith("@"):
            url = "@" + url
        url = f"https://www.youtube.com/{url}"

    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    urls_to_try = [url]
    if not url.rstrip("/").endswith("/about"):
        urls_to_try.append(url.rstrip("/") + "/about")

    patterns = [
        r'"channelId":"(UC[\w-]+)"',
        r'"externalId":"(UC[\w-]+)"',
        r'channel/(UC[\w-]+)',
        r'"browseId":"(UC[\w-]+)"',
    ]

    for target_url in urls_to_try:
        try:
            async with session.get(
                target_url,
                headers=headers,
                cookies=YT_COOKIES,
                allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    continue
                html = await resp.text()
                for pattern in patterns:
                    m = re.search(pattern, html)
                    if m:
                        return m.group(1)
        except Exception as e:
            print(f"[YouTube Scraping] Erreur sur {target_url} : {e}")

    return None


async def extract_youtube_channel_id(session: aiohttp.ClientSession, url: str) -> str | None:
    handle, channel_id = extract_youtube_handle_or_id(url)

    if channel_id:
        return channel_id

    if not handle:
        return None

    result = await get_channel_id_via_api(session, handle)
    if result:
        return result

    result = await get_channel_id_via_scraping(session, handle)
    if result:
        return result

    return None


async def get_channel_branding(session: aiohttp.ClientSession, channel_id: str) -> dict:
    """Récupère titre, avatar et bannière d'une chaîne YouTube (nécessite YOUTUBE_API_KEY)."""
    result = {"channel_title": None, "avatar_url": None, "banner_url": None}

    if not YOUTUBE_API_KEY:
        return result

    api_url = "https://www.googleapis.com/youtube/v3/channels"
    params = {
        "part": "snippet,brandingSettings",
        "id": channel_id,
        "key": YOUTUBE_API_KEY,
    }

    try:
        async with session.get(api_url, params=params) as resp:
            if resp.status != 200:
                return result
            data = await resp.json()
            items = data.get("items", [])
            if not items:
                return result

            item = items[0]
            snippet = item.get("snippet", {})
            branding = item.get("brandingSettings", {}).get("image", {})

            result["channel_title"] = snippet.get("title")

            thumbnails = snippet.get("thumbnails", {})
            avatar = (
                thumbnails.get("high", {}).get("url")
                or thumbnails.get("medium", {}).get("url")
                or thumbnails.get("default", {}).get("url")
            )
            result["avatar_url"] = avatar
            result["banner_url"] = branding.get("bannerExternalUrl")

    except Exception as e:
        print(f"[YouTube API] Erreur récupération branding : {e}")

    return result


async def get_latest_youtube_video(session: aiohttp.ClientSession, channel_id: str):
    """Retourne (video_id, titre, lien, thumbnail_url, channel_name_from_feed)."""
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return None, None, None, None, None
            text = await resp.text()

        root = ET.fromstring(text)
        ns = {
            "atom": "http://www.w3.org/2005/Atom",
            "yt": "http://www.youtube.com/xml/schemas/2015",
            "media": "http://search.yahoo.com/mrss/",
        }

        feed_author = root.find("atom:author/atom:name", ns)
        channel_name = feed_author.text if feed_author is not None else None

        entry = root.find("atom:entry", ns)
        if entry is None:
            return None, None, None, None, channel_name

        video_id = entry.find("yt:videoId", ns).text
        title = entry.find("atom:title", ns).text
        link = entry.find("atom:link", ns).attrib.get("href")

        thumbnail_url = f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg"

        return video_id, title, link, thumbnail_url, channel_name
    except Exception as e:
        print(f"[YouTube RSS] Erreur récupération vidéo : {e}")
        return None, None, None, None, None


async def get_youtube_video_info(session: aiohttp.ClientSession, video_id: str) -> dict | None:
    """Récupère titre, chaîne, thumbnail via l'API (si dispo) ou en fallback via oEmbed."""
    if YOUTUBE_API_KEY:
        api_url = "https://www.googleapis.com/youtube/v3/videos"
        params = {"part": "snippet", "id": video_id, "key": YOUTUBE_API_KEY}
        try:
            async with session.get(api_url, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    items = data.get("items", [])
                    if items:
                        snippet = items[0]["snippet"]
                        return {
                            "title": snippet.get("title"),
                            "channel_title": snippet.get("channelTitle"),
                            "channel_id": snippet.get("channelId"),
                            "thumbnail": f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg",
                            "url": f"https://www.youtube.com/watch?v={video_id}",
                        }
        except Exception as e:
            print(f"[YouTube API] Erreur récupération vidéo : {e}")

    # Fallback : oEmbed (ne nécessite pas de clé API)
    oembed_url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
    try:
        async with session.get(oembed_url) as resp:
            if resp.status == 200:
                data = await resp.json()
                return {
                    "title": data.get("title"),
                    "channel_title": data.get("author_name"),
                    "channel_id": None,
                    "thumbnail": f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg",
                    "url": f"https://www.youtube.com/watch?v={video_id}",
                }
    except Exception as e:
        print(f"[YouTube oEmbed] Erreur : {e}")

    return None


# ---------------------------------------------------------------------------
# Envoi des annonces (embeds améliorés)
# ---------------------------------------------------------------------------
async def send_youtube_announcement(channel, yt_conf: dict, title: str, link: str, thumbnail_url: str | None):
    channel_name = yt_conf.get("channel_title") or "cette chaîne"
    message = safe_format(
        yt_conf.get("message_template") or DEFAULT_YT_MESSAGE,
        channel=channel_name,
        title=title,
        url=link,
    )

    embed = discord.Embed(
        title=title,
        description=message,
        color=discord.Color.red(),
    )

    avatar_url = yt_conf.get("avatar_url")
    banner_url = yt_conf.get("banner_url")

    embed.set_author(
        name=channel_name,
        icon_url=avatar_url if avatar_url else discord.Embed.Empty,
        url=yt_conf.get("url") or discord.Embed.Empty,
    )

    if banner_url:
        embed.set_image(url=banner_url)
        if thumbnail_url:
            embed.set_thumbnail(url=thumbnail_url)
    elif thumbnail_url:
        embed.set_image(url=thumbnail_url)
        if avatar_url:
            embed.set_thumbnail(url=avatar_url)

    embed.set_footer(text="YouTube", icon_url="https://www.youtube.com/s/desktop/f506bd45/img/favicon_32x32.png")

    view = LinkView(url=link, label="Regarder la vidéo", emoji="▶️")

    try:
        await channel.send(embed=embed, view=view)
    except Exception as e:
        print(f"Erreur envoi annonce YouTube : {e}")


async def send_twitch_announcement(channel, tw_conf: dict, stream: dict):
    channel_name = tw_conf.get("display_name") or tw_conf.get("username")
    stream_title = stream.get("title", "Sans titre")
    game_name = stream.get("game_name", "Inconnu")
    twitch_url = f"https://twitch.tv/{tw_conf.get('username')}"

    message = safe_format(
        tw_conf.get("message_template") or DEFAULT_TWITCH_MESSAGE,
        channel=channel_name,
        title=stream_title,
        url=twitch_url,
        game=game_name,
    )

    embed = discord.Embed(
        title=stream_title,
        description=message,
        color=discord.Color.purple(),
    )

    avatar_url = tw_conf.get("avatar_url")
    thumb = stream.get("thumbnail_url", "")
    if thumb:
        thumb = thumb.replace("{width}", "1280").replace("{height}", "720")

    embed.set_author(
        name=channel_name,
        icon_url=avatar_url if avatar_url else discord.Embed.Empty,
        url=twitch_url,
    )

    if thumb:
        embed.set_image(url=thumb)
    if avatar_url:
        embed.set_thumbnail(url=avatar_url)

    embed.add_field(name="🎮 Jeu", value=game_name, inline=True)
    embed.add_field(name="👀 Viewers", value=str(stream.get("viewer_count", "?")), inline=True)

    embed.set_footer(text="Twitch", icon_url="https://static.twitchcdn.net/assets/favicon-32-e29e246c157142c94346.png")

    view = LinkView(url=twitch_url, label="Regarder le live", emoji="🔴")

    try:
        await channel.send(embed=embed, view=view)
    except Exception as e:
        print(f"Erreur envoi annonce Twitch : {e}")


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------
@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        print(f"✅ {len(synced)} commande(s) synchronisée(s)")
    except Exception as e:
        print(f"Erreur de synchronisation : {e}")

    print(f"🤖 Connecté en tant que {bot.user}")
    if not check_updates.is_running():
        check_updates.start()


@bot.tree.command(name="setup-salon", description="Définit le salon où seront envoyées les annonces")
@app_commands.describe(salon="Le salon textuel pour les annonces")
async def setup_salon(interaction: discord.Interaction, salon: discord.TextChannel):
    conf = get_guild_conf(interaction.guild_id)
    conf["announce_channel"] = salon.id
    async with config_lock:
        save_config(config)
    await interaction.response.send_message(
        f"✅ Le salon d'annonce est maintenant {salon.mention}", ephemeral=True
    )


@bot.tree.command(name="setup-ytb", description="Définit la chaîne YouTube à surveiller")
@app_commands.describe(url="URL de la chaîne (ex: https://youtube.com/@example)")
async def setup_ytb(interaction: discord.Interaction, url: str):
    await interaction.response.defer(ephemeral=True)

    async with aiohttp.ClientSession() as session:
        channel_id = await extract_youtube_channel_id(session, url)

        if not channel_id:
            msg = "❌ Impossible de trouver cette chaîne YouTube. Vérifie l'URL."
            if not YOUTUBE_API_KEY:
                msg += "\n💡 Ajoute une `YOUTUBE_API_KEY` dans le `.env` pour une détection fiable."
            await interaction.followup.send(msg)
            return

        video_id, video_title, _, _, channel_name = await get_latest_youtube_video(session, channel_id)
        branding = await get_channel_branding(session, channel_id)

    conf = get_guild_conf(interaction.guild_id)
    conf["youtube"]["channel_id"] = channel_id
    conf["youtube"]["url"] = url
    conf["youtube"]["last_video_id"] = video_id
    conf["youtube"]["channel_title"] = branding["channel_title"] or channel_name
    conf["youtube"]["avatar_url"] = branding["avatar_url"]
    conf["youtube"]["banner_url"] = branding["banner_url"]

    async with config_lock:
        save_config(config)

    extra = f"\nDernière vidéo détectée : *{video_title}*" if video_title else ""
    banner_info = "\n🖼️ Bannière récupérée avec succès." if branding["banner_url"] else "\n⚠️ Aucune bannière trouvée."
    await interaction.followup.send(
        f"✅ Chaîne YouTube configurée : **{conf['youtube']['channel_title'] or url}**{extra}{banner_info}"
    )


@bot.tree.command(name="setup-twitch", description="Définit la chaîne Twitch à surveiller")
@app_commands.describe(url="URL ou pseudo de la chaîne (ex: https://twitch.tv/example ou example)")
async def setup_twitch(interaction: discord.Interaction, url: str):
    await interaction.response.defer(ephemeral=True)

    username = extract_twitch_username(url)
    if not username:
        await interaction.followup.send("❌ URL ou pseudo Twitch invalide.")
        return

    if not TWITCH_CLIENT_ID or not TWITCH_CLIENT_SECRET:
        await interaction.followup.send(
            "❌ `TWITCH_CLIENT_ID` / `TWITCH_CLIENT_SECRET` manquants dans le `.env`."
        )
        return

    async with aiohttp.ClientSession() as session:
        user_info = await get_twitch_user_info(session, username)

    if not user_info:
        await interaction.followup.send(f"❌ La chaîne Twitch **{username}** n'existe pas.")
        return

    conf = get_guild_conf(interaction.guild_id)
    conf["twitch"]["username"] = username
    conf["twitch"]["is_live"] = False
    conf["twitch"]["display_name"] = user_info["display_name"]
    conf["twitch"]["avatar_url"] = user_info["avatar_url"]
    conf["twitch"]["offline_image_url"] = user_info["offline_image_url"]

    async with config_lock:
        save_config(config)

    await interaction.followup.send(f"✅ Chaîne Twitch configurée : **{user_info['display_name']}**")


@bot.tree.command(name="setup-message-ytb", description="Personnalise le message d'annonce YouTube")
@app_commands.describe(message="Placeholders dispo : {channel}, {title}, {url}")
async def setup_message_ytb(interaction: discord.Interaction, message: str):
    conf = get_guild_conf(interaction.guild_id)
    conf["youtube"]["message_template"] = message
    async with config_lock:
        save_config(config)

    preview = safe_format(message, channel="MaChaine", title="Titre de la vidéo", url="https://youtube.com/...")
    await interaction.response.send_message(
        f"✅ Message YouTube mis à jour !\n**Aperçu :**\n{preview}", ephemeral=True
    )


@bot.tree.command(name="setup-message-twitch", description="Personnalise le message d'annonce Twitch")
@app_commands.describe(message="Placeholders dispo : {channel}, {title}, {url}, {game}")
async def setup_message_twitch(interaction: discord.Interaction, message: str):
    conf = get_guild_conf(interaction.guild_id)
    conf["twitch"]["message_template"] = message
    async with config_lock:
        save_config(config)

    preview = safe_format(
        message, channel="MaChaine", title="Titre du live", url="https://twitch.tv/...", game="Just Chatting"
    )
    await interaction.response.send_message(
        f"✅ Message Twitch mis à jour !\n**Aperçu :**\n{preview}", ephemeral=True
    )


@bot.tree.command(name="status", description="Affiche la configuration actuelle du serveur")
async def status(interaction: discord.Interaction):
    conf = get_guild_conf(interaction.guild_id)
    salon = f"<#{conf['announce_channel']}>" if conf["announce_channel"] else "Non défini"
    yt_name = conf["youtube"].get("channel_title") or conf["youtube"].get("url") or "Non défini"
    tw_name = conf["twitch"].get("display_name") or "Non défini"

    embed = discord.Embed(title="⚙️ Configuration du bot", color=discord.Color.blurple())
    embed.add_field(name="Salon d'annonce", value=salon, inline=False)
    embed.add_field(name="Chaîne YouTube", value=yt_name, inline=False)
    embed.add_field(name="Message YouTube", value=f"```{conf['youtube']['message_template']}```", inline=False)
    embed.add_field(name="Chaîne Twitch", value=tw_name, inline=False)
    embed.add_field(name="Message Twitch", value=f"```{conf['twitch']['message_template']}```", inline=False)

    if conf["youtube"].get("banner_url"):
        embed.set_image(url=conf["youtube"]["banner_url"])

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="test-ytb", description="Force une annonce test pour la chaîne YouTube configurée")
async def test_ytb(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    conf = get_guild_conf(interaction.guild_id)
    announce_channel_id = conf.get("announce_channel")

    if not conf["youtube"].get("channel_id"):
        await interaction.followup.send("❌ Aucune chaîne YouTube configurée.")
        return
    if not announce_channel_id:
        await interaction.followup.send("❌ Aucun salon d'annonce configuré.")
        return

    async with aiohttp.ClientSession() as session:
        video_id, title, link, thumbnail, _ = await get_latest_youtube_video(
            session, conf["youtube"]["channel_id"]
        )

    if not video_id:
        await interaction.followup.send("❌ Impossible de récupérer la dernière vidéo.")
        return

    channel = bot.get_channel(announce_channel_id)
    await send_youtube_announcement(channel, conf["youtube"], title, link, thumbnail)
    await interaction.followup.send("✅ Annonce test envoyée !")


@bot.tree.command(name="force-annonce", description="Force une annonce à partir d'un lien YouTube ou Twitch")
@app_commands.describe(lien="Lien de la vidéo YouTube ou de la chaîne Twitch")
async def force_annonce(interaction: discord.Interaction, lien: str):
    await interaction.response.defer(ephemeral=True)

    conf = get_guild_conf(interaction.guild_id)
    announce_channel_id = conf.get("announce_channel")

    if not announce_channel_id:
        await interaction.followup.send("❌ Aucun salon d'annonce configuré. Utilise `/setup-salon` d'abord.")
        return

    channel = bot.get_channel(announce_channel_id)
    if not channel:
        await interaction.followup.send("❌ Le salon d'annonce configuré est introuvable.")
        return

    link_type = detect_link_type(lien)

    if link_type is None:
        await interaction.followup.send(
            "❌ Lien non reconnu. Envoie un lien YouTube (`youtube.com/watch?v=...` ou `youtu.be/...`) "
            "ou un lien Twitch (`twitch.tv/pseudo`)."
        )
        return

    async with aiohttp.ClientSession() as session:

        # --- Cas YouTube ---
        if link_type == "youtube":
            video_id = extract_youtube_video_id(lien)
            if not video_id:
                await interaction.followup.send("❌ Impossible d'extraire l'ID de la vidéo.")
                return

            video_info = await get_youtube_video_info(session, video_id)
            if not video_info:
                await interaction.followup.send("❌ Impossible de récupérer les informations de cette vidéo.")
                return

            yt_conf = dict(conf.get("youtube", {}))
            if video_info.get("channel_title"):
                yt_conf["channel_title"] = video_info["channel_title"]

            await send_youtube_announcement(
                channel,
                yt_conf,
                video_info["title"],
                video_info["url"],
                video_info["thumbnail"],
            )

            await interaction.followup.send(f"✅ Annonce YouTube envoyée dans {channel.mention} !")

        # --- Cas Twitch ---
        elif link_type == "twitch":
            username, user_info, stream = await get_twitch_stream_by_url(session, lien)

            if not username:
                await interaction.followup.send("❌ Impossible d'extraire le pseudo Twitch depuis ce lien.")
                return

            if not user_info:
                await interaction.followup.send(f"❌ La chaîne Twitch **{username}** n'existe pas.")
                return

            tw_conf = dict(conf.get("twitch", {}))
            tw_conf["username"] = username
            tw_conf["display_name"] = user_info["display_name"]
            tw_conf["avatar_url"] = user_info["avatar_url"]

            if stream:
                await send_twitch_announcement(channel, tw_conf, stream)
                await interaction.followup.send(f"✅ Annonce Twitch (live en cours) envoyée dans {channel.mention} !")
            else:
                fake_stream = {
                    "title": "Chaîne Twitch",
                    "game_name": "Inconnu",
                    "viewer_count": "?",
                    "thumbnail_url": user_info.get("offline_image_url") or "",
                }
                await send_twitch_announcement(channel, tw_conf, fake_stream)
                await interaction.followup.send(
                    f"✅ Annonce Twitch envoyée dans {channel.mention} !\n"
                    f"⚠️ Note : la chaîne n'est pas actuellement en live."
                )


# ---------------------------------------------------------------------------
# Boucle de vérification (toutes les 3 minutes)
# ---------------------------------------------------------------------------
@tasks.loop(minutes=3)
async def check_updates():
    async with aiohttp.ClientSession() as session:
        for gid, conf in list(config.items()):
            announce_channel_id = conf.get("announce_channel")
            if not announce_channel_id:
                continue

            channel = bot.get_channel(announce_channel_id)
            if not channel:
                continue

            changed = False

            # --- Vérification YouTube ---
            yt = conf.get("youtube", {})
            if yt.get("channel_id"):
                video_id, title, link, thumbnail, _ = await get_latest_youtube_video(session, yt["channel_id"])
                if video_id and video_id != yt.get("last_video_id"):
                    yt["last_video_id"] = video_id
                    changed = True
                    await send_youtube_announcement(channel, yt, title, link, thumbnail)

            # --- Vérification Twitch ---
            tw = conf.get("twitch", {})
            if tw.get("username"):
                stream = await is_twitch_live(session, tw["username"])

                if stream and not tw.get("is_live"):
                    tw["is_live"] = True
                    changed = True
                    await send_twitch_announcement(channel, tw, stream)

                elif not stream and tw.get("is_live"):
                    tw["is_live"] = False
                    changed = True

            if changed:
                async with config_lock:
                    save_config(config)


@check_updates.before_loop
async def before_check_updates():
    await bot.wait_until_ready()


# ---------------------------------------------------------------------------
# Lancement
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise SystemExit("❌ DISCORD_TOKEN manquant dans le .env")
    bot.run(DISCORD_TOKEN)