"""Interface commune aux sites d'emploi.

Les sélecteurs CSS de ces sites changent souvent : si une recherche renvoie 0
résultat, c'est en général ici (ou dans le fichier du site) qu'il faut les ajuster.
"""
from __future__ import annotations

import random
import re
from abc import ABC, abstractmethod
from urllib.parse import urljoin, urlparse

from playwright.sync_api import Page

from ..browser import BrowserSession
from ..models import JobOffer

# Recherchés dans le titre et le texte visible (pas dans le HTML, qui contient souvent « recaptcha »)
CHALLENGE_MARKERS = (
    "vérifiez que vous êtes humain", "verify you are human", "security check", "contrôle de sécurité",
    "requête bloquée", "request blocked", "vous avez été bloqué", "you have been blocked",
    "the request could not be satisfied", "access denied", "just a moment", "un instant…",
    "êtes-vous un robot", "are you a robot", "complete the captcha", "résolvez le captcha",
)

REMOTE_WANTED = {"remote", "télétravail", "teletravail", "full remote", "à distance", "a distance"}
IDF = re.compile(
    r"paris|île-de-france|ile-de-france|hauts-de-seine|seine-saint-denis|val-de-marne|yvelines|essonne|val-d.oise|"
    r"seine-et-marne|courbevoie|boulogne|levallois|neuilly|charenton|vanves|issy|nanterre|puteaux|la défense|"
    r"la defense|saint-denis|saint denis|montrouge|clichy|ivry|vincennes|rueil|suresnes|saint-cloud|saint cloud|massy|"
    r"versailles|maisons-laffitte|marne-la-vallée|cergy|évry|evry|saint-ouen|saint ouen|malakoff|gennevilliers|"
    r"colombes|bagneux|châtillon|chatillon|meudon|sèvres|sevres|montreuil|pantin|velizy|vélizy|guyancourt|saclay|"
    r"palaiseau|arcueil|gentilly|kremlin|bobigny|fontenay|nogent|rosny|noisy|créteil|creteil|clamart|antony", re.I)
REMOTE_LOC = re.compile(r"distance|remote|télétravail|teletravail", re.I)


def location_ok(found: str, wanted: str) -> bool:
    """Le lieu de l'offre correspond-il au lieu cherché ? (Paris = toute l'Île-de-France, Remote = à distance)"""
    w = wanted.strip().lower()
    if not w or not found.strip():
        return True  # lieu inconnu : on laisse le LLM juger
    if REMOTE_LOC.search(w):
        return bool(REMOTE_LOC.search(found))
    if "paris" in w or "île-de-france" in w or "ile-de-france" in w:
        return bool(IDF.search(found))
    return w in found.lower()


class SiteBlocked(Exception):
    """Le site demande une vérification anti-robot : l'agent n'essaie pas de la contourner."""


class SessionExpired(Exception):
    """La session n'est plus connectée : relancer `agent-job login <site>` et recopier le fichier."""


class JobSite(ABC):
    name: str
    label: str = ""  # nom affiché (prompt de candidature, notes)
    base_url: str
    login_url: str
    domains: tuple[str, ...] = ()
    login_markers: tuple[str, ...] = ("/login", "/signin", "authwall", "/uas/")
    description_selectors: list[str] = []
    apply_button_texts: list[str] = ["Postuler", "Apply"]
    external_markers: list[str] = []  # textes de bouton signalant une candidature hors plateforme
    easy_label: str = ""  # colonne « ats » d'une offre à candidature intégrée au site
    read_pause: tuple[float, float] = (4, 12)  # temps de lecture d'une offre (secondes)

    def __init__(self, session: BrowserSession) -> None:
        self.session = session

    @abstractmethod
    def search_url(self, keywords: str, location: str, easy_apply: bool = False) -> str: ...

    @abstractmethod
    def parse_results(self, page: Page, limit: int) -> list[JobOffer]: ...

    # -- contrôles -----------------------------------------------------------
    def check_page(self, page: Page, require_login: bool = False) -> None:
        try:
            visible = page.inner_text("body", timeout=5_000)[:3000]
        except Exception:
            visible = ""
        text = (page.title() + " " + visible).lower()
        # Les sites gardent des iframes de sécurité invisibles en permanence : seule une iframe visible compte.
        challenge = page.locator("iframe[src*='captcha'], iframe[title*='challenge'], iframe[title*='security verification']")
        visible_challenge = any(f.is_visible() for f in challenge.all())
        if any(m in text for m in CHALLENGE_MARKERS) or visible_challenge:
            blocked_url = page.url
            # Quitter la page de vérification : son script recharge la page visée plus tard (Cloudflare), ce qui
            # interromprait la navigation suivante de l'agent, sur un autre site.
            try:
                page.goto("about:blank", timeout=5_000)
            except Exception:
                pass
            raise SiteBlocked(f"{self.name} : accès bloqué ou vérification anti-robot ({blocked_url}).")
        if require_login and (any(m in page.url for m in self.login_markers) or not self.is_logged_in()):
            raise SessionExpired(f"{self.name} : session déconnectée, relancer `agent-job login {self.name}`.")

    def is_logged_in(self) -> bool:
        """Par défaut on ne sait pas vérifier : on suppose connecté (surchargé par site)."""
        return True

    def owns(self, url: str) -> bool:
        host = urlparse(url).hostname or ""
        return any(host == d or host.endswith("." + d) for d in self.domains)

    # -- actions -------------------------------------------------------------
    def page_url(self, url: str, page: int) -> str | None:
        """Adresse de la page `page` (0 = première) des résultats ; None : pas de pagination connue."""
        return url if page == 0 else None

    def search(self, keywords: str, location: str = "", limit: int = 10, easy_apply: bool = False,
               page_no: int = 0) -> list[JobOffer]:
        url = self.page_url(self.search_url(keywords, location, easy_apply), page_no)
        if url is None:
            return []
        page = self.session.goto(url)
        self.check_page(page)
        page.mouse.move(300, 500)  # au-dessus de la liste de résultats (souvent un panneau défilant)
        for _ in range(3):  # charge les résultats affichés au défilement
            page.mouse.wheel(0, 2500)
            self.session.pause(0.6, 1.4)
        return self.parse_results(page, limit)

    def fetch_description(self, url: str) -> str:
        page = self.session.goto(url)
        self.check_page(page)
        for sel in self.description_selectors + ["main", "body"]:
            loc = page.locator(sel).first
            if loc.count():
                try:
                    return loc.inner_text(timeout=5_000).strip()
                except Exception:
                    continue
        return ""

    def refine(self, offer: JobOffer) -> JobOffer:
        """Titre, entreprise et lieu précisés d'après l'offre lue (après fetch_description)."""
        return offer

    def read_offer(self) -> None:
        """Temps de lecture de l'offre affichée (read_pause) ; surchargé par site pour faire défiler la page."""
        self.session.pause(*self.read_pause)

    def scroll_read(self, box: dict | None) -> None:
        """Lecture comme un visiteur pendant read_pause : la souris se place sur la description (`box`, zone à
        l'écran ; None : milieu de la page), la molette la fait défiler par petits pas irréguliers répartis sur tout
        le temps de lecture, puis elle remonte en haut."""
        page = self.session.page
        total = random.uniform(*self.read_pause) * self.session.slow
        steps = random.randint(4, 9)
        weights = [random.uniform(0.5, 1.5) for _ in range(steps + 1)]
        waits = [total * w / sum(weights) for w in weights]
        try:
            vw = (page.viewport_size or {}).get("width", 1280)
            x = box["x"] + box["width"] * random.uniform(0.3, 0.7) if box else vw * random.uniform(0.4, 0.6)
            y = box["y"] + min(box["height"], 600) * random.uniform(0.4, 0.8) if box else random.uniform(300, 500)
            page.mouse.move(x, y, steps=random.randint(8, 20))
        except Exception:
            pass
        for w in waits[:-1]:
            page.wait_for_timeout(w * 1000)
            try:
                page.mouse.wheel(0, random.randint(200, 550))
            except Exception:
                pass
        page.wait_for_timeout(waits[-1] * 1000)
        try:
            page.mouse.wheel(0, -8000)
            page.wait_for_timeout(random.uniform(0.6, 1.4) * 1000)
        except Exception:
            pass

    def has_easy_apply(self) -> bool | None:
        """L'offre affichée propose-t-elle la candidature intégrée ? None : le site ne sait pas le dire."""
        return None

    def is_closed(self) -> bool:
        """L'offre affichée n'accepte plus de candidatures."""
        return False

    def external_apply_url(self) -> str:
        """Adresse de la candidature hors plateforme pour l'offre affichée ("" : inconnue)."""
        return ""

    def start_application(self, url: str, on_loaded=None) -> str:
        """Ouvre l'offre et lance la candidature intégrée à la plateforme.

        `on_loaded(page)` est appelé une fois l'annonce affichée, avant le clic sur « Postuler ».
        Renvoie "ok", "external" (candidature sur le site de l'entreprise) ou "not_found".
        """
        page = self.session.goto(url)
        self.check_page(page, require_login=True)
        if on_loaded:
            on_loaded(page)
        for text in self.external_markers:
            if page.get_by_role("button", name=text).or_(page.get_by_role("link", name=text)).count():
                return "external"
        for text in self.apply_button_texts:
            btn = page.get_by_role("button", name=text).or_(page.get_by_role("link", name=text)).first
            if btn.count():
                try:
                    with page.context.expect_page(timeout=4_000) as new_page:
                        btn.click(timeout=5_000)
                    popup = new_page.value  # la candidature s'ouvre dans un nouvel onglet
                    popup.wait_for_load_state("domcontentloaded")
                    self.session._page = popup
                except Exception:
                    pass  # pas de nouvel onglet : formulaire dans la page (modale)
                self.session.pause(1.5, 3)
                current = self.session.page
                self.check_page(current, require_login=True)
                return "ok" if self.owns(current.url) else "external"
        return "not_found"

    def abs(self, href: str | None) -> str:
        return urljoin(self.base_url, href or "")


def text_of(loc) -> str:
    try:
        return loc.first.inner_text(timeout=1_000).strip() if loc.count() else ""
    except Exception:
        return ""
