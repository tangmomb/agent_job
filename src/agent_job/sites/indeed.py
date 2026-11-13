from __future__ import annotations

import random
import re
from urllib.parse import quote_plus

from playwright.sync_api import Page

from .. import ats
from ..models import JobOffer
from .base import REMOTE_WANTED, JobSite, SessionExpired, text_of


class Indeed(JobSite):
    name = "indeed"
    label = "Indeed"
    base_url = "https://fr.indeed.com"
    login_url = "https://secure.indeed.com/account/login"
    description_selectors = ["#jobDescriptionText", ".jobsearch-JobComponent-description"]
    domains = ("indeed.com", "indeed.fr")
    login_markers = ("secure.indeed.com/auth", "/account/login", "bot-detection")
    apply_button_texts = ["Postuler maintenant", "Postuler"]
    external_markers = ["Postuler sur le site de l'entreprise"]
    easy_label = ats.INDEED_EASY
    # Temps de lecture d'une offre, comme LinkedIn (secondes, × ralentissement 2 = 56 à 232 s) : avec le clic sur la
    # carte et l'affichage du panneau, l'agent reste environ 1 à 4 min sur chaque offre.
    read_pause = (28, 116)

    AUTH_COOKIE = "PPID"  # présent une fois connecté (secure.indeed.com)
    PAGE_SIZE = 10
    REMOTE = REMOTE_WANTED

    def search_url(self, keywords: str, location: str, easy_apply: bool = False) -> str:
        loc = location.strip()
        loc = "Télétravail" if loc.lower() in self.REMOTE else loc  # lieu « Télétravail » d'Indeed (offres à distance)
        url = f"{self.base_url}/jobs?q={quote_plus(keywords)}&l={quote_plus(loc)}"
        url += "&fromage=14&sort=date"  # 14 derniers jours (maximum proposé par Indeed)
        return url + "&iafilter=1" if easy_apply else url

    _has_next = True  # la dernière page de résultats lue propose-t-elle une page suivante ?

    def page_url(self, url: str, page: int) -> str | None:
        # Ne demander une page suivante que si Indeed en affiche le lien : une page qui n'existe pas
        # (start= au-delà des résultats) fait partie de ce qui déclenche la vérification anti-robot.
        if page and not self._has_next:
            return None
        return url + f"&start={page * self.PAGE_SIZE}" if page else url

    def canonical(self, url: str) -> str:
        m = re.search(r"[?&](?:jk|vjk)=([0-9a-zA-Z]+)", url)
        return f"{self.base_url}/viewjob?jk={m.group(1)}" if m else url

    def is_logged_in(self) -> bool:
        return any(c["name"] == self.AUTH_COOKIE for c in self.session.context.cookies("https://secure.indeed.com"))

    def check_page(self, page: Page, require_login: bool = False) -> None:
        # Visiteur repéré comme robot : Indeed renvoie vers la page de connexion (from=bot-detection-anonymous)
        if any(m in page.url for m in self.login_markers):
            raise SessionExpired(f"{self.name} : Indeed demande une connexion pour lire les offres, "
                                 f"lancer `agent-job login {self.name}`.")
        super().check_page(page, require_login)

    def parse_results(self, page: Page, limit: int) -> list[JobOffer]:
        offers: list[JobOffer] = []
        seen: set[str] = set()
        cards = page.locator("div.job_seen_beacon")
        for i in range(cards.count()):
            card = cards.nth(i)
            link = card.locator("a[data-jk]").first
            if not link.count():
                continue
            jk = link.get_attribute("data-jk")
            if not jk or jk in seen:
                continue
            seen.add(jk)
            tags = text_of(card.locator(".jobMetaDataGroup")) + " " + card.inner_text(timeout=2_000)
            offers.append(JobOffer(
                site=self.name,
                url=f"{self.base_url}/viewjob?jk={jk}",
                title=text_of(card.locator(".jobTitle span[title], .jobTitle span")) or text_of(link),
                company=text_of(card.locator("[data-testid='company-name']")),
                location=text_of(card.locator("[data-testid='text-location']")),
                easy_apply=True if re.search(r"candidature simplifiée|easily apply", tags, re.I) else None,
            ))
            if len(offers) >= limit:
                break
        return offers

    # Ouvrir /viewjob directement déclenche la vérification anti-robot de Cloudflare : l'offre est lue
    # comme le ferait un visiteur, en cliquant sa carte dans la liste de résultats (panneau de droite).
    PANE = "#jobsearch-ViewjobPaneWrapper"
    CONTENT = "[data-testid=viewjob-main-content]"
    EASY_BTN = "[data-testid=viewjob-indeed-apply]"  # « Postuler maintenant » (smartapply.indeed.com)
    EXTERNAL_BTN = "[data-testid=viewjob-apply]"  # « Continuer pour postuler » (/applystart -> site de l'entreprise)

    def search(self, keywords: str, location: str = "", limit: int = 10, easy_apply: bool = False,
               page_no: int = 0) -> list[JobOffer]:
        if page_no == 0:
            self._has_next = True
        offers = super().search(keywords, location, limit, easy_apply, page_no)
        self._decline_cookies()
        try:
            self._has_next = bool(self.session.page.locator(
                "[data-testid=pagination-page-next], a[aria-label='Next Page'], a[aria-label='Page suivante']").count())
        except Exception:
            self._has_next = False
        return offers

    def _decline_cookies(self) -> None:
        btn = self.session.page.get_by_role("button", name="Tout refuser")
        try:
            if btn.count() and btn.first.is_visible():
                btn.first.click(timeout=3_000)
        except Exception:
            pass

    @staticmethod
    def _jk(url: str) -> str:
        m = re.search(r"[?&](?:jk|vjk)=([0-9a-zA-Z]+)", url)
        return m.group(1) if m else ""

    def fetch_description(self, url: str) -> str:
        page, jk = self.session.page, self._jk(url)
        card = page.locator(f"a[data-jk='{jk}']").first
        if not jk or not card.count() or not card.is_visible():
            # Offre absente de la liste affichée (phase de candidature, autre page) ou carte présente mais cachée
            # (impossible à faire défiler ni à cliquer) : liste centrée sur l'offre
            page = self.session.goto(f"{self.base_url}/jobs?q=&l=&vjk={jk}")
            self.check_page(page)
            self._decline_cookies()
        else:
            # Comme un visiteur : amener la carte à l'écran, la survoler un instant, puis cliquer
            card.scroll_into_view_if_needed(timeout=5_000)
            self.session.pause(0.8, 2.0)
            card.hover(timeout=5_000)
            self.session.pause(0.4, 1.2)
            card.click(timeout=5_000)
        try:
            page.wait_for_url(re.compile(rf"vjk={jk}"), timeout=10_000)
            page.locator(f"{self.PANE} {self.CONTENT}").first.wait_for(state="visible", timeout=15_000)
            self.session.pause(1.0, 2.5)
        except Exception:
            pass
        self.check_page(page)
        loc = page.locator(f"{self.PANE} {self.CONTENT}").first
        try:
            return loc.inner_text(timeout=5_000).strip() if loc.count() else ""
        except Exception:
            return ""

    def read_offer(self) -> None:
        """Lecture dans le panneau de droite de la liste, en le faisant défiler (scroll_read)."""
        box = None
        try:
            pane = self.session.page.locator(self.PANE).first
            box = pane.bounding_box() if pane.count() else None
        except Exception:
            pass
        self.scroll_read(box)

    # Titre, entreprise et lieu : ceux de la carte de résultat (complets, attribut title)

    def has_easy_apply(self) -> bool | None:
        page = self.session.page
        try:
            if page.locator(f"{self.PANE} {self.EASY_BTN}").count():
                return True
            if page.locator(f"{self.PANE} {self.EXTERNAL_BTN}").count():
                return False
        except Exception:
            pass
        return None

    CLOSED = re.compile(r"cette offre (d.emploi )?a expiré|this job has expired|n.est plus disponible", re.I)

    def is_closed(self) -> bool:
        try:
            pane = self.session.page.locator(self.PANE).first
            return bool(pane.count() and self.CLOSED.search(pane.inner_text(timeout=5_000)[:3000]))
        except Exception:
            return False

    def external_apply_url(self) -> str:
        """Adresse du site de l'entreprise derrière « Continuer pour postuler » (/applystart, redirection Indeed)."""
        page = self.session.page
        btn = page.locator(f"{self.PANE} {self.EXTERNAL_BTN}").first
        try:
            if not btn.count():
                return ""
            href = self.abs(btn.get_attribute("href", timeout=2_000) or "")
            if not href.startswith("http"):
                return ""
            if not self.owns(href):
                return href
            btn.hover(timeout=3_000)  # comme un clic sur « Continuer pour postuler » : survol, petite hésitation
            self.session.pause(1.0, 2.5)
            tab = page.context.new_page()  # suivre la redirection dans un onglet temporaire
            try:
                tab.goto(href, wait_until="load", timeout=30_000)
                url, stable = tab.url, 0
                for _ in range(8):  # relais qui redirigent en JavaScript
                    tab.wait_for_timeout(1_500)
                    stable = stable + 1 if tab.url == url else 0
                    url = tab.url
                    if stable >= 2:
                        break
                return "" if self.owns(url) or url == "about:blank" else url
            finally:
                tab.close()
        except Exception:
            return ""
