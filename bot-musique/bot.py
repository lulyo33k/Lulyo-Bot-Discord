"""
Bot Discord musical (YouTube / YouTube Music / Spotify / Deezer) - tout le code est dans ce fichier.

    /musique url:<lien>   ajoute une playlist, un album ou une piste à la file d'attente
    /search nom:<texte>   cherche un morceau par son nom et joue le résultat le plus probable
    /mp3 fichier:<pièce jointe> | nom:<bibliothèque>   joue un fichier MP3
    /dash                 tableau de bord (embeds + boutons) : lecture, file d'attente, stats
    /clear                supprime les messages du bot dans le salon (ou le MP) courant
    /skip  /pause  /resume  /replay  /stop  /queue  /alea

/musique, /mp3 et /dash fonctionnent aussi en message privé (MP) avec le bot : il joue alors dans
le salon vocal où se trouve l'utilisateur (parmi les serveurs en commun). Un simple message écrit
au bot en MP déclenche une réponse d'aide.

Chaîne audio : yt-dlp (résolution du flux au dernier moment) -> FFmpeg -> Discord.
Les MP3 sont lus directement par FFmpeg (URL de la pièce jointe Discord ou fichier local du
dossier MP3_DIR). Rien n'est téléchargé sur le disque. Chaque serveur Discord a son propre lecteur.

Spotify et Deezer : seules les métadonnées (artiste, titre, durée) sont lues ; l'audio
est cherché sur YouTube au moment de la lecture (ni Spotify ni Deezer ne fournissent de flux).
"""

import asyncio
import functools
import itertools
import json
import logging
import math
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urljoin, urlparse

import aiohttp
import discord
import yt_dlp
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logging.getLogger("discord").setLevel(logging.WARNING)
log = logging.getLogger("musicbot")


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("Variable %s invalide (%r) : valeur par défaut %s", name, raw, default)
        return default


DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "").strip()
# Conservé pour compatibilité avec les anciens fichiers .env ; non utilisé pour la sync globale.
DISCORD_GUILD_ID = _env_int("DISCORD_GUILD_ID", 0)
IDLE_TIMEOUT = _env_int("IDLE_TIMEOUT", 300)  # secondes ; <= 0 désactive le timer
MAX_PLAYLIST_ITEMS = max(1, _env_int("MAX_PLAYLIST_ITEMS", 500))
FFMPEG_PATH = os.getenv("FFMPEG_PATH", "").strip() or "ffmpeg"
MP3_DIR = os.getenv("MP3_DIR", "").strip()  # dossier de la bibliothèque MP3 du bot (optionnel)

AUDIO_EXTENSIONS = {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".opus"}  # formats acceptés par /mp3
MAX_LOCAL_FILES = 5000          # fichiers listés au maximum dans la bibliothèque MP3_DIR
LOCAL_CACHE_TTL = 30            # secondes de cache du contenu de MP3_DIR (autocomplétion)
FFPROBE_TIMEOUT = 20            # secondes max pour lire la durée / les tags d'un fichier audio

DASH_REFRESH = 10               # secondes entre deux actualisations automatiques de /dash
DASH_LIFETIME = 14 * 60         # durée de vie du /dash (le jeton d'interaction Discord expire à 15 min)
DASH_QUEUE_LIMIT = 5            # morceaux à suivre affichés dans /dash
DM_REPLY_COOLDOWN = 5           # secondes minimum entre deux réponses automatiques à un même utilisateur en MP
CLEAR_SCAN_LIMIT = 1000         # nombre de messages récents examinés par /clear

QUEUE_DISPLAY_LIMIT = 10        # morceaux affichés par /queue
LOAD_TIMEOUT = 120              # secondes max pour charger une playlist
RESOLVE_TIMEOUT = 60            # secondes max pour résoudre le flux d'un morceau
MAX_CONSECUTIVE_FAILURES = 8    # arrêt de la lecture si trop d'échecs d'affilée
MIN_PLAY_SECONDS = 2.0          # un morceau qui s'arrête plus vite = échec FFmpeg probable


# --------------------------------------------------------------------------- #
# Utilitaires
# --------------------------------------------------------------------------- #
def fmt_duration(seconds) -> str:
    if not isinstance(seconds, (int, float)) or seconds <= 0:
        return "?"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def clean_title(title: str, limit: int = 70) -> str:
    """Titre sûr pour l'affichage Discord (tronqué, markdown échappé)."""
    text = re.sub(r"\s+", " ", title or "").strip() or "Titre inconnu"
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    text = text.replace("[", "(").replace("]", ")")
    return discord.utils.escape_markdown(text)


_YT_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
}
_DEEZER_HOSTS = {"deezer.com", "www.deezer.com"}
_DEEZER_SHORT_HOSTS = {"link.deezer.com", "deezer.page.link", "dzr.page.link"}
_SPOTIFY_HOSTS = {"open.spotify.com", "play.spotify.com"}
_SPOTIFY_SHORT_HOSTS = {"spotify.link", "spotify.app.link"}

_DEEZER_PATH_RE = re.compile(r"^/(?:[a-z]{2}(?:-[a-z]{2})?/)?(track|album|playlist)/(\d{1,20})/?$")
_SPOTIFY_INTL_RE = re.compile(r"^intl-[a-z]{2}(?:-[a-z]{2,4})?$")
_SPOTIFY_ID_RE = re.compile(r"^[A-Za-z0-9]{22}$")

SERVICE_LABELS = {"youtube": "YouTube", "spotify": "Spotify", "deezer": "Deezer"}


def _is_youtube_target(url: str, parsed, host: str) -> bool:
    if host.endswith("youtu.be"):
        return len(parsed.path.strip("/")) > 0
    query = parse_qs(parsed.query)
    if "list" in query or "v" in query:
        return True
    return bool(re.match(r"^/(shorts|live|embed)/[\w-]{5,}", parsed.path))


def parse_deezer_ref(url: str) -> tuple[str, str] | None:
    """(type, id) d'un lien Deezer complet (track / album / playlist), sinon None."""
    match = _DEEZER_PATH_RE.match(urlparse(url).path)
    return (match.group(1), match.group(2)) if match else None


def parse_spotify_ref(url: str) -> tuple[str, str] | None:
    """(type, id) d'un lien Spotify complet (track / album / playlist), sinon None."""
    parts = [p for p in urlparse(url).path.split("/") if p]
    if parts and _SPOTIFY_INTL_RE.match(parts[0]):
        parts = parts[1:]
    if parts and parts[0] == "embed":
        parts = parts[1:]
    if len(parts) >= 4 and parts[0] == "user" and parts[2] == "playlist":  # ancien format
        parts = parts[2:]
    if len(parts) >= 2 and parts[0] in ("track", "album", "playlist") and _SPOTIFY_ID_RE.match(parts[1]):
        return parts[0], parts[1]
    return None


def identify_source(raw: str) -> tuple[str, str] | None:
    """Reconnaît un lien YouTube / Spotify / Deezer. Retourne (service, url) ou None."""
    url = (raw or "").strip().strip("<>")
    if not url or len(url) > 500 or re.search(r"\s", url):
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    host = (parsed.hostname or "").lower()

    if host in _YT_HOSTS:
        return ("youtube", url) if _is_youtube_target(url, parsed, host) else None
    if host in _DEEZER_HOSTS:
        ref = parse_deezer_ref(url)
        return ("deezer", f"https://www.deezer.com/{ref[0]}/{ref[1]}") if ref else None
    if host in _DEEZER_SHORT_HOSTS:
        return ("deezer", url)
    if host in _SPOTIFY_HOSTS:
        ref = parse_spotify_ref(url)
        return ("spotify", f"https://open.spotify.com/{ref[0]}/{ref[1]}") if ref else None
    if host in _SPOTIFY_SHORT_HOSTS:
        return ("spotify", url)
    return None


def describe_ytdlp_error(exc: Exception) -> str:
    """Message utilisateur lisible à partir d'une erreur yt-dlp."""
    text = str(exc).lower()
    if "private video" in text or "this video is private" in text:
        return "🔒 Cette vidéo est privée."
    if "does not exist" in text or ("playlist" in text and "not found" in text):
        return "❌ Playlist introuvable (elle n'existe pas ou n'est pas accessible)."
    if any(k in text for k in ("video unavailable", "no longer available", "has been removed", "terminated", "not available", "unavailable")):
        return "❌ Cette vidéo est indisponible (supprimée, bloquée ou restreinte)."
    if "unsupported url" in text or "is not a valid url" in text:
        return "❌ URL non supportée."
    if "sign in to confirm" in text or "not a bot" in text:
        return "⚠️ YouTube demande une vérification (anti-bot) : réessaie plus tard."
    if any(k in text for k in ("getaddrinfo", "urlopen error", "timed out", "connection", "network", "temporary failure", "ssl", "certificate")):
        return "🌐 Erreur réseau lors de l'accès à YouTube. Réessaie dans un instant."
    return "⚠️ Erreur yt-dlp : impossible de récupérer ce contenu."


# --------------------------------------------------------------------------- #
# yt-dlp (opérations bloquantes, à appeler via asyncio.to_thread)
# --------------------------------------------------------------------------- #
class _YTDLLogger:
    """Redirige les logs yt-dlp vers `logging` sans polluer la console."""

    def debug(self, msg):
        log.debug("yt-dlp: %s", msg)

    def info(self, msg):
        log.debug("yt-dlp: %s", msg)

    def warning(self, msg):
        log.warning("yt-dlp: %s", msg)

    def error(self, msg):
        log.debug("yt-dlp (erreur): %s", msg)


@dataclass(slots=True)
class Track:
    title: str
    url: str | None          # page YouTube ; None pour un morceau Spotify / Deezer pas encore résolu
    duration: int | None
    requested_by: str
    query: str | None = None  # recherche YouTube (« artiste - titre ») quand url est None
    kind: str = "youtube"     # "youtube" | "attachment" (MP3 envoyé sur Discord) | "local" (MP3 de MP3_DIR)
    stream: str | None = None  # source directe pour FFmpeg (URL CDN Discord ou chemin local) si kind != "youtube"


_UNAVAILABLE_TITLES = {"[deleted video]", "[private video]", "[unavailable video]", "[deleted]", "[private]"}
_BAD_AVAILABILITY = {"private", "needs_auth", "premium_only", "subscriber_only"}
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def _video_url(entry: dict) -> str | None:
    """URL canonique construite à partir de l'ID (jamais d'URL arbitraire dans la queue)."""
    vid = entry.get("id")
    if isinstance(vid, str) and _VIDEO_ID_RE.match(vid):
        return f"https://www.youtube.com/watch?v={vid}"
    return None


def _collect_entries(entries, tracks: list, requested_by: str, counters: dict, depth: int = 0) -> None:
    for entry in entries:
        if len(tracks) >= MAX_PLAYLIST_ITEMS:
            return
        if not entry:
            counters["skipped"] += 1
            continue
        nested = entry.get("entries")
        if nested is not None and depth < 2:
            _collect_entries(nested, tracks, requested_by, counters, depth + 1)
            continue
        title = (entry.get("title") or "").strip()
        url = _video_url(entry)
        if (
            url is None
            or title.lower() in _UNAVAILABLE_TITLES
            or entry.get("availability") in _BAD_AVAILABILITY
        ):
            counters["skipped"] += 1
            log.info("Morceau ignoré (indisponible) : %s", title or entry.get("id") or "?")
            continue
        duration = entry.get("duration")
        tracks.append(
            Track(
                title=title or "Titre inconnu",
                url=url,
                duration=int(duration) if isinstance(duration, (int, float)) else None,
                requested_by=requested_by,
            )
        )


def load_tracks_blocking(url: str, requested_by: str) -> tuple[list[Track], int]:
    """Récupère les métadonnées (sans résoudre les flux audio). Retourne (morceaux, ignorés)."""
    opts = {
        "quiet": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "playlistend": MAX_PLAYLIST_ITEMS,
        "socket_timeout": 15,
        "retries": 3,
        "logger": _YTDLLogger(),
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        raise yt_dlp.utils.DownloadError("Aucune information renvoyée")

    tracks: list[Track] = []
    counters = {"skipped": 0}
    if info.get("entries") is not None:
        _collect_entries(info["entries"], tracks, requested_by, counters)
        log.info(
            "Playlist chargée : %s (%d morceaux, %d ignorés)",
            info.get("title") or "?", len(tracks), counters["skipped"],
        )
    else:
        video_url = _video_url(info)
        if video_url is None:
            raise yt_dlp.utils.DownloadError("Vidéo non supportée")
        duration = info.get("duration")
        tracks.append(
            Track(
                title=(info.get("title") or "Titre inconnu").strip(),
                url=video_url,
                duration=int(duration) if isinstance(duration, (int, float)) else None,
                requested_by=requested_by,
            )
        )
        log.info("Vidéo chargée : %s", tracks[0].title)
    return tracks, counters["skipped"]


_BAD_WORDS = ("cover", "karaoke", "live", "reaction", "instrumental", "remix", "8d", "slowed", "sped up", "nightcore", "tutorial")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _pick_search_result(entries, query: str, expected_duration: int | None) -> str | None:
    """Choisit le meilleur résultat YouTube : proche de la durée attendue, sans mots « cover / live… »."""
    wanted = query.lower()
    best_url, best_score = None, None
    for rank, entry in enumerate(entries):
        if not entry:
            continue
        url = _video_url(entry)
        if url is None:
            continue
        title = (entry.get("title") or "").lower()
        score = rank * 2.0
        duration = entry.get("duration")
        if expected_duration and isinstance(duration, (int, float)):
            diff = abs(duration - expected_duration)
            score += diff / 5
            if diff > max(30, expected_duration * 0.25):
                score += 25
        score += 15 * sum(1 for word in _BAD_WORDS if word in title and word not in wanted)
        if best_score is None or score < best_score:
            best_url, best_score = url, score
    return best_url


def _search_youtube_blocking(query: str, expected_duration: int | None) -> str:
    opts = {
        "quiet": True,
        "skip_download": True,
        "extract_flat": True,
        "socket_timeout": 15,
        "retries": 3,
        "logger": _YTDLLogger(),
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch5:{query}", download=False)
    entries = list((info or {}).get("entries") or [])
    url = _pick_search_result(entries, query, expected_duration)
    if url is None:
        raise RuntimeError(f"Aucun résultat YouTube pour « {query} »")
    return url


def search_track_blocking(query: str, requested_by: str) -> Track:
    """Cherche le morceau YouTube le plus probable pour une requête libre (« artiste - titre », etc.)."""
    video_url = _search_youtube_blocking(query, expected_duration=None)
    tracks, _skipped = load_tracks_blocking(video_url, requested_by)
    if not tracks:
        raise RuntimeError(f"Aucun résultat lisible pour « {query} »")
    return tracks[0]


def _extract_stream_blocking(url: str) -> tuple[str, dict]:
    """Résout l'URL du flux audio d'une vidéo YouTube. Retourne (url_flux, en-têtes HTTP)."""
    opts = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "skip_download": True,
        "socket_timeout": 15,
        "retries": 3,
        "logger": _YTDLLogger(),
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if info and info.get("entries"):
        info = next((e for e in info["entries"] if e), None)
    if not info or not info.get("url"):
        raise RuntimeError("Aucun flux audio disponible")
    return info["url"], info.get("http_headers") or {}


def resolve_stream_blocking(track: Track) -> tuple[str, dict, str]:
    """Trouve la vidéo (recherche YouTube si besoin) puis son flux. Retourne (flux, en-têtes, page YouTube)."""
    if track.kind != "youtube":  # MP3 : la source est déjà directe, yt-dlp n'intervient pas
        if not track.stream:
            raise RuntimeError("Fichier audio sans source")
        return track.stream, {}, track.url or ""
    page_url = track.url
    if page_url is None:
        if not track.query:
            raise RuntimeError("Morceau sans source")
        page_url = _search_youtube_blocking(track.query, track.duration)
    stream_url, headers = _extract_stream_blocking(page_url)
    return stream_url, headers, page_url


# --------------------------------------------------------------------------- #
# Spotify / Deezer : métadonnées uniquement (l'audio vient de YouTube)
# --------------------------------------------------------------------------- #
class SourceError(Exception):
    """Échec de chargement d'une source ; le message est prêt à être affiché à l'utilisateur."""


_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=20)
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
_REDIRECT_CODES = (301, 302, 303, 307, 308)
_NEXT_DATA_RE = re.compile(r"<script[^>]*id=\"__NEXT_DATA__\"[^>]*>(.*?)</script>", re.S)
SPOTIFY_EMBED_LIMIT = 100  # nombre de morceaux exposés par la page embed de Spotify


async def _http_get(session: aiohttp.ClientSession, url: str, *, expect: str = "text"):
    """GET sans suivre les redirections ; erreurs converties en SourceError lisibles."""
    try:
        async with session.get(url, allow_redirects=False) as resp:
            if resp.status == 404:
                raise SourceError("❌ Contenu introuvable (lien invalide ou supprimé).")
            if resp.status in (401, 403):
                raise SourceError("🔒 Ce contenu n'est pas accessible publiquement.")
            if resp.status == 429:
                raise SourceError("⏳ Trop de requêtes vers ce service, réessaie dans un instant.")
            if resp.status >= 400 or resp.status in _REDIRECT_CODES:
                raise SourceError(f"⚠️ Le service a répondu par une erreur inattendue ({resp.status}).")
            if expect == "json":
                return await resp.json(content_type=None)
            return await resp.text()
    except SourceError:
        raise
    except asyncio.TimeoutError:
        raise SourceError("⏱️ Le service met trop de temps à répondre. Réessaie dans un instant.") from None
    except (aiohttp.ClientError, ValueError) as exc:
        log.error("Erreur réseau sur %s : %s", urlparse(url).hostname, exc)
        raise SourceError("🌐 Erreur réseau lors de l'accès au service. Réessaie dans un instant.") from None


async def _follow_short_link(session: aiohttp.ClientSession, url: str, allowed_hosts: set) -> str:
    """Résout un lien court en suivant les redirections, uniquement vers des domaines autorisés."""
    current = url
    for _ in range(5):
        parsed = urlparse(current)
        if parsed.scheme not in ("http", "https") or (parsed.hostname or "").lower() not in allowed_hosts:
            raise SourceError("❌ Lien court non reconnu. Utilise le lien complet du morceau ou de la playlist.")
        try:
            async with session.get(current, allow_redirects=False) as resp:
                location = resp.headers.get("Location")
                if resp.status in _REDIRECT_CODES and location:
                    current = urljoin(current, location)
                    continue
                return current
        except asyncio.TimeoutError:
            raise SourceError("⏱️ Le service met trop de temps à répondre. Réessaie dans un instant.") from None
        except aiohttp.ClientError as exc:
            log.error("Erreur réseau (lien court) : %s", exc)
            raise SourceError("🌐 Erreur réseau lors de l'accès au service. Réessaie dans un instant.") from None
    raise SourceError("❌ Lien court non résolu (trop de redirections).")


def _make_search_track(artist: str, title: str, duration, requested_by: str) -> Track:
    """Morceau « à chercher sur YouTube » : affichage « Artiste - Titre »."""
    artist = re.sub(r"\s+", " ", (artist or "").replace("\xa0", " ")).strip()
    title = re.sub(r"\s+", " ", (title or "").replace("\xa0", " ")).strip()
    display = f"{artist} - {title}" if artist else title
    seconds = int(duration) if isinstance(duration, (int, float)) and duration > 0 else None
    return Track(
        title=display,
        url=None,
        duration=seconds,
        requested_by=requested_by,
        query=_CTRL_RE.sub("", display)[:200],
    )


# ---- Deezer (API publique, sans clé) ---------------------------------------
def _raise_deezer_error(data) -> None:
    if not isinstance(data, dict) or "error" not in data:
        return
    err = data.get("error")
    err = err if isinstance(err, dict) else {}
    code, kind, message = err.get("code"), err.get("type"), str(err.get("message") or "")
    log.error("Erreur API Deezer : %s", err)
    if code == 800 or kind == "DataException":
        raise SourceError("❌ Contenu Deezer introuvable (lien invalide ou supprimé).")
    if code in (200, 300) or kind == "OAuthException" or "permission" in message.lower():
        raise SourceError("🔒 Ce contenu Deezer est privé ou inaccessible.")
    if code == 4 or "quota" in message.lower():
        raise SourceError("⏳ Limite de requêtes Deezer atteinte, réessaie dans quelques secondes.")
    raise SourceError("⚠️ Deezer a renvoyé une erreur.")


def _deezer_item_to_track(item, requested_by: str) -> Track | None:
    if not isinstance(item, dict):
        return None
    title = (item.get("title_short") or item.get("title") or "").strip()
    artist = ((item.get("artist") or {}).get("name") or "") if isinstance(item.get("artist"), dict) else ""
    if not title:
        return None
    return _make_search_track(artist, title, item.get("duration"), requested_by)


async def load_deezer(url: str, requested_by: str) -> tuple[list[Track], int, list[str]]:
    async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT, headers=_BROWSER_HEADERS) as session:
        if (urlparse(url).hostname or "").lower() in _DEEZER_SHORT_HOSTS:
            url = await _follow_short_link(session, url, _DEEZER_SHORT_HOSTS | _DEEZER_HOSTS)
        ref = parse_deezer_ref(url) if (urlparse(url).hostname or "").lower() in _DEEZER_HOSTS else None
        if ref is None:
            raise SourceError("❌ Lien Deezer non reconnu (une piste, un album ou une playlist est attendu).")
        kind, ident = ref

        tracks: list[Track] = []
        skipped = 0
        if kind == "track":
            data = await _http_get(session, f"https://api.deezer.com/track/{ident}", expect="json")
            _raise_deezer_error(data)
            track = _deezer_item_to_track(data, requested_by)
            if track:
                tracks.append(track)
            else:
                skipped += 1
        else:
            next_url = f"https://api.deezer.com/{kind}/{ident}/tracks?limit=100"
            pages = 0
            while next_url and len(tracks) < MAX_PLAYLIST_ITEMS and pages < 30:
                data = await _http_get(session, next_url, expect="json")
                _raise_deezer_error(data)
                items = data.get("data") if isinstance(data, dict) else None
                for item in items or []:
                    if len(tracks) >= MAX_PLAYLIST_ITEMS:
                        break
                    track = _deezer_item_to_track(item, requested_by)
                    if track:
                        tracks.append(track)
                    else:
                        skipped += 1
                candidate = data.get("next") if isinstance(data, dict) else None
                next_url = candidate if isinstance(candidate, str) and candidate.startswith("https://api.deezer.com/") else None
                pages += 1
    log.info("%s Deezer chargé : %d morceaux, %d ignorés", kind, len(tracks), skipped)
    return tracks, skipped, []


# ---- Spotify (page « embed » publique, sans clé ni compte) -------------------
def _find_dict_with_key(obj, key: str, depth: int = 0):
    if depth > 8:
        return None
    if isinstance(obj, dict):
        if key in obj:
            return obj
        children = obj.values()
    elif isinstance(obj, list):
        children = obj
    else:
        return None
    for child in children:
        found = _find_dict_with_key(child, key, depth + 1)
        if found is not None:
            return found
    return None


def parse_spotify_embed(data: dict, requested_by: str, max_items: int) -> tuple[list[Track], int]:
    """Extrait (morceaux, ignorés) du JSON `__NEXT_DATA__` de la page embed Spotify."""
    entity = None
    try:
        entity = data["props"]["pageProps"]["state"]["data"]["entity"]
    except (KeyError, TypeError):
        pass
    if not isinstance(entity, dict):
        entity = _find_dict_with_key(data, "trackList")
    if not isinstance(entity, dict):
        raise SourceError("⚠️ Impossible de lire ce lien Spotify (format de page inattendu).")

    entries = entity.get("trackList")
    items = entries if isinstance(entries, list) else [entity]  # album / playlist  ou  piste seule
    tracks: list[Track] = []
    skipped = 0
    for item in items:
        if len(tracks) >= max_items:
            break
        if not isinstance(item, dict):
            skipped += 1
            continue
        title = str(item.get("title") or item.get("name") or "").strip()
        artist = item.get("subtitle")
        if not artist and isinstance(item.get("artists"), list):
            artist = ", ".join(a["name"] for a in item["artists"] if isinstance(a, dict) and a.get("name"))
        duration_ms = item.get("duration")
        seconds = duration_ms / 1000 if isinstance(duration_ms, (int, float)) else None
        if not title:
            skipped += 1
            continue
        tracks.append(_make_search_track(str(artist or ""), title, seconds, requested_by))
    return tracks, skipped


async def load_spotify(url: str, requested_by: str) -> tuple[list[Track], int, list[str]]:
    async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT, headers=_BROWSER_HEADERS) as session:
        if (urlparse(url).hostname or "").lower() in _SPOTIFY_SHORT_HOSTS:
            url = await _follow_short_link(session, url, _SPOTIFY_SHORT_HOSTS | _SPOTIFY_HOSTS)
        ref = parse_spotify_ref(url) if (urlparse(url).hostname or "").lower() in _SPOTIFY_HOSTS else None
        if ref is None:
            raise SourceError(
                "❌ Lien Spotify non reconnu (une piste, un album ou une playlist est attendu). "
                "Essaie avec le lien complet open.spotify.com."
            )
        kind, ident = ref
        html = await _http_get(session, f"https://open.spotify.com/embed/{kind}/{ident}")

    match = _NEXT_DATA_RE.search(html)
    if not match:
        log.error("Spotify : données introuvables dans la page embed (%s %s)", kind, ident)
        raise SourceError("⚠️ Impossible de lire ce lien Spotify (Spotify a peut-être modifié sa page).")
    try:
        data = json.loads(match.group(1))
    except ValueError:
        raise SourceError("⚠️ Impossible de lire ce lien Spotify (données illisibles).") from None

    tracks, skipped = parse_spotify_embed(data, requested_by, MAX_PLAYLIST_ITEMS)
    notes: list[str] = []
    if kind != "track" and len(tracks) + skipped >= SPOTIFY_EMBED_LIMIT:
        notes.append(f"ℹ️ Spotify ne donne accès qu'aux {SPOTIFY_EMBED_LIMIT} premiers morceaux d'une playlist ou d'un album.")
    log.info("%s Spotify chargé : %d morceaux, %d ignorés", kind, len(tracks), skipped)
    return tracks, skipped, notes


async def load_source(service: str, url: str, requested_by: str) -> tuple[list[Track], int, list[str]]:
    """Charge les morceaux d'un lien YouTube / Spotify / Deezer. Retourne (morceaux, ignorés, notes)."""
    if service == "youtube":
        tracks, skipped = await asyncio.to_thread(load_tracks_blocking, url, requested_by)
        return tracks, skipped, []
    if service == "deezer":
        return await load_deezer(url, requested_by)
    if service == "spotify":
        return await load_spotify(url, requested_by)
    raise SourceError("❌ Source non supportée.")


# --------------------------------------------------------------------------- #
# Fichiers audio (MP3) : pièces jointes Discord et bibliothèque locale (MP3_DIR)
# --------------------------------------------------------------------------- #
KIND_LABELS = {
    "youtube": "YouTube",
    "attachment": "MP3 (fichier envoyé)",
    "local": "MP3 (bibliothèque du bot)",
}
_DISCORD_CDN_DOMAINS = ("discordapp.com", "discordapp.net")
_local_cache: dict = {"at": None, "files": []}


def _tidy(text) -> str:
    """Texte d'un tag audio nettoyé (espaces normalisés, caractères de contrôle retirés)."""
    return _CTRL_RE.sub("", re.sub(r"\s+", " ", str(text or ""))).strip()


@functools.lru_cache(maxsize=1)
def _find_ffprobe() -> str | None:
    """ffprobe est livré avec FFmpeg : on le cherche à côté de FFMPEG_PATH, puis dans le PATH."""
    exe = "ffprobe.exe" if os.name == "nt" else "ffprobe"
    ffmpeg = shutil.which(FFMPEG_PATH) or (FFMPEG_PATH if os.path.isfile(FFMPEG_PATH) else None)
    if ffmpeg:
        sibling = os.path.join(os.path.dirname(os.path.abspath(ffmpeg)), exe)
        if os.path.isfile(sibling):
            return sibling
    return shutil.which("ffprobe")


def probe_audio_blocking(source: str) -> tuple[str | None, str | None, int | None]:
    """Lit (titre, artiste, durée en secondes) d'un fichier audio avec ffprobe. Meilleur effort : (None, None, None) sinon."""
    ffprobe = _find_ffprobe()
    if ffprobe is None:
        log.info("ffprobe introuvable : durée et tags des MP3 non lus")
        return None, None, None
    cmd = [
        ffprobe, "-v", "error", "-print_format", "json",
        "-show_entries", "format=duration:format_tags=title,artist",
        "-i", source,
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=FFPROBE_TIMEOUT, check=False,
        )
        data = json.loads(result.stdout or "{}")
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        log.warning("ffprobe : lecture des métadonnées impossible (%s)", exc)
        return None, None, None
    fmt = data.get("format") if isinstance(data, dict) else None
    fmt = fmt if isinstance(fmt, dict) else {}
    raw_tags = fmt.get("tags")
    tags = {str(k).lower(): v for k, v in raw_tags.items()} if isinstance(raw_tags, dict) else {}
    try:
        duration = float(fmt.get("duration"))
    except (TypeError, ValueError):
        duration = 0.0
    seconds = int(duration) if math.isfinite(duration) and duration > 0 else None
    return tags.get("title"), tags.get("artist"), seconds


async def build_audio_track(source: str, fallback_title: str, requested_by: str, kind: str) -> Track:
    """Crée un Track à partir d'un fichier audio (titre / artiste / durée lus dans les tags si possible)."""
    raw_title, raw_artist, duration = await asyncio.to_thread(probe_audio_blocking, source)
    title, artist = _tidy(raw_title), _tidy(raw_artist)
    display = f"{artist} - {title}" if (title and artist) else (title or fallback_title)
    return Track(
        title=display[:200],
        url=None,
        duration=duration,
        requested_by=requested_by,
        kind=kind,
        stream=source,
    )


def is_audio_attachment(attachment: discord.Attachment) -> bool:
    ext = os.path.splitext(attachment.filename or "")[1].lower()
    return ext in AUDIO_EXTENSIONS or (attachment.content_type or "").lower().startswith("audio/")


def is_discord_cdn_url(url: str) -> bool:
    """Les pièces jointes ne sont lues que depuis le CDN de Discord."""
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and any(host == d or host.endswith("." + d) for d in _DISCORD_CDN_DOMAINS)


def _mp3_root() -> Path | None:
    if not MP3_DIR:
        return None
    root = Path(MP3_DIR).expanduser()
    return root.resolve() if root.is_dir() else None


def _scan_local_blocking(root: Path) -> list[str]:
    """Chemins relatifs (séparateur « / ») des fichiers audio de MP3_DIR, sans suivre les liens de dossiers."""
    files: list[str] = []
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            if os.path.splitext(name)[1].lower() in AUDIO_EXTENSIONS:
                files.append(os.path.relpath(os.path.join(dirpath, name), root).replace("\\", "/"))
                if len(files) >= MAX_LOCAL_FILES:
                    return sorted(files, key=str.lower)
    return sorted(files, key=str.lower)


async def get_local_listing() -> list[str]:
    """Contenu de MP3_DIR (mis en cache LOCAL_CACHE_TTL secondes pour l'autocomplétion et /dash)."""
    root = _mp3_root()
    if root is None:
        return []
    at = _local_cache["at"]
    if at is None or time.monotonic() - at > LOCAL_CACHE_TTL:
        _local_cache["files"] = await asyncio.to_thread(_scan_local_blocking, root)
        _local_cache["at"] = time.monotonic()
    return _local_cache["files"]


def _safe_local_path(root: Path, relative: str) -> Path | None:
    """Chemin absolu du fichier si (et seulement si) il est bien DANS MP3_DIR et de type audio."""
    try:
        path = (root / relative).resolve()
        path.relative_to(root)
    except (ValueError, OSError):
        return None
    if path.suffix.lower() not in AUDIO_EXTENSIONS or not path.is_file():
        return None
    return path


async def find_local_file(query: str) -> tuple[Path | None, str | None]:
    """Retrouve un fichier de MP3_DIR. Retourne (chemin, None) ou (None, message d'erreur)."""
    root = _mp3_root()
    if root is None:
        return None, "📁 Aucune bibliothèque MP3 configurée (variable MP3_DIR dans le fichier .env)."
    query = (query or "").strip()
    if not query:
        return None, "❌ Nom de fichier vide."
    direct = _safe_local_path(root, query)
    if direct is not None:
        return direct, None
    needle = query.lower()
    matches = [f for f in await get_local_listing() if needle in f.lower()]
    if len(matches) == 1:
        path = _safe_local_path(root, matches[0])
        if path is not None:
            return path, None
    if len(matches) > 1:
        return None, f"❌ {len(matches)} fichiers correspondent à « {clean_title(query, 40)} » : précise le nom (autocomplétion)."
    return None, "❌ Fichier introuvable dans la bibliothèque du bot."


def build_audio_source(stream_url: str, headers: dict, local: bool = False) -> discord.FFmpegPCMAudio:
    """FFmpeg reçoit des arguments séparés (aucun shell impliqué)."""
    if local:  # fichier sur le disque : les options -reconnect* (HTTP) feraient échouer FFmpeg
        return discord.FFmpegPCMAudio(stream_url, executable=FFMPEG_PATH, options="-vn")
    before = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
    user_agent = headers.get("User-Agent")
    if user_agent:
        before += f" -user_agent {shlex.quote(user_agent)}"
    return discord.FFmpegPCMAudio(
        stream_url,
        executable=FFMPEG_PATH,
        before_options=before,
        options="-vn",
    )


# --------------------------------------------------------------------------- #
# Lecteur par serveur
# --------------------------------------------------------------------------- #
players: dict[int, "GuildPlayer"] = {}


class GuildPlayer:
    """État de lecture d'un serveur : queue, morceau courant, boucle de lecture, timer d'inactivité."""

    def __init__(self, guild: discord.abc.Snowflake):
        self.guild = guild
        self.queue: deque[Track] = deque()
        self.current: Track | None = None
        self.text_channel = None
        self.task: asyncio.Task | None = None
        self.idle_task: asyncio.Task | None = None
        self.lock = asyncio.Lock()
        self.closing = False
        self._skipped = False
        # suivi de la progression du morceau en cours (affichée par /dash)
        self._play_started: float | None = None
        self._paused_at: float | None = None
        self._paused_total = 0.0

    # -- état ------------------------------------------------------------- #
    @property
    def voice_client(self):
        return self.guild.voice_client

    @property
    def is_paused(self) -> bool:
        vc = self.voice_client
        return bool(vc and vc.is_paused())

    def human_count(self) -> int:
        """Nombre de personnes (hors bots connus) dans le salon du bot."""
        vc = self.voice_client
        if vc is None or vc.channel is None:
            return 0
        count = 0
        for user_id in vc.channel.voice_states:
            if user_id == self.guild.me.id:
                continue
            member = self.guild.get_member(user_id)
            if member is not None and member.bot:
                continue
            count += 1
        return count

    async def notify(self, message: str) -> None:
        channel = self.text_channel
        if channel is None:
            return
        try:
            await channel.send(message)
        except Exception as exc:  # permissions, salon supprimé, réseau...
            log.debug("Impossible d'envoyer un message : %s", exc)

    # -- contrôle --------------------------------------------------------- #
    def ensure_task(self) -> None:
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run(), name=f"player-{self.guild.id}")

    def shuffle(self) -> int:
        """Mélange les morceaux à venir (le morceau en cours n'est pas touché)."""
        items = list(self.queue)
        random.shuffle(items)
        self.queue = deque(items)
        return len(items)

    def replay(self) -> bool:
        """Relance le morceau en cours depuis le début (sort aussi de la pause). False si rien ne joue encore."""
        vc = self.voice_client
        if self.current is None or vc is None or not (vc.is_playing() or vc.is_paused()):
            return False
        self.queue.appendleft(self.current)  # la boucle de lecture le reprendra aussitôt
        self._skipped = True
        vc.stop()
        return True

    def skip(self) -> None:
        vc = self.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            self._skipped = True
            vc.stop()  # déclenche le callback `after` -> morceau suivant

    def pause(self) -> None:
        vc = self.voice_client
        if vc and not vc.is_paused():
            vc.pause()
            if self._paused_at is None:
                self._paused_at = time.monotonic()

    def resume(self) -> None:
        vc = self.voice_client
        if vc:
            vc.resume()
            if self._paused_at is not None:
                self._paused_total += time.monotonic() - self._paused_at
                self._paused_at = None

    def _mark_started(self) -> None:
        self._play_started = time.monotonic()
        self._paused_at = None
        self._paused_total = 0.0

    def elapsed(self) -> float | None:
        """Secondes écoulées dans le morceau en cours (pauses exclues) ; None si rien ne joue encore."""
        if self.current is None or self._play_started is None:
            return None
        reference = self._paused_at if self._paused_at is not None else time.monotonic()
        return max(0.0, reference - self._play_started - self._paused_total)

    async def cleanup(self, reason: str = "", message: str | None = None) -> None:
        """Arrête tout, vide la queue, quitte le vocal et oublie ce lecteur (idempotent)."""
        if self.closing:
            return
        self.closing = True
        log.info("Déconnexion (serveur %s)%s", self.guild.id, f" : {reason}" if reason else "")
        if players.get(self.guild.id) is self:
            players.pop(self.guild.id, None)
        self.queue.clear()
        self.current = None
        me = asyncio.current_task()
        for task in (self.idle_task, self.task):
            if task is not None and task is not me and not task.done():
                task.cancel()
        vc = self.voice_client
        if vc is not None:
            try:
                if vc.is_playing() or vc.is_paused():
                    vc.stop()
                await vc.disconnect(force=True)
            except Exception:
                log.exception("Erreur lors de la déconnexion vocale")
        if message:
            await self.notify(message)

    # -- timer d'inactivité ---------------------------------------------- #
    def _should_idle(self) -> bool:
        vc = self.voice_client
        if vc is None or not vc.is_connected():
            return False
        alone = self.human_count() == 0
        inactive = self.current is None and not self.queue
        return alone or inactive

    def refresh_idle_timer(self) -> None:
        """Démarre le timer si le bot est seul (ou n'a plus rien à jouer), l'annule sinon."""
        if self.closing or IDLE_TIMEOUT <= 0:
            return
        if self._should_idle():
            if self.idle_task is None or self.idle_task.done():
                self.idle_task = asyncio.create_task(self._idle_countdown())
        else:
            self.cancel_idle_timer()

    def cancel_idle_timer(self) -> None:
        task = self.idle_task
        self.idle_task = None
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()

    async def _idle_countdown(self) -> None:
        await asyncio.sleep(IDLE_TIMEOUT)
        if self.closing or not self._should_idle():
            return
        if self.human_count() == 0:
            msg = "👋 Je quitte le salon vocal (seul depuis trop longtemps). File d'attente vidée."
        else:
            msg = "👋 Je quitte le salon vocal (plus rien à jouer)."
        await self.cleanup("inactivité", msg)

    # -- boucle de lecture ---------------------------------------------- #
    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        failures = 0
        try:
            while not self.closing:
                vc = self.voice_client
                if vc is None or not vc.is_connected() or not self.queue:
                    break

                track = self.queue.popleft()
                self.current = track
                title = clean_title(track.title, 60)

                # 1) résolution du flux au dernier moment (les URLs expirent)
                try:
                    stream_url, headers, page_url = await asyncio.wait_for(
                        asyncio.to_thread(resolve_stream_blocking, track), RESOLVE_TIMEOUT
                    )
                    track.url = track.url or page_url or None  # mémorise la vidéo trouvée (lien dans /queue)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.error("Erreur yt-dlp sur %r : %s", track.title, exc)
                    failures += 1
                    if await self._too_many_failures(failures, title):
                        break
                    continue

                # 2) lecture via FFmpeg
                if self.closing or not vc.is_connected():
                    break
                done = asyncio.Event()
                errors: list[Exception] = []

                def after(error, _done=done, _errors=errors):
                    if error:
                        _errors.append(error)
                    try:
                        loop.call_soon_threadsafe(_done.set)
                    except RuntimeError:  # boucle déjà fermée (arrêt du bot)
                        pass

                self._skipped = False
                try:
                    source = build_audio_source(stream_url, headers, local=track.kind == "local")
                    vc.play(source, after=after)
                except Exception as exc:
                    log.error("Erreur FFmpeg au lancement de %r : %s", track.title, exc)
                    failures += 1
                    if await self._too_many_failures(failures, title):
                        break
                    continue

                started = time.monotonic()
                self._mark_started()
                log.info("Morceau lancé : %s", track.title)
                await done.wait()
                self._play_started = None
                elapsed = time.monotonic() - started
                log.info("Morceau terminé : %s (%.0fs)", track.title, elapsed)

                if self.closing or not vc.is_connected():
                    break

                short_track = track.duration is not None and track.duration <= 10
                if errors or (not self._skipped and elapsed < MIN_PLAY_SECONDS and not short_track):
                    log.error("Erreur FFmpeg / lecture interrompue sur %r : %s", track.title, errors or "arrêt immédiat")
                    failures += 1
                    if await self._too_many_failures(failures, title):
                        break
                else:
                    failures = 0
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Erreur inattendue dans la boucle de lecture (serveur %s)", self.guild.id)
            await self.notify("⚠️ Une erreur inattendue a interrompu la lecture.")
        finally:
            self.current = None
        # Aucun `await` entre la sortie de la boucle et la fin de la tâche :
        # un /musique concurrent verra donc `task.done()` cohérent.
        if not self.closing:
            self.refresh_idle_timer()

    async def _too_many_failures(self, failures: int, title: str) -> bool:
        """Signale l'échec d'un morceau. Retourne True si la lecture doit être abandonnée."""
        await self.notify(f'⚠️ Impossible de lire "{title}". Passage au suivant.')
        if failures >= MAX_CONSECUTIVE_FAILURES:
            self.queue.clear()
            await self.notify(
                f"⚠️ {failures} morceaux de suite illisibles : lecture interrompue. "
                "Vérifie que yt-dlp et FFmpeg sont à jour."
            )
            return True
        return False


def get_player(guild) -> "GuildPlayer":
    player = players.get(guild.id)
    if player is None or player.closing:
        player = GuildPlayer(guild)
        players[guild.id] = player
    return player


# --------------------------------------------------------------------------- #
# Client Discord
# --------------------------------------------------------------------------- #
class MusicBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()  # inclut voice_states ; aucun intent privilégié requis
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.tree = app_commands.CommandTree(self)
        self._invite_logged = False
        self.started_at = time.time()
        self._dm_last: dict[int, float] = {}  # anti-spam des réponses en message privé

    async def setup_hook(self) -> None:
        """Synchronise les commandes sur tous les serveurs connus + globalement.

        La synchronisation globale de Discord peut être lente. Une copie guild
        est donc synchronisée pour chaque serveur auquel le bot est déjà connecté,
        ce qui rend les slash commands disponibles immédiatement.
        """
        try:
            # Synchronisation globale : nécessaire pour les nouveaux serveurs et
            # pour conserver les commandes enregistrées au niveau de l'application.
            global_synced = await self.tree.sync()
            log.info("%d commandes synchronisées globalement", len(global_synced))

            # Synchronisation par serveur : Discord l'applique immédiatement,
            # contrairement aux commandes globales qui peuvent prendre du temps.
            for guild in self.guilds:
                try:
                    self.tree.copy_global_to(guild=guild)
                    guild_synced = await self.tree.sync(guild=guild)
                    log.info(
                        "%d commandes synchronisées sur le serveur %s (%s)",
                        len(guild_synced),
                        guild.name,
                        guild.id,
                    )
                except Exception:
                    log.exception(
                        "Échec de la synchronisation des commandes sur %s (%s)",
                        guild.name,
                        guild.id,
                    )
        except Exception:
            log.exception("Échec de la synchronisation globale des commandes")

    async def on_guild_join(self, guild: discord.Guild) -> None:
        """Synchronise immédiatement les slash commands lorsqu'un nouveau serveur ajoute le bot."""
        try:
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info(
                "%d commandes synchronisées sur le nouveau serveur %s (%s)",
                len(synced),
                guild.name,
                guild.id,
            )
        except Exception:
            log.exception(
                "Échec de la synchronisation des commandes sur le nouveau serveur %s (%s)",
                guild.name,
                guild.id,
            )

    async def on_ready(self) -> None:
        log.info("Bot connecté : %s (id %s) - %d serveur(s)", self.user, self.user.id, len(self.guilds))
        if DISCORD_CLIENT_ID and not self._invite_logged:
            self._invite_logged = True
            perms = discord.Permissions(view_channel=True, send_messages=True, connect=True, speak=True)
            invite = discord.utils.oauth_url(
                DISCORD_CLIENT_ID, permissions=perms, scopes=("bot", "applications.commands")
            )
            log.info("Lien d'invitation : %s", invite)

    async def on_message(self, message: discord.Message) -> None:
        """Message privé envoyé au bot : il répond avec l'aide (les slash commands marchent aussi en MP)."""
        if message.guild is not None or message.author.bot:
            return
        now = time.monotonic()
        if now - self._dm_last.get(message.author.id, -DM_REPLY_COOLDOWN) < DM_REPLY_COOLDOWN:
            return
        if len(self._dm_last) > 1000:
            self._dm_last.clear()
        self._dm_last[message.author.id] = now

        channel = find_user_voice(message.author.id)
        if channel is not None:
            status = (
                f"🔊 Je te vois dans **{clean_title(channel.name, 50)}** "
                f"({clean_title(channel.guild.name, 50)}) : je jouerai la musique là-bas."
            )
        else:
            status = "🔇 Je ne te vois dans aucun salon vocal : rejoins-en un sur un serveur où je suis, puis lance une commande."
        text = (
            "👋 Salut ! Je suis un bot musique. Ici, en message privé, tu peux utiliser :\n"
            "• `/musique` : lien YouTube / YouTube Music / Spotify / Deezer\n"
            "• `/search` : nom d'un morceau (artiste, titre...)\n"
            "• `/mp3` : un fichier audio (joins-le à la commande)\n"
            "• `/dash` : tableau de bord avec boutons (pause, suivant, stop…)\n\n"
            f"Je joue dans le salon vocal où tu te trouves.\n{status}"
        )
        try:
            await message.channel.send(text)
        except discord.HTTPException as exc:  # MP fermés, etc.
            log.debug("Réponse en MP impossible : %s", exc)

    async def on_voice_state_update(self, member, before, after) -> None:
        player = players.get(member.guild.id)
        if player is None:
            return
        if member.id == self.user.id:
            # Le bot a été déconnecté (kick, salon supprimé...) : on laisse 3 s au cas où
            # discord.py se reconnecte tout seul, puis on nettoie.
            if before.channel is not None and after.channel is None and not player.closing:
                await asyncio.sleep(3)
                vc = member.guild.voice_client
                if not player.closing and (vc is None or not vc.is_connected()):
                    await player.cleanup("déconnecté du salon vocal")
            return
        player.refresh_idle_timer()

    async def close(self) -> None:
        for player in list(players.values()):
            try:
                await player.cleanup("arrêt du bot")
            except Exception:
                log.exception("Erreur lors du nettoyage à l'arrêt")
        await super().close()


bot = MusicBot()


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    log.error("Erreur dans la commande /%s : %r", getattr(interaction.command, "name", "?"), error, exc_info=error)
    message = "⚠️ Une erreur inattendue est survenue. Réessaie dans un instant."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        pass


async def reject(interaction: discord.Interaction, message: str) -> None:
    await interaction.response.send_message(message, ephemeral=True)


def find_user_voice(user_id: int):
    """Salon vocal où se trouve l'utilisateur, parmi tous les serveurs du bot (sert aux commandes en MP).

    Un utilisateur ne peut être que dans un seul salon vocal à la fois. Utilise uniquement l'intent
    voice_states (déjà actif) : aucun intent privilégié n'est nécessaire.
    """
    for guild in bot.guilds:
        for channel in (*guild.voice_channels, *guild.stage_channels):
            if user_id in channel.voice_states:
                return channel
    return None


def user_voice_channel(interaction: discord.Interaction):
    """Salon vocal de l'utilisateur : celui du membre sur un serveur, ou trouvé dans les serveurs en commun en MP."""
    if interaction.guild is not None:
        voice = getattr(interaction.user, "voice", None)
        return voice.channel if voice is not None else None
    return find_user_voice(interaction.user.id)


def target_guild(interaction: discord.Interaction) -> discord.Guild | None:
    """Serveur visé par la commande : celui où elle est lancée, ou (en MP) celui du salon vocal de l'utilisateur."""
    if interaction.guild is not None:
        return interaction.guild
    channel = find_user_voice(interaction.user.id)
    return channel.guild if channel is not None else None


async def get_controlled_player(interaction: discord.Interaction) -> GuildPlayer | None:
    """Vérifie qu'un lecteur actif existe et que l'utilisateur est dans le même salon que le bot."""
    guild = target_guild(interaction)
    if guild is None:  # MP et utilisateur dans aucun salon vocal
        await reject(interaction, "❌ Tu dois être dans le même salon vocal que le bot.")
        return None
    player = players.get(guild.id)
    vc = guild.voice_client
    if player is None or player.closing or vc is None or not vc.is_connected():
        await reject(interaction, "❌ Le bot n'est connecté à aucun salon vocal.")
        return None
    channel = user_voice_channel(interaction)
    if channel is None or channel.id != vc.channel.id:
        await reject(interaction, "❌ Tu dois être dans le même salon vocal que le bot.")
        return None
    return player


# --------------------------------------------------------------------------- #
# Commandes
# --------------------------------------------------------------------------- #
async def check_voice_access(interaction: discord.Interaction):
    """L'utilisateur est-il en vocal, le bot a-t-il les droits et n'est-il pas utilisé ailleurs ?

    Retourne le salon vocal, ou None (le message d'erreur a alors déjà été envoyé).
    """
    channel = user_voice_channel(interaction)
    if channel is None:
        if interaction.guild is None:
            await reject(
                interaction,
                "❌ Je ne te vois dans aucun salon vocal : rejoins un salon vocal d'un serveur où je suis, "
                "puis relance la commande.",
            )
        else:
            await reject(interaction, "❌ Tu dois être dans un salon vocal pour utiliser cette commande.")
        return None
    guild = channel.guild  # en MP, le serveur est celui du salon vocal de l'utilisateur

    perms = channel.permissions_for(guild.me)
    if not (perms.view_channel and perms.connect and perms.speak):
        await reject(
            interaction,
            "❌ Je n'ai pas les permissions nécessaires dans ce salon vocal (View Channel, Connect, Speak).",
        )
        return None

    vc = guild.voice_client
    if vc is not None and vc.is_connected() and vc.channel.id != channel.id:
        await reject(interaction, "❌ Le bot est déjà utilisé dans un autre salon vocal.")
        return None
    return channel


async def enqueue_tracks(interaction: discord.Interaction, channel, tracks: list[Track]) -> bool | None:
    """Connecte le bot au salon (si besoin) et ajoute les morceaux à la file (sérialisé par serveur).

    Retourne « le lecteur était déjà occupé » (True / False), ou None en cas d'échec
    (le message d'erreur a alors déjà été envoyé via followup : la commande doit avoir été différée).
    """
    guild = channel.guild
    player = get_player(guild)
    async with player.lock:
        vc = guild.voice_client
        if vc is not None and vc.is_connected() and vc.channel.id != channel.id:
            await interaction.followup.send("❌ Le bot est déjà utilisé dans un autre salon vocal.")
            return None

        if vc is None or not vc.is_connected():
            try:
                if vc is not None:  # client vocal périmé
                    await vc.disconnect(force=True)
                log.info("Connexion au salon vocal %s (serveur %s)", channel.id, guild.id)
                await channel.connect(timeout=30.0, reconnect=True, self_deaf=True)
            except asyncio.TimeoutError:
                log.error("Connexion vocale : délai dépassé")
                await _drop_if_unused(player)
                await interaction.followup.send("⏱️ Impossible de rejoindre le salon vocal (délai dépassé).")
                return None
            except discord.Forbidden:
                await _drop_if_unused(player)
                await interaction.followup.send("❌ Je n'ai pas la permission de rejoindre ce salon vocal.")
                return None
            except Exception as exc:
                log.exception("Connexion vocale impossible")
                await _drop_if_unused(player)
                await interaction.followup.send(f"❌ Impossible de rejoindre le salon vocal : {exc}")
                return None

        already_busy = player.current is not None or bool(player.queue)
        if interaction.guild is not None or player.text_channel is None:  # un MP ne remplace pas le salon texte du serveur
            player.text_channel = interaction.channel
        player.queue.extend(tracks)
        player.cancel_idle_timer()
        player.ensure_task()
        player.refresh_idle_timer()  # ne démarre que si le bot est seul dans le salon
    return already_busy


@bot.tree.command(name="musique", description="Ajoute une playlist ou une vidéo YouTube / YouTube Music à la file d'attente")
@app_commands.describe(url="Lien d'une playlist ou d'une vidéo YouTube / YouTube Music")
async def musique(interaction: discord.Interaction, url: str):
    log.info("Commande /musique reçue (serveur %s, par %s)", interaction.guild_id or "MP", interaction.user)
    member = interaction.user

    channel = await check_voice_access(interaction)
    if channel is None:
        return

    source = identify_source(url)
    if source is None:
        return await reject(
            interaction,
            "❌ Lien invalide. Donne un lien de vidéo ou playlist YouTube / YouTube Music, "
            "ou une piste, un album ou une playlist Spotify / Deezer.",
        )
    service, clean_url = source

    await interaction.response.defer(thinking=True)

    # 1) métadonnées de la playlist / album / piste (yt-dlp dans un thread, HTTP en asynchrone)
    try:
        tracks, skipped, notes = await asyncio.wait_for(
            load_source(service, clean_url, member.display_name), LOAD_TIMEOUT
        )
    except asyncio.TimeoutError:
        log.error("Chargement %s : délai dépassé pour %s", service, clean_url)
        return await interaction.followup.send(
            f"⏱️ {SERVICE_LABELS[service]} met trop de temps à répondre. Réessaie dans un instant."
        )
    except SourceError as exc:
        log.warning("Chargement %s impossible : %s", service, exc)
        return await interaction.followup.send(str(exc))
    except yt_dlp.utils.YoutubeDLError as exc:
        log.error("Erreur yt-dlp : %s", exc)
        return await interaction.followup.send(describe_ytdlp_error(exc))
    except Exception:
        log.exception("Erreur inattendue au chargement de %s", clean_url)
        return await interaction.followup.send("⚠️ Impossible de charger ce lien.")

    if not tracks:
        extra = f" ({skipped} morceau(x) indisponible(s) ignoré(s))" if skipped else ""
        return await interaction.followup.send(f"❌ Aucun morceau lisible trouvé dans ce lien{extra}.")

    # 2) connexion + ajout à la queue
    already_busy = await enqueue_tracks(interaction, channel, tracks)
    if already_busy is None:
        return

    count = len(tracks)
    noun = "morceau ajouté" if count == 1 else "morceaux ajoutés"
    tail = "à la suite de la file d'attente" if already_busy else "à la file d'attente"
    message = f"🎵 {count} {noun} {tail}."
    if skipped:
        message += f"\n⚠️ {skipped} morceau(x) indisponible(s) ignoré(s)."
    if count >= MAX_PLAYLIST_ITEMS:
        message += f"\nℹ️ Playlist limitée aux {MAX_PLAYLIST_ITEMS} premiers morceaux."
    for note in notes:
        message += f"\n{note}"
    if service != "youtube":
        message += f"\nℹ️ Les morceaux {SERVICE_LABELS[service]} sont lus depuis YouTube (recherche au moment de la lecture)."
    await interaction.followup.send(message)


async def _drop_if_unused(player: GuildPlayer) -> None:
    """Après un échec de connexion, ne garde pas un lecteur vide en mémoire."""
    if player.current is None and not player.queue and player.voice_client is None:
        await player.cleanup("connexion vocale échouée")


@bot.tree.command(name="search", description="Cherche un morceau par son nom (artiste, titre...) et joue le résultat le plus probable")
@app_commands.describe(nom="Nom du morceau à chercher, par exemple « artiste - titre »")
async def search(interaction: discord.Interaction, nom: str):
    log.info("Commande /search reçue (serveur %s, par %s)", interaction.guild_id or "MP", interaction.user)
    member = interaction.user

    nom = nom.strip()
    if not nom:
        return await reject(interaction, "❌ Donne le nom d'un morceau à chercher.")

    channel = await check_voice_access(interaction)
    if channel is None:
        return

    await interaction.response.defer(thinking=True)

    try:
        track = await asyncio.wait_for(
            asyncio.to_thread(search_track_blocking, nom, member.display_name), LOAD_TIMEOUT
        )
    except asyncio.TimeoutError:
        log.error("Recherche : délai dépassé pour « %s »", nom)
        return await interaction.followup.send("⏱️ La recherche met trop de temps à répondre. Réessaie dans un instant.")
    except yt_dlp.utils.YoutubeDLError as exc:
        log.error("Erreur yt-dlp (recherche) : %s", exc)
        return await interaction.followup.send(describe_ytdlp_error(exc))
    except Exception as exc:
        log.warning("Recherche impossible pour « %s » : %s", nom, exc)
        return await interaction.followup.send(f"❌ Aucun résultat trouvé pour « {clean_title(nom, 80)} ».")

    already_busy = await enqueue_tracks(interaction, channel, [track])
    if already_busy is None:
        return

    tail = "à la suite de la file d'attente" if already_busy else "à la file d'attente"
    label = f"[{clean_title(track.title, 80)}]({track.url})" if track.url else clean_title(track.title, 80)
    await interaction.followup.send(f"🔎 **{label}** ajouté {tail}.")


@bot.tree.command(name="skip", description="Passe au morceau suivant")
@app_commands.guild_only()
async def skip(interaction: discord.Interaction):
    log.info("Commande /skip reçue (serveur %s)", interaction.guild_id)
    player = await get_controlled_player(interaction)
    if player is None:
        return
    if player.current is None:
        return await reject(interaction, "❌ Aucun morceau en cours de lecture.")
    title = clean_title(player.current.title, 80)
    player.skip()
    await interaction.response.send_message(f"⏭️ Morceau passé : **{title}**")


@bot.tree.command(name="pause", description="Met la musique en pause")
@app_commands.guild_only()
async def pause(interaction: discord.Interaction):
    log.info("Commande /pause reçue (serveur %s)", interaction.guild_id)
    player = await get_controlled_player(interaction)
    if player is None:
        return
    vc = player.voice_client
    if vc.is_paused():
        return await reject(interaction, "ℹ️ La musique est déjà en pause.")
    if not vc.is_playing():
        return await reject(interaction, "❌ Aucun morceau en cours de lecture.")
    player.pause()
    await interaction.response.send_message("⏸️ Musique en pause.")


@bot.tree.command(name="resume", description="Reprend la musique")
@app_commands.guild_only()
async def resume(interaction: discord.Interaction):
    log.info("Commande /resume reçue (serveur %s)", interaction.guild_id)
    player = await get_controlled_player(interaction)
    if player is None:
        return
    vc = player.voice_client
    if not vc.is_paused():
        return await reject(interaction, "ℹ️ La musique n'est pas en pause.")
    player.resume()
    await interaction.response.send_message("▶️ Lecture reprise.")


@bot.tree.command(name="replay", description="Relance le morceau en cours depuis le début")
@app_commands.guild_only()
async def replay(interaction: discord.Interaction):
    log.info("Commande /replay reçue (serveur %s)", interaction.guild_id)
    player = await get_controlled_player(interaction)
    if player is None:
        return
    if player.current is None:
        return await reject(interaction, "❌ Aucun morceau en cours de lecture.")
    title = clean_title(player.current.title, 80)
    if not player.replay():
        return await reject(interaction, "⏳ Le morceau est en cours de chargement, réessaie dans un instant.")
    await interaction.response.send_message(f"🔁 Relecture depuis le début : **{title}**")


@bot.tree.command(name="stop", description="Arrête la musique, vide la file d'attente et quitte le salon vocal")
@app_commands.guild_only()
async def stop(interaction: discord.Interaction):
    log.info("Commande /stop reçue (serveur %s)", interaction.guild_id)
    player = await get_controlled_player(interaction)
    if player is None:
        return
    await interaction.response.send_message("⏹️ Musique arrêtée, file d'attente vidée. À bientôt !")
    await player.cleanup("commande /stop")


@bot.tree.command(name="alea", description="Lecture aléatoire : mélange les prochains morceaux de la file d'attente")
@app_commands.guild_only()
async def alea(interaction: discord.Interaction):
    log.info("Commande /alea reçue (serveur %s)", interaction.guild_id)
    player = await get_controlled_player(interaction)
    if player is None:
        return
    if len(player.queue) < 2:
        return await reject(interaction, "❌ Il faut au moins 2 morceaux à suivre pour mélanger la file d'attente.")
    count = player.shuffle()
    log.info("File mélangée (serveur %s) : %d morceaux", interaction.guild_id, count)
    await interaction.response.send_message(f"🔀 File d'attente mélangée ({count} morceaux à suivre). Utilise /queue pour voir le nouvel ordre.")


@bot.tree.command(name="queue", description="Affiche le morceau en cours et les prochains morceaux")
@app_commands.guild_only()
async def queue_cmd(interaction: discord.Interaction):
    log.info("Commande /queue reçue (serveur %s)", interaction.guild_id)
    player = players.get(interaction.guild_id)
    if player is None or player.closing or (player.current is None and not player.queue):
        return await interaction.response.send_message("📭 La file d'attente est vide.", ephemeral=True)

    lines: list[str] = []
    current = player.current
    if current is not None:
        state = " ⏸️ *(en pause)*" if player.is_paused else ""
        shown = clean_title(current.title, 80)
        label = f"[{shown}]({current.url})" if current.url else shown
        lines.append(f"**▶️ En cours :** {label} `{fmt_duration(current.duration)}`{state}")
    else:
        lines.append("**▶️ En cours :** chargement du morceau suivant…")

    upcoming = list(itertools.islice(player.queue, QUEUE_DISPLAY_LIMIT))
    total_upcoming = len(player.queue)
    if upcoming:
        lines.append("")
        lines.append(f"**⏭️ À suivre ({total_upcoming}) :**")
        for position, track in enumerate(upcoming, start=1):
            lines.append(f"`{position}.` {clean_title(track.title, 70)} `{fmt_duration(track.duration)}`")
        remaining = total_upcoming - len(upcoming)
        if remaining > 0:
            lines.append(f"… et **{remaining}** autre(s) morceau(x)")
    else:
        lines.append("")
        lines.append("Aucun autre morceau à suivre.")

    embed = discord.Embed(title="🎵 File d'attente", description="\n".join(lines)[:4000], color=discord.Color.blurple())
    await interaction.response.send_message(embed=embed)


# --------------------------------------------------------------------------- #
# /mp3 : lecture de fichiers audio (pièce jointe Discord ou bibliothèque MP3_DIR)
# --------------------------------------------------------------------------- #
async def mp3_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    try:
        files = await get_local_listing()
    except Exception:
        log.exception("Autocomplétion /mp3 : lecture de MP3_DIR impossible")
        return []
    needle = current.strip().lower()
    matches = [f for f in files if len(f) <= 100 and needle in f.lower()]
    return [app_commands.Choice(name=f, value=f) for f in matches[:25]]


@bot.tree.command(name="mp3", description="Joue un fichier audio MP3 (envoyé sur Discord ou pris dans la bibliothèque du bot)")
@app_commands.describe(
    fichier="Fichier audio à envoyer (mp3, wav, ogg, flac, m4a, opus)",
    nom="Fichier de la bibliothèque du bot (dossier MP3_DIR)",
)
@app_commands.autocomplete(nom=mp3_autocomplete)
async def mp3_cmd(
    interaction: discord.Interaction,
    fichier: Optional[discord.Attachment] = None,
    nom: Optional[str] = None,
):
    log.info("Commande /mp3 reçue (serveur %s, par %s)", interaction.guild_id or "MP", interaction.user)
    member = interaction.user

    if fichier is None and nom is None:
        return await reject(
            interaction,
            "❌ Joins un fichier audio (option « fichier ») ou choisis-en un dans la bibliothèque du bot (option « nom »).",
        )
    if fichier is not None and nom is not None:
        return await reject(interaction, "❌ Utilise soit « fichier », soit « nom », pas les deux en même temps.")
    if fichier is not None:
        if not is_audio_attachment(fichier):
            return await reject(
                interaction, "❌ Ce fichier n'est pas un fichier audio (formats acceptés : mp3, wav, ogg, flac, m4a, opus)."
            )
        if not is_discord_cdn_url(fichier.url):
            return await reject(interaction, "❌ Pièce jointe non reconnue.")

    channel = await check_voice_access(interaction)
    if channel is None:
        return

    await interaction.response.defer(thinking=True)  # ffprobe / lecture du dossier peuvent prendre quelques secondes

    try:
        if fichier is not None:
            fallback = Path(fichier.filename).stem.replace("_", " ").strip() or "Fichier audio"
            track = await build_audio_track(fichier.url, fallback, member.display_name, "attachment")
        else:
            path, error = await find_local_file(nom)
            if path is None:
                return await interaction.followup.send(error or "❌ Fichier introuvable.")
            fallback = path.stem.replace("_", " ").strip() or "Fichier audio"
            track = await build_audio_track(str(path), fallback, member.display_name, "local")
    except Exception:
        log.exception("Chargement du fichier audio impossible")
        return await interaction.followup.send("⚠️ Impossible de charger ce fichier audio.")

    already_busy = await enqueue_tracks(interaction, channel, [track])
    if already_busy is None:
        return

    tail = "à la suite de la file d'attente" if already_busy else "à la file d'attente"
    message = f"🎵 **{clean_title(track.title, 80)}** ajouté {tail}."
    if track.duration is None:
        message += "\nℹ️ Durée inconnue (ffprobe introuvable ou fichier illisible) : la barre de progression du /dash sera indisponible."
    await interaction.followup.send(message)


# --------------------------------------------------------------------------- #
# /dash : tableau de bord (embeds + boutons, actualisé automatiquement)
# --------------------------------------------------------------------------- #
active_dashes: dict[tuple[str, int], "DashView"] = {}  # clé : ("g", id serveur) ou ("u", id utilisateur) en MP


def dash_key(interaction: discord.Interaction) -> tuple[str, int]:
    return ("g", interaction.guild.id) if interaction.guild is not None else ("u", interaction.user.id)


def _fmt_uptime(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days} j {hours} h {minutes:02d} min"
    if hours:
        return f"{hours} h {minutes:02d} min"
    return f"{minutes} min"


def _progress_bar(elapsed: float, duration: int, width: int = 14) -> str:
    ratio = min(max(elapsed / duration, 0.0), 1.0)
    position = min(int(ratio * width), width - 1)
    return "▬" * position + "🔘" + "▬" * (width - position - 1)


async def build_dash_embeds(guild: discord.Guild | None, show_guild: bool = False) -> list[discord.Embed]:
    """Les 3 embeds du tableau de bord : lecture en cours, file d'attente, statistiques.

    guild vaut None en MP quand l'utilisateur n'est dans aucun salon vocal (seules les stats sont utiles).
    """
    player = players.get(guild.id) if guild is not None else None
    active = player is not None and not player.closing
    vc = guild.voice_client if guild is not None else None
    current = player.current if active else None
    upcoming = list(player.queue) if active else []

    # 1) lecture en cours
    if current is not None:
        paused = player.is_paused
        shown = clean_title(current.title, 90)
        label = f"[{shown}]({current.url})" if current.url else shown
        now = discord.Embed(
            title="⏸️ En pause" if paused else "🎶 En cours de lecture",
            description=f"**{label}**",
            color=discord.Color.orange() if paused else discord.Color.green(),
        )
        elapsed = player.elapsed()
        if elapsed is None:
            progress = "⏳ Chargement du flux…"
        else:
            clock = fmt_duration(elapsed) if elapsed >= 1 else "0:00"
            if current.duration:
                progress = f"{_progress_bar(elapsed, current.duration)}\n`{clock} / {fmt_duration(current.duration)}`"
            else:
                progress = f"`{clock}` (durée inconnue)"
        now.add_field(name="Progression", value=progress, inline=False)
        now.add_field(name="Demandé par", value=clean_title(current.requested_by, 40), inline=True)
        now.add_field(name="Source", value=KIND_LABELS.get(current.kind, "YouTube"), inline=True)
        if vc is not None and vc.channel is not None:
            now.add_field(name="Salon vocal", value=f"{vc.channel.mention} · 👥 {player.human_count()}", inline=True)
        if show_guild:
            now.add_field(name="Serveur", value=clean_title(guild.name, 60), inline=True)
    elif active and upcoming:
        now = discord.Embed(
            title="⏳ Chargement",
            description="Chargement du morceau suivant…",
            color=discord.Color.blurple(),
        )
    elif guild is None:
        now = discord.Embed(
            title="🔇 Aucun salon vocal",
            description=(
                "Je ne te vois dans aucun salon vocal d'un serveur où je suis. Rejoins-en un, "
                "puis utilise `/musique` ou `/mp3` ici : le tableau de bord suivra tout seul."
            ),
            color=discord.Color.greyple(),
        )
    else:
        now = discord.Embed(
            title="💤 Rien en cours de lecture",
            description="Lance de la musique avec `/musique` (YouTube, Spotify, Deezer) ou `/mp3` (fichier audio).",
            color=discord.Color.greyple(),
        )

    # 2) file d'attente
    queue_embed = discord.Embed(title=f"📜 File d'attente ({len(upcoming)})", color=discord.Color.blurple())
    if upcoming:
        lines = [
            f"`{position}.` {clean_title(track.title, 60)} `{fmt_duration(track.duration)}`"
            for position, track in enumerate(upcoming[:DASH_QUEUE_LIMIT], start=1)
        ]
        remaining = len(upcoming) - DASH_QUEUE_LIMIT
        if remaining > 0:
            lines.append(f"… et **{remaining}** autre(s) morceau(x)")
        known = sum(track.duration or 0 for track in upcoming)
        if known:
            lines.append(f"\n⏱️ Durée cumulée connue : `{fmt_duration(known)}`")
        queue_embed.description = "\n".join(lines)
    else:
        queue_embed.description = "Aucun morceau à suivre."

    # 3) statistiques
    stats = discord.Embed(title="📊 Statistiques", color=discord.Color.dark_grey())
    latency = bot.latency
    library = f"{len(await get_local_listing())} fichier(s)" if _mp3_root() is not None else "non configurée"
    stats.add_field(name="Serveurs", value=str(len(bot.guilds)), inline=True)
    stats.add_field(name="Lecteurs actifs", value=str(len(players)), inline=True)
    stats.add_field(name="Latence", value=f"{latency * 1000:.0f} ms" if math.isfinite(latency) else "?", inline=True)
    stats.add_field(name="En ligne depuis", value=_fmt_uptime(time.time() - bot.started_at), inline=True)
    stats.add_field(name="Inactivité", value=f"{IDLE_TIMEOUT} s" if IDLE_TIMEOUT > 0 else "désactivée", inline=True)
    stats.add_field(name="Bibliothèque MP3", value=library, inline=True)
    stats.set_footer(text=f"Actualisé toutes les {DASH_REFRESH} s")
    stats.timestamp = discord.utils.utcnow()

    return [now, queue_embed, stats]


class DashView(discord.ui.View):
    """Boutons du /dash. Le message s'actualise seul jusqu'à expiration du jeton d'interaction (15 min)."""

    def __init__(self, interaction: discord.Interaction):
        super().__init__(timeout=DASH_LIFETIME)
        self.origin = interaction
        self.in_dm = interaction.guild is None
        self.user_id = interaction.user.id
        self.key = dash_key(interaction)
        self.fixed_guild = interaction.guild  # None en MP : le serveur suit alors le salon vocal de l'utilisateur
        self.created = time.monotonic()
        self.expired = False
        self.refresh_task: asyncio.Task | None = None

    def current_guild(self) -> discord.Guild | None:
        if not self.in_dm:
            return self.fixed_guild
        channel = find_user_voice(self.user_id)  # MP : on suit l'utilisateur d'un salon vocal à l'autre
        return channel.guild if channel is not None else None

    async def render(self) -> list[discord.Embed]:
        return await build_dash_embeds(self.current_guild(), show_guild=self.in_dm)

    def start(self) -> None:
        active_dashes[self.key] = self
        self.refresh_task = asyncio.create_task(self._auto_refresh(), name=f"dash-{self.key[0]}{self.key[1]}")

    def _unregister(self) -> None:
        if active_dashes.get(self.key) is self:
            active_dashes.pop(self.key, None)

    async def _update(self, interaction: discord.Interaction, delay: float = 0.0) -> None:
        """Réaffiche le tableau de bord après une action (l'interaction doit avoir été différée)."""
        if delay:
            await asyncio.sleep(delay)
        await interaction.edit_original_response(embeds=await self.render(), view=self)

    async def _auto_refresh(self) -> None:
        try:
            while not self.expired:
                await asyncio.sleep(DASH_REFRESH)
                if self.expired:
                    return
                if time.monotonic() - self.created >= DASH_LIFETIME - DASH_REFRESH:
                    break
                await self.origin.edit_original_response(embeds=await self.render(), view=self)
        except asyncio.CancelledError:
            raise
        except discord.HTTPException as exc:  # message supprimé, jeton expiré...
            log.debug("Tableau de bord : actualisation arrêtée (%s)", exc)
            self.expired = True
            self._unregister()
            self.stop()
            return
        await self.finish()

    async def finish(self, note: str = "Tableau de bord expiré : relance /dash pour en afficher un nouveau.") -> None:
        """Désactive les boutons et fige le message (idempotent)."""
        if self.expired:
            return
        self.expired = True
        self._unregister()
        task = self.refresh_task
        self.refresh_task = None
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
        for item in self.children:
            item.disabled = True
        try:
            embeds = await self.render()
            embeds[-1].set_footer(text=note)
            await self.origin.edit_original_response(embeds=embeds, view=self)
        except discord.HTTPException as exc:
            log.debug("Tableau de bord : impossible de figer le message (%s)", exc)
        self.stop()

    async def on_timeout(self) -> None:
        await self.finish()

    # -- boutons ---------------------------------------------------------- #
    @discord.ui.button(label="Pause / Reprendre", emoji="⏯️", style=discord.ButtonStyle.primary, row=0)
    async def btn_toggle(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = await get_controlled_player(interaction)
        if player is None:
            return
        vc = player.voice_client
        if vc.is_paused():
            player.resume()
        elif vc.is_playing():
            player.pause()
        else:
            return await reject(interaction, "❌ Aucun morceau en cours de lecture.")
        await interaction.response.defer()
        await self._update(interaction)

    @discord.ui.button(label="Suivant", emoji="⏭️", style=discord.ButtonStyle.secondary, row=0)
    async def btn_skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = await get_controlled_player(interaction)
        if player is None:
            return
        if player.current is None:
            return await reject(interaction, "❌ Aucun morceau en cours de lecture.")
        player.skip()
        await interaction.response.defer()
        await self._update(interaction, delay=1.5)  # laisse le temps au morceau suivant de démarrer

    @discord.ui.button(label="Rejouer", emoji="🔁", style=discord.ButtonStyle.secondary, row=0)
    async def btn_replay(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = await get_controlled_player(interaction)
        if player is None:
            return
        if not player.replay():
            return await reject(interaction, "❌ Aucun morceau en cours de lecture.")
        await interaction.response.defer()
        await self._update(interaction, delay=1.5)

    @discord.ui.button(label="Mélanger", emoji="🔀", style=discord.ButtonStyle.secondary, row=0)
    async def btn_shuffle(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = await get_controlled_player(interaction)
        if player is None:
            return
        if len(player.queue) < 2:
            return await reject(interaction, "❌ Il faut au moins 2 morceaux à suivre pour mélanger la file d'attente.")
        player.shuffle()
        await interaction.response.defer()
        await self._update(interaction)

    @discord.ui.button(label="Stop", emoji="⏹️", style=discord.ButtonStyle.danger, row=0)
    async def btn_stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = await get_controlled_player(interaction)
        if player is None:
            return
        await interaction.response.defer()
        await player.cleanup("bouton du tableau de bord")
        await self._update(interaction)

    @discord.ui.button(label="Actualiser", emoji="🔄", style=discord.ButtonStyle.secondary, row=1)
    async def btn_refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await self._update(interaction)


@bot.tree.command(name="dash", description="Affiche le tableau de bord : lecture en cours, file d'attente et statistiques")
async def dash(interaction: discord.Interaction):
    log.info("Commande /dash reçue (serveur %s, par %s)", interaction.guild_id or "MP", interaction.user)
    await interaction.response.defer()
    previous = active_dashes.get(dash_key(interaction))
    if previous is not None:  # un seul tableau de bord actif par serveur (ou par utilisateur en MP)
        await previous.finish("Remplacé par un nouveau tableau de bord.")
    view = DashView(interaction)
    await interaction.followup.send(embeds=await view.render(), view=view)
    view.start()


# --------------------------------------------------------------------------- #
# /clear : suppression des messages du bot
# --------------------------------------------------------------------------- #
@bot.tree.command(name="clear", description="Supprime tous les messages du bot dans ce salon (ou ce message privé)")
async def clear(interaction: discord.Interaction):
    log.info("Commande /clear reçue (serveur %s, par %s)", interaction.guild_id or "MP", interaction.user)
    channel = interaction.channel
    if channel is None:
        return await reject(interaction, "❌ Salon introuvable.")
    if interaction.guild is not None and hasattr(channel, "permissions_for"):
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.view_channel and perms.read_message_history):
            return await reject(
                interaction,
                "❌ Il me manque la permission « Voir les anciens messages » (Read Message History) dans ce salon.",
            )

    await interaction.response.defer(ephemeral=True)  # la réponse à /clear ne laisse aucun message dans le salon

    deleted = failed = scanned = 0
    mine: list[discord.Message] = []
    try:
        # on liste d'abord, puis on supprime : évite de modifier l'historique pendant qu'on le parcourt
        async for message in channel.history(limit=CLEAR_SCAN_LIMIT):
            scanned += 1
            if message.author.id == bot.user.id:
                mine.append(message)
    except discord.Forbidden:
        return await interaction.followup.send("❌ Je n'ai pas accès à l'historique de ce salon.", ephemeral=True)
    except discord.HTTPException as exc:
        log.warning("/clear : lecture de l'historique impossible (%s)", exc)
        return await interaction.followup.send("⚠️ Impossible de lire l'historique de ce salon.", ephemeral=True)

    for message in mine:
        try:
            await message.delete()
            deleted += 1
        except discord.NotFound:
            pass  # déjà supprimé (par exemple un /dash qui vient d'expirer)
        except discord.HTTPException as exc:
            failed += 1
            log.debug("/clear : suppression impossible (%s)", exc)

    if deleted == 0 and failed == 0:
        text = "🧹 Je n'ai aucun message à supprimer ici."
    else:
        text = f"🧹 {deleted} message(s) supprimé(s)."
        if failed:
            text += f" ⚠️ {failed} n'ont pas pu être supprimés."
        if scanned >= CLEAR_SCAN_LIMIT:
            text += f" (Seuls les {CLEAR_SCAN_LIMIT} derniers messages du salon sont examinés : relance /clear si besoin.)"
    log.info("/clear : %d supprimé(s), %d échec(s)", deleted, failed)
    await interaction.followup.send(text, ephemeral=True)

    if interaction.guild is None:  # en MP, « éphémère » n'existe pas : on efface aussi cette confirmation
        await asyncio.sleep(5)
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            pass


# --------------------------------------------------------------------------- #
# Démarrage
# --------------------------------------------------------------------------- #
def main() -> None:
    if not DISCORD_TOKEN:
        log.critical("DISCORD_TOKEN manquant : copie .env.example en .env et renseigne le token.")
        sys.exit(1)
    if shutil.which(FFMPEG_PATH) is None and not os.path.isfile(FFMPEG_PATH):
        log.critical("FFmpeg introuvable (%r). Installe-le et ajoute-le au PATH (voir README).", FFMPEG_PATH)
        sys.exit(1)
    if MP3_DIR and _mp3_root() is None:
        log.warning("MP3_DIR (%r) n'est pas un dossier existant : la bibliothèque MP3 est désactivée.", MP3_DIR)
    elif MP3_DIR:
        log.info("Bibliothèque MP3 : %s", _mp3_root())
    try:
        from discord.voice_client import has_nacl
        if not has_nacl:
            log.critical("PyNaCl n'est pas installé : pip install -r requirements.txt")
            sys.exit(1)
    except ImportError:
        pass
    try:
        bot.run(DISCORD_TOKEN, log_handler=None)
    except discord.LoginFailure:
        log.critical("Token Discord invalide : vérifie DISCORD_TOKEN dans .env.")
        sys.exit(1)
    except (discord.HTTPException, OSError) as exc:
        log.critical("Connexion à Discord impossible : %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
