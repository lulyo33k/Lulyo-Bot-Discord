#!/usr/bin/env python3
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BOT_FOLDERS = [
    ROOT / "annonce",
    ROOT / "Sondage",
    ROOT / "Ticket",
]


def start_bot(bot_dir: Path):
    bot_file = bot_dir / "bot.py"
    if not bot_file.exists():
        print(f"[WARN] {bot_dir.name}: bot.py introuvable, ignoré.")
        return None

    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")

    if sys.platform.startswith("win"):
        creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        process = subprocess.Popen(
            [sys.executable, str(bot_file)],
            cwd=str(bot_dir),
            env=env,
            creationflags=creationflags,
        )
    else:
        process = subprocess.Popen(
            [sys.executable, str(bot_file)],
            cwd=str(bot_dir),
            env=env,
            start_new_session=True,
        )

    print(f"[OK] {bot_dir.name}: démarré (PID {process.pid})")
    return process


def stop_all(processes):
    for proc in processes:
        if proc and proc.poll() is None:
            try:
                if sys.platform.startswith("win"):
                    proc.terminate()
                else:
                    os.killpg(proc.pid, signal.SIGTERM)
            except Exception as e:
                print(f"[WARN] Impossible d'arrêter le processus {proc.pid}: {e}")
    for proc in processes:
        if proc:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    if sys.platform.startswith("win"):
                        proc.kill()
                    else:
                        os.killpg(proc.pid, signal.SIGKILL)
                except Exception:
                    pass


if __name__ == "__main__":
    print("Démarrage des bots Discord...")
    processes = []

    for folder in BOT_FOLDERS:
        proc = start_bot(folder)
        if proc is not None:
            processes.append(proc)

    if not processes:
        print("Aucun bot trouvé. Vérifie les dossiers et les fichiers bot.py.")
        sys.exit(1)

    print("Tous les bots sont lancés. Appuie sur Ctrl+C pour tout arrêter.")

    try:
        while True:
            for proc in processes:
                if proc.poll() is not None:
                    print(f"[INFO] Un bot a quitté (PID {proc.pid}), code de sortie: {proc.returncode}")
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nArrêt demandé, fermeture des bots...")
        stop_all(processes)
        print("Tous les bots ont été arrêtés.")
        sys.exit(0)
