from __future__ import annotations

import html
import random
import re
from urllib.parse import quote_plus

from playwright.sync_api import Page

from .. import ats
from ..models import JobOffer
from .base import REMOTE_WANTED, JobSite, SessionExpired, text_of


class LinkedIn(JobSite):
    name = "linkedin"
    label = "LinkedIn"
    base_url = "https://www.linkedin.com"
    login_url = "https://www.linkedin.com/login"
    description_selectors = ["#job-details", ".jobs-description__content", ".jobs-box__html-content"]
    domains = ("linkedin.com",)
    apply_button_texts = ["Candidature simplifiée", "Easy Apply"]
    easy_label = ats.LINKEDIN_EASY
    external_markers = []  # sans bouton « Candidature simplifiée », start_application renvoie not_found
    # Temps de lecture d'une offre (secondes, × ralentissement 2 = 56 à 232 s) ; avec les 3 à 7 s qui suivent
    # le chargement de la page, l'agent reste environ 1 à 4 min sur chaque offre.
    read_pause = (28, 116)

    def read_offer(self) -> None:
        """Lecture de l'offre dans le panneau de droite de la liste (ou sur la page de l'offre), en le faisant
        défiler (scroll_read)."""
        page = self.session.page
        box = None
        try:
            pane = self._pane(page)
            box = pane.bounding_box() if pane is not None else None
            if not box:
                for sel in self.description_selectors:
                    loc = page.locator(sel).first
                    if loc.count():
                        box = loc.bounding_box()
                        break
        except Exception:
            pass
        self.scroll_read(box)

    # Connecté, liste de résultats : carte d'offre à gauche, détail de l'offre cliquée dans le panneau de droite
    CARD = "div[role=button][componentkey='job-card-component-ref-{jid}']"
    ABOUT = "[componentkey^='JobDetails_AboutTheJob_']"
    NEXT_PAGE = "button[data-testid='pagination-controls-next-button-visible']"
    LAST_PAGE = "button[data-testid='pagination-controls-next-button-hidden']"

    def _pane(self, page: Page):
        """Panneau de droite (colonne qui contient « À propos de l'offre d'emploi »), ou None hors de la liste."""
        try:
            pane = page.locator("[data-testid='lazy-column']").filter(has=page.locator(self.ABOUT)).last  # colonne intérieure, celle qui défile
            return pane if pane.count() else None
        except Exception:
            return None

    def _zone(self):
        """Zone de l'offre affichée : panneau de droite dans la liste, sinon la page entière."""
        page = self.session.page
        return self._pane(page) or page

    AUTH_COOKIE = "li_at"

    def canonical(self, url: str) -> str:
        """fr.linkedin.com/jobs/view/titre-123 -> www.linkedin.com/jobs/view/123/ (domaine de la session)."""
        m = re.search(r"/jobs/view/(?:[^/?]*-)?(\d+)", url) or re.search(r"[?&]currentJobId=(\d+)", url)
        return f"{self.base_url}/jobs/view/{m.group(1)}/" if m else url  # currentJobId : lien de liste de résultats

    def fetch_description(self, url: str) -> str:
        """Comme un visiteur : la carte de la liste affichée est amenée à l'écran, survolée puis cliquée, et l'offre
        est lue dans le panneau de droite. Offre absente de la liste (phase de candidature…) : page de l'offre."""
        page = self.session.page
        m = re.search(r"/jobs/view/(?:[^/?]*-)?(\d+)", url)
        jid = m.group(1) if m else ""
        card = page.locator(self.CARD.format(jid=jid)).first if jid and "/jobs/search" in page.url else None
        if card is None or not card.count():
            return super().fetch_description(self.canonical(url))
        try:
            card.scroll_into_view_if_needed(timeout=5_000)
            self.session.pause(0.8, 2.0)
            card.hover(timeout=5_000)
            self.session.pause(0.4, 1.2)
            card.click(timeout=5_000)
            page.locator(f"[componentkey='JobDetails_AboutTheJob_{jid}']").first.wait_for(state="visible", timeout=15_000)
            self.session.pause(1.0, 2.5)
        except Exception:
            return super().fetch_description(self.canonical(url))
        self.check_page(page)
        pane = self._pane(page)
        try:
            return pane.inner_text(timeout=5_000).strip() if pane is not None else ""
        except Exception:
            return ""

    def refine(self, offer: JobOffer) -> JobOffer:
        """Offre ouverte directement (lien manuel, sans carte de résultat) : titre et entreprise d'après le titre de
        la page, « Intitulé | Entreprise | LinkedIn »."""
        if offer.title and offer.company:
            return offer
        try:
            title = html.unescape(re.sub(r"^\(\d+\+?\)\s*", "", self.session.page.title()))  # « &amp; » dans le titre
        except Exception:
            return offer
        parts = [p.strip() for p in title.split("|") if p.strip() and p.strip() != "LinkedIn"]
        return offer.model_copy(update={"title": offer.title or (parts[0] if parts else ""),
                                        "company": offer.company or (parts[1] if len(parts) > 1 else "")})

    def start_application(self, url: str, on_loaded=None) -> str:
        return super().start_application(self.canonical(url), on_loaded)

    EASY_APPLY = re.compile(r"candidature simplifiée|easy apply", re.I)

    def has_easy_apply(self) -> bool | None:
        """Bouton « Candidature simplifiée » sur l'offre affichée (après fetch_description)."""
        # Libellé ancré au début : les cartes de la liste d'offres affichée à côté sont aussi des « boutons »
        # dont le libellé contient « Candidature simplifiée » (bouton de l'offre : « Candidature simplifiée pour ce poste »).
        page = self._zone()
        easy = re.compile(r"^\s*(candidature simplifiée|easy apply)", re.I)
        try:
            btn = page.get_by_role("button", name=easy).or_(page.get_by_role("link", name=easy))
            if btn.count():
                return True
            # Déjà postulé par LinkedIn : boutons remplacés par « Statut de la demande … Voir le CV »
            status = page.get_by_text(re.compile(r"^\s*(statut de la demande|application status)", re.I))
            resume = page.get_by_role("link", name=re.compile(r"voir le cv|view resume", re.I))
            if status.count() and resume.count():
                return True
            postuler = re.compile(r"^\s*(postuler|apply)(\s|$)", re.I)
            if page.get_by_role("button", name=postuler).or_(page.get_by_role("link", name=postuler)).count():
                return False
        except Exception:
            pass
        return None

    CLOSED = re.compile(r"candidatures ne sont pas acceptées|n.accepte plus de candidatures|no longer accepting applications", re.I)

    def is_closed(self) -> bool:
        """Mention « Les candidatures ne sont pas acceptées pour le moment » sur l'offre affichée."""
        try:
            page = self.session.page
            pane = self._pane(page)
            zone = pane or (page.locator("main").first if page.locator("main").count() else page.locator("body"))
            return bool(self.CLOSED.search(zone.inner_text(timeout=5_000)[:4000]))  # haut de l'offre
        except Exception:
            return False

    POSTULER = re.compile(r"^\s*(postuler|apply)(\s|$)", re.I)
    CONTINUE = re.compile(r"^\s*(continuer|continue)\b", re.I)

    def external_apply_url(self) -> str:
        """Lien du bouton « Postuler » de l'offre affichée (candidature sur le site de l'entreprise).

        Sans rien envoyer : on lit le lien, ou on clique et on relève l'adresse du nouvel onglet, puis on le ferme.
        Renvoie "" si l'adresse n'a pas pu être relevée.
        """
        page = self.session.page
        zone = self._zone()
        btn = zone.get_by_role("link", name=self.POSTULER).or_(zone.get_by_role("button", name=self.POSTULER)).first
        try:
            if not btn.count():
                return ""
            href = btn.get_attribute("href", timeout=2_000) or ""
            if href.startswith("http") and not ("linkedin.com" in href and "/safety/go" not in href):
                # lien direct, ou redirection /safety/go/?url=… (démêlée par ats.unwrap)
                if ats.host_of(ats.unwrap(href)) not in ats.RELAYS:
                    return href
                tab = page.context.new_page()  # relais : suivre la redirection dans un onglet temporaire
                try:
                    tab.goto(ats.unwrap(href), wait_until="load", timeout=30_000)
                except Exception:
                    pass
                return self._settled_url(tab) or href
            popup = None
            try:
                with page.context.expect_page(timeout=6_000) as new_page:
                    btn.click(timeout=5_000)
                popup = new_page.value
            except Exception:
                # Fenêtre intermédiaire (« Continuer vers le site ») avant l'ouverture du nouvel onglet
                cont = page.get_by_role("button", name=self.CONTINUE).or_(page.get_by_role("link", name=self.CONTINUE)).first
                if cont.count():
                    with page.context.expect_page(timeout=6_000) as new_page:
                        cont.click(timeout=5_000)
                    popup = new_page.value
            if popup is None:
                return ""
            try:
                # Laisser passer les redirections de suivi jusqu'au site de candidature
                popup.wait_for_url(lambda u: "linkedin.com" not in u and u != "about:blank", timeout=15_000)
                popup.wait_for_load_state("load", timeout=15_000)
            except Exception:
                pass
            return self._settled_url(popup)
        except Exception:
            return ""

    @staticmethod
    def _settled_url(tab) -> str:
        """Adresse de l'onglet une fois stable (relais qui redirigent en JavaScript après le chargement), puis fermeture."""
        try:
            url, stable = tab.url, 0
            for _ in range(10):
                tab.wait_for_timeout(1_500)
                stable = stable + 1 if tab.url == url else 0
                url = tab.url
                if stable >= 2:
                    break
            return "" if url == "about:blank" else url
        finally:
            tab.close()

    def is_logged_in(self) -> bool:
        return any(c["name"] == self.AUTH_COOKIE for c in self.session.context.cookies("https://www.linkedin.com"))

    REMOTE = REMOTE_WANTED
    # Connecté, LinkedIn redirige /jobs/search/ vers la nouvelle interface /jobs/search-results/ et ignore
    # « location » : sans geoId, il cherche autour du lieu du compte. Lieux connus -> geoId ; autres lieux -> écrits
    # dans la recherche (« … à Lyon »).
    # Télétravail : f_WT=2 sur /jobs/search/ est converti par la redirection en filtre « À distance » (vérifié le
    # 2026-11-11 : 23 offres à distance sur 25, aucune hybride ni sur site, pages suivantes comprises). search
    # contrôle que le filtre est actif ; sinon il clique « Type de lieu de travail > À distance » (l'adresse obtenue,
    # avec referralSearchId, garde le filtre pour les pages suivantes), et en dernier recours écrit
    # « … en télétravail » dans la recherche (moins sûr : LinkedIn s'en sert surtout pour classer).
    GEO_IDS = {"paris": "101240143", "île-de-france": "101240143", "ile-de-france": "101240143",
               "france": "105015875"}
    WORKPLACE_CHIP = re.compile(r"^\s*Type de lieu de travail\s*$")
    REMOTE_OPTION = re.compile(r"^\s*À distance\s*$")
    SHOW_RESULTS = re.compile(r"^\s*Afficher les résultats\s*$")

    def __init__(self, session) -> None:
        super().__init__(session)
        self._remote_urls: dict[str, str] = {}  # mots-clés -> adresse « À distance » retenue (pages suivantes)
        self._last_search: tuple[str, str, int] | None = None  # (mots-clés, lieu, page) affichée dans la liste

    def search_url(self, keywords: str, location: str, easy_apply: bool = False, remote_text: bool = False) -> str:
        loc = location.strip()
        query, geo = keywords, None
        if loc.lower() in self.REMOTE:
            # mots-clés seuls + filtre f_WT=2 ; « en télétravail » écrit seulement en dernier recours
            query, geo = (f"{keywords} en télétravail" if remote_text else keywords), self.GEO_IDS["france"]
        elif loc.lower() in self.GEO_IDS:
            geo = self.GEO_IDS[loc.lower()]
        elif loc:
            query = f"{keywords} à {loc}"
        url = f"{self.base_url}/jobs/search/?keywords={quote_plus(query)}&f_TPR=r2592000"  # 30 derniers jours
        if loc:
            url += f"&location={quote_plus('France' if loc.lower() in self.REMOTE else loc)}"  # interface visiteur
        if geo:
            url += f"&geoId={geo}"
        if loc.lower() in self.REMOTE:
            url += "&f_WT=2"  # interface visiteur
        # f_AL : Candidature simplifiée uniquement (non utilisé par la session : toutes les offres sont lues,
        # le Sheet indique si la candidature est simplifiée)
        return url + "&f_AL=true" if easy_apply else url

    PAGE_SIZE = 25

    def page_url(self, url: str, page: int) -> str | None:
        return url + f"&start={page * self.PAGE_SIZE}" if page else url

    def search(self, keywords: str, location: str = "", limit: int = 10, easy_apply: bool = False,
               page_no: int = 0) -> list[JobOffer]:
        if page_no and self._last_search == (keywords, location, page_no - 1):
            clicked = self._click_next_page()
            if clicked is not None:  # sinon (liste ou bouton introuvable) : adresse de la page, comme avant
                self._last_search = (keywords, location, page_no) if clicked else None
                return self._scroll_and_parse(self.session.page, limit) if clicked else []
        self._last_search = (keywords, location, page_no)
        if location.strip().lower() not in self.REMOTE:
            return super().search(keywords, location, limit, easy_apply, page_no)
        if page_no == 0:
            base = self.search_url(keywords, location, easy_apply)
            page = self.session.goto(base)
            self.check_page(page)
            new_ui = page.locator("div[componentkey^='job-card-component-ref-']").count() > 0
            if new_ui and page.get_by_text(self.WORKPLACE_CHIP).count():  # f_WT=2 non converti en filtre
                base = self._apply_remote_filter(page)
                if not base:  # filtre introuvable : télétravail écrit dans la recherche
                    base = self.search_url(keywords, location, easy_apply, remote_text=True)
                    page = self.session.goto(base)
                    self.check_page(page)
            self._remote_urls[keywords] = base
        else:
            base = self._remote_urls.get(keywords) or self.search_url(keywords, location, easy_apply)
            page = self.session.goto(self.page_url(base, page_no))
            self.check_page(page)
        return self._scroll_and_parse(page, limit)

    def _click_next_page(self) -> bool | None:
        """Bouton « Suivant » sous la liste, cliqué comme un visiteur. True : page suivante affichée ; False : dernière
        page (plus de résultats) ; None : liste ou bouton introuvable."""
        page = self.session.page
        if "/jobs/search" not in page.url:
            return None
        try:
            btn = page.locator(self.NEXT_PAGE).first
            if not btn.count():
                return False if page.locator(self.LAST_PAGE).count() else None
            first = page.locator("div[role=button][componentkey^='job-card-component-ref-']").first
            before = first.get_attribute("componentkey", timeout=2_000) if first.count() else None
            btn.scroll_into_view_if_needed(timeout=5_000)
            self.session.pause(0.8, 2.0)
            btn.hover(timeout=5_000)
            self.session.pause(0.4, 1.2)
            btn.click(timeout=5_000)
            page.wait_for_function(
                """before => { const c = document.querySelector("div[role=button][componentkey^='job-card-component-ref-']");
                               return c && c.getAttribute('componentkey') !== before; }""",
                arg=before, timeout=20_000)
            self.session.pause(1.5, 3.5)  # comme après un chargement de page
        except Exception:
            return None
        self.check_page(page)
        return True

    def _apply_remote_filter(self, page: Page) -> str:
        """Filtre « Type de lieu de travail > À distance » cliqué comme un visiteur ; renvoie l'adresse filtrée
        (sans currentJobId), ou "" si le filtre n'a pas pu être appliqué."""
        try:
            chip = page.get_by_text(self.WORKPLACE_CHIP).first
            if not chip.count():
                return ""
            chip.click(timeout=5_000)
            self.session.pause(0.8, 1.8)
            page.get_by_text(self.REMOTE_OPTION).first.click(timeout=5_000)
            self.session.pause(0.6, 1.4)
            page.get_by_text(self.SHOW_RESULTS).first.click(timeout=5_000)
            page.wait_for_url(lambda u: "referralSearchId=" in u, timeout=15_000)
            self.session.pause(1.5, 3.0)
            if page.get_by_text(self.WORKPLACE_CHIP).count():  # libellé inchangé : filtre non retenu
                return ""
        except Exception:
            return ""
        return re.sub(r"([?&])currentJobId=\d+&?", r"\1", page.url).rstrip("&?")

    def _scroll_and_parse(self, page: Page, limit: int) -> list[JobOffer]:
        page.mouse.move(300, 500)  # au-dessus de la liste de résultats (panneau défilant)
        for _ in range(3):  # charge les résultats affichés au défilement
            page.mouse.wheel(0, 2500)
            self.session.pause(0.6, 1.4)
        return self.parse_results(page, limit)

    def parse_results(self, page: Page, limit: int) -> list[JobOffer]:
        # Connecté : nouvelle interface /jobs/search-results/ (classes CSS illisibles, id dans « componentkey »)
        if page.locator("div[componentkey^='job-card-component-ref-']").count():
            return self._parse_logged_in(page, limit)
        if self.is_logged_in() and page.locator("div.base-card, div.base-search-card").count():
            # Interface visiteur malgré le cookie li_at : jeton révoqué par LinkedIn (le cookie garde sa date)
            raise SessionExpired(f"{self.name} : session déconnectée (interface visiteur), "
                                 f"relancer `agent-job login {self.name}`.")
        return self._parse_guest(page, limit)

    def _parse_logged_in(self, page: Page, limit: int) -> list[JobOffer]:
        cards = page.eval_on_selector_all(
            "div[role=button][componentkey^='job-card-component-ref-']",
            "els => els.map(e => ({key: e.getAttribute('componentkey'), text: e.innerText}))",
        )
        offers: list[JobOffer] = []
        seen: set[str] = set()
        for card in cards:
            jid = card["key"].rsplit("-", 1)[-1]
            if jid in seen:
                continue
            seen.add(jid)
            # Lignes : titre, entreprise, lieu, puis mentions (anciens élèves, date, Candidature simplifiée…)
            lines = [l.strip() for l in card["text"].split("\n") if l.strip() and l.strip() != "(Vérifié)"]
            # 1re ligne parfois = libellé d'accessibilité (« Sélectionné, Titre », « Titre (offre d'emploi vérifiée) »)
            # 1re ligne = libellé d'accessibilité du titre (« Sélectionné, Titre », « Titre (offre d'emploi vérifiée) »)
            # ou titre répété : on la retire. Un titre qui contient simplement le nom de l'entreprise est gardé.
            if len(lines) > 1:
                bare = re.sub(r"^Sélectionné,\s*|\s*\(offre d.emploi vérifiée\)$", "", lines[0]).strip()
                if bare == lines[1]:
                    lines = lines[1:]
            offers.append(JobOffer(
                site=self.name,
                url=f"{self.base_url}/jobs/view/{jid}/",
                title=lines[0] if lines else "",
                company=lines[1] if len(lines) > 1 else "",
                location=lines[2] if len(lines) > 2 else "",
                easy_apply=True if any(self.EASY_APPLY.search(l) for l in lines[3:]) else None,
            ))
            if len(offers) >= limit:
                break
        return offers

    def _parse_guest(self, page: Page, limit: int) -> list[JobOffer]:
        offers: list[JobOffer] = []
        cards = page.locator("div.job-card-container, li.jobs-search-results__list-item, div.base-card")
        for i in range(min(cards.count(), limit)):
            card = cards.nth(i)
            link = card.locator("a[href*='/jobs/view/']").first
            if not link.count():
                continue
            offers.append(JobOffer(
                site=self.name,
                url=self.canonical(self.abs(link.get_attribute("href")).split("?")[0]),
                title=text_of(card.locator(".job-card-list__title, .base-search-card__title, strong")) or text_of(link),
                company=text_of(card.locator(".artdeco-entity-lockup__subtitle, .base-search-card__subtitle")),
                location=text_of(card.locator(".artdeco-entity-lockup__caption, .job-search-card__location")),
            ))
        return offers
