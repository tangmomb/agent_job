"""Chargement de la configuration (.env) et du profil candidat (YAML)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")


def _path(env: str, default: str) -> Path:
    p = Path(os.getenv(env) or default)
    return p if p.is_absolute() else ROOT / p


@dataclass(frozen=True)
class Settings:
    profile_path: Path = _path("PROFILE_PATH", "config/profile.yaml")
    google_sheet_id: str = os.getenv("GOOGLE_SHEET_ID", "")
    google_worksheet: str = os.getenv("GOOGLE_WORKSHEET", "")  # vide = premier onglet
    google_credentials_file: Path = _path("GOOGLE_CREDENTIALS_FILE", "config/google_credentials.json")
    google_token_file: Path = _path("GOOGLE_TOKEN_FILE", "config/google_token.json")
    sessions_dir: Path = _path("SESSIONS_DIR", "config/sessions")
    letters_dir: Path = _path("LETTERS_DIR", "data/lettres")
    model: str = os.getenv("AGENT_MODEL", "mistral-large-4")  # évaluation des offres et lettres (lot)
    apply_model: str = os.getenv("APPLY_MODEL", "zai-glm-5-3-flash")  # remplissage des formulaires (en direct)
    # Plusieurs actions par appel (moins d'appels) ou une seule (plus prudent, défaut : le modèle est peu cher)
    # Tri des e-mails : étape cruciale (statuts du Sheet), même modèle que l'évaluation par défaut
    mail_model: str = os.getenv("MAIL_MODEL", "") or os.getenv("AGENT_MODEL", "mistral-large-4")
    apply_parallel: bool = os.getenv("APPLY_PARALLEL", "false").lower() in ("1", "true", "yes", "oui")
    headless: bool = os.getenv("HEADLESS", "true").lower() in ("1", "true", "yes", "oui")
    browser_channel: str = os.getenv("BROWSER_CHANNEL", "")  # vide = Chromium de Playwright ; "chrome" en local
    max_iterations: int = int(os.getenv("AGENT_MAX_ITERATIONS", "250"))


settings = Settings()


def load_profile() -> dict[str, Any]:
    if not settings.profile_path.exists():
        raise FileNotFoundError(
            f"Profil introuvable : {settings.profile_path}. "
            "Copiez config/profile.example.yaml en config/profile.yaml et remplissez-le."
        )
    with settings.profile_path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}
