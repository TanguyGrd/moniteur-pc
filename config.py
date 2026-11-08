"""Chargement de la configuration (config.json à côté de l'appli, sinon valeurs par défaut)."""
from __future__ import annotations

import json
import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"

DEFAULTS = {
    # --- Chat Claude ---
    "anthropic_api_key": "",          # laisser vide pour utiliser la variable d'env ANTHROPIC_API_KEY
    "model": "claude-sonnet-5-5",
    "effort": "medium",               # low / medium / high : profondeur de réflexion du modèle
    # --- Interface ---
    "refresh_seconds": 1.0,
    "always_on_top": True,
    "opacity": 1.0,              # 0.5 à 1.0 (transparence de la fenêtre)
    "drives": ["C", "D"],
    "sound_on_alert": True,
    # --- Seuils d'alerte ---
    "alert_disk_free_gb": {"C": 15, "D": 20},
    "alert_ram_percent": 90,
    "alert_cpu_percent": 95,
    "alert_cpu_sustained_s": 20,
    "alert_cpu_temp_c": 90,
    "alert_gpu_temp_c": 83,
    "alert_vram_percent": 95,
    "alert_process_cpu_percent": 50,   # un processus seul au-dessus de ce % du CPU total...
    "alert_process_sustained_s": 20,   # ... pendant au moins ce nombre de secondes
    "alert_process_ram_gb": 6,
    "alert_cooldown_s": 300,           # délai mini avant de répéter la même alerte
}


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))  # copie profonde
    error = None
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
            for k, v in user.items():
                if k.startswith("_"):
                    continue  # commentaires
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
        except (OSError, json.JSONDecodeError) as e:
            error = f"config.json illisible ({e}) — valeurs par défaut utilisées."
    cfg["_error"] = error
    return cfg


def api_key(cfg: dict) -> str | None:
    """Clé API : d'abord la variable d'environnement, sinon config.json. Jamais affichée."""
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip() or str(cfg.get("anthropic_api_key") or "").strip()
    if not key or key.startswith("COLLE"):
        return None
    return key
