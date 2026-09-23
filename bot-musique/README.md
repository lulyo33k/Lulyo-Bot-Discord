# Bot Discord musical (YouTube / YouTube Music)

Un bot Discord qui lit dans un salon vocal des playlists ou vidéos YouTube / YouTube Music.
**Tout le code est dans un seul fichier : `bot.py`.**

Chaîne audio : `yt-dlp` (flux résolu au moment de la lecture) → `FFmpeg` → Discord. Rien n'est téléchargé sur le disque.

## Installation

### 1. Python

1. Télécharge Python **3.11 ou plus récent** sur <https://www.python.org/downloads/>.
2. Pendant l'installation, **coche « Add python.exe to PATH »**.
3. Vérifie dans PowerShell :

```powershell
python --version
```

## FFmpeg

FFmpeg doit être installé séparément et accessible depuis le `PATH`.

**Méthode rapide (winget, Windows 10/11) :**

```powershell
winget install Gyan.FFmpeg
```

**Méthode manuelle :**

1. Télécharge une version « release essentials » sur <https://www.gyan.dev/ffmpeg/builds/> et extrais l'archive, par exemple dans `C:\ffmpeg`.
2. Ajoute `C:\ffmpeg\bin` au `PATH` :
   Menu Démarrer → « Modifier les variables d'environnement système » → **Variables d'environnement** → variable `Path` (utilisateur) → **Nouveau** → `C:\ffmpeg\bin` → OK.
3. **Ferme et rouvre PowerShell**, puis vérifie :

```powershell
ffmpeg -version
```

## Installation des dépendances

Dans le dossier du projet :

```powershell
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

> Si PowerShell refuse d'activer l'environnement : `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, puis relance la commande `activate`.

## Configuration

Copie `.env.example` en `.env` :

```powershell
copy .env.example .env
```

Puis renseigne les valeurs :

| Variable | Rôle |
|---|---|
| `DISCORD_TOKEN` | Token du bot (**obligatoire**, à ne jamais partager) |
| `DISCORD_CLIENT_ID` | ID de l'application ; sert à afficher un lien d'invitation au démarrage |
| `DISCORD_GUILD_ID` | *(optionnel)* ID de ton serveur de test : les commandes y apparaissent **immédiatement**. Sans lui, elles sont synchronisées globalement (jusqu'à ~1 h de délai) |
| `IDLE_TIMEOUT` | Secondes avant de quitter le vocal quand le bot est seul (défaut `300`). `0` désactive le timer |
| `MAX_PLAYLIST_ITEMS` | *(optionnel)* Nombre max de morceaux chargés par playlist (défaut `500`) |
| `FFMPEG_PATH` | *(optionnel)* Chemin complet vers `ffmpeg.exe` si tu ne veux pas modifier le `PATH` |

Pour récupérer l'ID d'un serveur : Discord → Paramètres → Avancés → active le **mode développeur**, puis clic droit sur le serveur → **Copier l'identifiant du serveur**.

## Discord Developer Portal

1. Va sur <https://discord.com/developers/applications> → **New Application**.
2. Onglet **General Information** : copie l'**Application ID** → c'est `DISCORD_CLIENT_ID`.
3. Onglet **Bot** : clique sur **Reset Token**, copie le token → c'est `DISCORD_TOKEN`. Aucun « Privileged Gateway Intent » n'est nécessaire.
4. Invite le bot :
   - soit avec le lien affiché dans la console au premier lancement (si `DISCORD_CLIENT_ID` est renseigné) ;
   - soit dans **OAuth2 → URL Generator** : scopes **`bot`** et **`applications.commands`**, puis les permissions ci-dessous. Ouvre l'URL générée et choisis ton serveur.

**Permissions minimales :**

- View Channels
- Send Messages
- Connect
- Speak

## Lancement

```powershell
venv\Scripts\activate
python bot.py
```

Tu dois voir `Bot connecté : ...` dans la console. Arrêt : `Ctrl+C`.

## Commandes

```text
/musique url:<lien>   Playlist YouTube / YouTube Music ou vidéo seule
/skip                 Morceau suivant
/pause                Met en pause
/resume               Reprend la lecture
/stop                 Arrête, vide la file et quitte le salon vocal
/queue                Morceau en cours + prochains morceaux
```

Comportements à connaître :

- Il faut être dans un salon vocal pour utiliser `/musique` ; pour `/skip`, `/pause`, `/resume`, `/stop`, il faut être dans **le même salon que le bot**.
- Si le bot est déjà dans un autre salon vocal du serveur, `/musique` est refusé.
- Un lien de type `watch?v=...&list=...` charge **toute la playlist**.
- Les vidéos supprimées, privées ou indisponibles sont ignorées ; si un morceau ne peut pas être lu, le bot passe au suivant.
- Le bot quitte le salon après `IDLE_TIMEOUT` secondes s'il est **seul**, ou s'il n'a **plus rien à jouer**. Le timer est annulé si quelqu'un revient / si de nouveaux morceaux sont ajoutés.
- Chaque serveur a sa propre file : plusieurs serveurs peuvent lire en même temps.

## Dépannage

- **`FFmpeg introuvable`** : ferme/rouvre le terminal après l'ajout au `PATH`, ou renseigne `FFMPEG_PATH`.
- **Les commandes n'apparaissent pas** : renseigne `DISCORD_GUILD_ID` (synchronisation immédiate) et vérifie que le bot a été invité avec le scope `applications.commands`.
- **Erreurs yt-dlp / YouTube** : YouTube change souvent. Mets à jour : `pip install -U "yt-dlp[default]"`. Si yt-dlp signale un runtime JavaScript manquant, installe Deno (`winget install DenoLand.Deno`) puis relance le bot.
- **Pas de son sous Linux** : installe `libopus` (`sudo apt install libopus0`).
