"""Session navigateur Playwright, portable entre machines.

Les connexions aux sites sont stockées en JSON (cookies + localStorage) dans
SESSIONS_DIR/<site>.json. On se connecte une fois à la main
(`agent-job login linkedin`) ; l'agent réutilise ensuite ces fichiers.
Les cookies rafraîchis par les sites sont réenregistrés à chaque fin de session.
Aucun mot de passe n'est stocké par le projet.
"""
from __future__ import annotations

import json
import random
import time
from pathlib import Path

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright

from .config import settings

# Domaines de cookies propres à chaque site (pour réenregistrer chaque session séparément)
# Cookie qui prouve la connexion : sans lui, on n'écrase pas une session enregistrée
AUTH_COOKIES = {"linkedin": "li_at", "wttj": "wttj_api_session_key", "indeed": "PPID"}

SITE_DOMAINS = {
    "linkedin": ("linkedin.com",),
    "indeed": ("indeed.com", "indeed.fr"),
    "wttj": ("welcometothejungle.com",),
}


def session_file(site: str) -> Path:
    return settings.sessions_dir / f"{site}.json"


def load_storage_state() -> dict:
    state = {"cookies": [], "origins": []}
    for site in SITE_DOMAINS:
        f = session_file(site)
        if f.exists():
            data = json.loads(f.read_text(encoding="utf-8"))
            state["cookies"] += data.get("cookies", [])
            state["origins"] += data.get("origins", [])
    return state


def save_storage_state(state: dict, only_site: str | None = None, since: float | None = None) -> None:
    """Enregistre les cookies par site.

    Sans `only_site` (fin d'une session de l'agent), on ne fait que rafraîchir les sessions
    existantes : de simples cookies de visiteur ne créent jamais de fichier de session.
    """
    settings.sessions_dir.mkdir(parents=True, exist_ok=True)
    for site, domains in SITE_DOMAINS.items():
        if only_site and site != only_site:
            continue
        if not only_site and not session_file(site).exists():
            continue
        if since and session_file(site).exists() and session_file(site).stat().st_mtime > since:
            continue  # `agent-job login` refait pendant la session : ne pas écraser la nouvelle connexion
        cookies = [c for c in state["cookies"] if any(d in c["domain"] for d in domains)]
        if not cookies:
            continue
        auth = AUTH_COOKIES.get(site)
        if auth and not any(c["name"] == auth for c in cookies):
            if session_file(site).exists() and not only_site:
                expired = session_file(site).with_suffix(".expired.json")
                session_file(site).replace(expired)  # session révoquée : on la met de côté
            continue
        origins = [o for o in state.get("origins", []) if any(d in o["origin"] for d in domains)]
        session_file(site).write_text(json.dumps({"cookies": cookies, "origins": origins}), encoding="utf-8")


class BrowserSession:
    def __init__(self, headless: bool | None = None, persist: bool = True, channel: str | None = None) -> None:
        self.headless = settings.headless if headless is None else headless
        self.persist = persist
        # "chrome" / "msedge" : navigateur installé au lieu du Chromium de Playwright (BROWSER_CHANNEL)
        self.channel = channel if channel is not None else (settings.browser_channel or None)
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._ctx: BrowserContext | None = None
        self._page: Page | None = None

    def __enter__(self) -> "BrowserSession":
        self._started = time.time()
        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.launch(headless=self.headless, channel=self.channel)
        except Exception:
            if not self.channel:
                raise
            self._browser = self._pw.chromium.launch(headless=self.headless)  # navigateur absent : Chromium
        self._ctx = self._browser.new_context(
            storage_state=load_storage_state(),
            locale="fr-FR",
            timezone_id="Europe/Paris",
            viewport={"width": 1366, "height": 900},
        )
        self._page = self._ctx.new_page()
        return self

    def __exit__(self, *exc) -> None:
        try:
            if self._ctx and self.persist:
                save_storage_state(self._ctx.storage_state(), since=self._started)
        finally:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()

    @property
    def context(self) -> BrowserContext:
        assert self._ctx is not None, "Session non démarrée"
        return self._ctx

    @property
    def page(self) -> Page:
        assert self._page is not None, "Session non démarrée"
        return self._page

    slow: float = 1.0  # multiplicateur des pauses (rythme.ralentissement du profil)

    def pause(self, low: float = 1.0, high: float = 3.0) -> None:
        """Délai aléatoire pour garder un rythme humain."""
        self.page.wait_for_timeout(random.uniform(low, high) * self.slow * 1000)

    def goto(self, url: str) -> Page:
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        except Exception as e:
            if "interrupted by another navigation" not in str(e):
                raise
            # Une page précédente (ex. vérification anti-robot) a relancé sa propre navigation : on la quitte
            # puis on réessaie une fois.
            self.page.goto("about:blank", timeout=10_000)
            self.page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        self.pause(1.5, 3.5)
        return self.page
