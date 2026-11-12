"""Welcome to the Jungle : recherche sans compte, lecture dans le navigateur.

Depuis fin 2026, la recherche de WTTJ (/fr/jobs?query=…) n'affiche plus de liste d'offres sans compte
(« matching »), son API de recherche refuse les visiteurs et robots.txt l'interdit aux robots. Le CloudFront
du site bloque aussi le Chromium headless de Playwright. En revanche :
- toutes les offres en ligne sont publiées dans le sitemap (sitemaps/job-listings.N.xml.gz, signalé par
  robots.txt) : /fr/companies/<entreprise>/jobs/<titre>_<ville>[_<code>], avec la date de mise à jour ;
- chaque page d'offre est publique et contient un JSON-LD JobPosting (titre, entreprise, lieux, contrat,
  télétravail total, candidature sur WTTJ ou non, description).
La recherche compare donc les mots-clés au titre de l'URL ; la lecture se fait dans le navigateur de la session.
"""
from __future__ import annotations

import gzip
import html
import json
import random
import re
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from playwright.sync_api import Page

from .. import ats
from ..scheduler import DATA
from ..models import JobOffer
from .base import CHALLENGE_MARKERS, REMOTE_WANTED, JobSite, SiteBlocked, location_ok

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
REMOTE_CACHE = DATA / "wttj_teletravail.json"  # URL -> télétravail total (évite de relire les pages)
STOPWORDS = {"de", "d", "du", "des", "la", "le", "les", "l", "et", "en", "a", "au", "aux", "h", "f", "x"}
CONTRACTS = {"FULL_TIME": "CDI / temps plein", "PART_TIME": "temps partiel", "CONTRACTOR": "freelance",
             "TEMPORARY": "CDD / temporaire", "INTERN": "stage", "OTHER": "autre (alternance, VIE…)"}


def tokens(text: str) -> list[str]:
    """« Ingénieur IA » -> ["ingenieur", "ia"] (minuscules, sans accents)."""
    text = unicodedata.normalize("NFKD", text.lower())
    return re.findall(r"[a-z0-9]+", "".join(c for c in text if not unicodedata.combining(c)))


def html_to_text(fragment: str) -> str:
    fragment = re.sub(r"<\s*(br|/p|/li|/h\d|/ul)\s*/?>", "\n", fragment, flags=re.I)
    fragment = re.sub(r"<\s*li[^>]*>", "- ", fragment, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", "", fragment))
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


class WelcomeToTheJungle(JobSite):
    name = "wttj"
    label = "Welcome to the Jungle"
    base_url = "https://www.welcometothejungle.com"
    login_url = "https://www.welcometothejungle.com/fr/authenticate/signin"
    description_selectors = ["[data-testid='job-section-description']", "main"]
    domains = ("welcometothejungle.com",)
    apply_button_texts = ["Postuler", "Apply"]
    # Déconnecté, « Postuler » mène à …/signup?redirect=…/apply ; la connexion passe par /authenticate/
    login_markers = ("/signup", "/signin", "/authenticate")
    AUTH_COOKIE = "wttj_api_session_key"
    easy_label = ats.WTTJ_DIRECT
    read_pause = (8, 20)  # entre deux pages d'offre : le pare-feu de WTTJ réagit aux lectures rapprochées

    SITEMAP_INDEX = "https://www.welcometothejungle.com/sitemaps/index.xml.gz"
    MAX_AGE_DAYS = 30  # offres mises à jour depuis moins de 30 jours (comme la recherche LinkedIn)
    PAGE_SIZE = 25
    REMOTE_SLUG = {"remote", "teletravail", "distance"}  # « full-remote », « 100-teletravail », « a-distance »

    _sitemap: list[tuple[str, str]] = []  # (url, lastmod), le plus récent d'abord ; partagé entre sessions du démon
    _sitemap_at: datetime | None = None

    def __init__(self, session) -> None:
        super().__init__(session)
        self._current: dict = {}  # offre lue en dernier (fetch_description)
        try:
            self._remote: dict[str, bool] = json.loads(REMOTE_CACHE.read_text(encoding="utf-8"))
        except Exception:
            self._remote = {}

    # -- HTTP ------------------------------------------------------------------
    def _get(self, url: str) -> tuple[int, bytes, str]:
        """(statut, contenu, adresse finale). 403 de CloudFront ou page de vérification -> SiteBlocked."""
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "fr-FR,fr;q=0.9"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                if r.headers.get("x-amzn-waf-action"):  # défi JavaScript du pare-feu (HTTP 202, page vide)
                    raise SiteBlocked(f"{self.name} : vérification anti-robot du pare-feu ({url}).")
                return r.status, r.read(), r.geturl()
        except urllib.error.HTTPError as e:
            body = e.read()
            if e.code in (403, 429) or any(m in body[:3000].decode("utf-8", "replace").lower() for m in CHALLENGE_MARKERS):
                raise SiteBlocked(f"{self.name} : accès bloqué (HTTP {e.code}, {url}).") from e
            return e.code, body, url

    def _load_sitemap(self) -> list[tuple[str, str]]:
        cls = type(self)
        if cls._sitemap and cls._sitemap_at and datetime.now() - cls._sitemap_at < timedelta(hours=6):
            return cls._sitemap
        _, raw, _ = self._get(self.SITEMAP_INDEX)
        parts = re.findall(r"<loc>([^<]*job-listings[^<]*)</loc>", gzip.decompress(raw).decode("utf-8"))
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self.MAX_AGE_DAYS)).isoformat()
        rows: list[tuple[str, str]] = []
        for part in parts:
            time.sleep(random.uniform(1, 3))  # requêtes espacées plutôt qu'en rafale (pare-feu de WTTJ)
            _, raw, _ = self._get(part)
            xml = gzip.decompress(raw).decode("utf-8")
            rows += [(u, m) for u, m in re.findall(r"<loc>([^<]+)</loc>\s*<lastmod>([^<]+)</lastmod>", xml)
                     if "/fr/companies/" in u and m >= cutoff]
        rows.sort(key=lambda r: r[1], reverse=True)
        cls._sitemap, cls._sitemap_at = rows, datetime.now()
        return rows

    # -- recherche ---------------------------------------------------------------
    def search_url(self, keywords: str, location: str, easy_apply: bool = False) -> str:
        return self.SITEMAP_INDEX  # pas de page de recherche : voir search()

    @staticmethod
    def _slug_parts(url: str) -> tuple[list[str], str]:
        """…/jobs/ingenieur-ia-h-f_paris-9e-arrondissement_ABC -> (["ingenieur", "ia", …], "Paris 9e Arrondissement")."""
        slug = url.split("/jobs/", 1)[1]
        title, _, rest = slug.partition("_")
        city = rest.split("_")[0].replace("-", " ").title() if rest else ""
        return tokens(title.replace("-", " ")), city

    def search(self, keywords: str, location: str = "", limit: int = 10, easy_apply: bool = False,
               page_no: int = 0) -> list[JobOffer]:
        """Offres dont le titre contient tous les mots-clés, au lieu demandé, les plus récentes d'abord.

        Lieu « Remote » : offres dont l'URL annonce le télétravail, ou déjà lues en télétravail total (JSON-LD,
        gardé dans data/wttj_teletravail.json). Les pages ne sont pas lues ici : le pare-feu de WTTJ bloque
        les lectures en rafale.
        """
        wanted = [t for t in tokens(keywords) if t not in STOPWORDS]
        remote = location.strip().lower() in REMOTE_WANTED
        start, end = page_no * self.PAGE_SIZE, (page_no + 1) * self.PAGE_SIZE
        found: list[JobOffer] = []
        for url, _ in self._load_sitemap():
            title_tokens, city = self._slug_parts(url)
            if not wanted or not set(wanted) <= set(title_tokens):
                continue
            if remote:
                if not (self._remote.get(url) or self.REMOTE_SLUG & set(title_tokens)):
                    continue
                loc = f"{city} (télétravail total)" if city else "Télétravail total"
            else:
                loc = city
                if location and not location_ok(city, location):
                    continue
            company = url.split("/companies/")[1].split("/")[0]
            found.append(JobOffer(site=self.name, url=url, title=" ".join(title_tokens).capitalize(),
                                  company=company.replace("-", " "), location=loc))
            if len(found) >= end:
                break
        return found[start:end][:limit]

    def parse_results(self, page: Page, limit: int) -> list[JobOffer]:
        return []  # pas de page de résultats (search() lit le sitemap)

    # -- lecture d'une offre -------------------------------------------------------
    def _save_remote(self) -> None:
        try:
            REMOTE_CACHE.parent.mkdir(parents=True, exist_ok=True)
            REMOTE_CACHE.write_text(json.dumps(self._remote), encoding="utf-8")
        except OSError:
            pass

    def _read(self, url: str) -> dict:
        """Lit la page de l'offre et renvoie ses informations (aussi gardées dans self._current).

        Par le navigateur de la session : le pare-feu de WTTJ soumet les clients HTTP simples à un défi
        JavaScript dès quelques pages, qu'un navigateur passe en chargeant la page comme un visiteur.
        Le Chromium headless de Playwright est, lui, refusé (403) : check_page lève alors SiteBlocked.
        """
        tab = self.session.goto(url)
        self.check_page(tab)
        return self._parse(tab, url)

    def _parse(self, tab: Page, url: str) -> dict:
        """Informations de l'offre affichée dans `tab` (JSON-LD de la page)."""
        try:  # le défi du pare-feu recharge la page avant l'affichage de l'offre
            tab.wait_for_selector("script[type='application/ld+json'], h1", state="attached", timeout=15_000)
        except Exception:
            pass
        page, final = tab.content(), tab.url
        info: dict = {"url": url, "closed": "/jobs/" not in final}
        m = re.search(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', page, re.S)
        data: dict = {}
        if m:
            try:
                data = json.loads(m.group(1))
            except ValueError:
                data = {}
        if data.get("@type") != "JobPosting":
            data = {}
        valid = data.get("validThrough") or ""
        if valid and valid < datetime.now(timezone.utc).isoformat():
            info["closed"] = True
        info["data"] = data
        info["remote"] = data.get("jobLocationType") == "TELECOMMUTE"
        info["direct"] = data.get("directApply") if isinstance(data.get("directApply"), bool) else None
        info["external"] = ""
        for tag in re.findall(r'<a\b[^>]*data-testid="job_(?:header|sticky)-button-apply"[^>]*>', page):
            href = re.search(r'href="([^"]+)"', tag)
            if href and href.group(1).startswith("http") and not self.owns(html.unescape(href.group(1))):
                info["external"] = html.unescape(href.group(1))
                break
        info["teletravail"] = next(iter(re.findall(r">(Télétravail [^<]{2,30})<", page)), "")
        info["page"] = page
        if data:
            self._remote[url] = info["remote"]
        self._current = info
        return info

    def fetch_description(self, url: str) -> str:
        info = self._read(url.split("?")[0])
        self._save_remote()
        data = info["data"]
        if not data:  # pas de JSON-LD : texte brut de la page
            body = re.sub(r"<(script|style)\b.*?</\1>", "", info["page"], flags=re.S | re.I)
            return html_to_text(body)[:15_000]
        org = data.get("hiringOrganization") or {}
        places = [p.get("address", {}).get("addressLocality", "") for p in data.get("jobLocation") or []]
        salary = (data.get("baseSalary") or {}).get("value") or {}
        lines = [
            data.get("title", ""),
            f"Entreprise : {org.get('name', '')}",
            f"Lieu : {', '.join(dict.fromkeys(p for p in places if p))}",
            f"Contrat : {CONTRACTS.get(data.get('employmentType'), data.get('employmentType', ''))}",
        ]
        if info["remote"] or info["teletravail"]:
            lines.append(f"Télétravail : {info['teletravail'] or 'total'}")
        if salary.get("minValue") or salary.get("maxValue"):
            unit = {"YEAR": "an", "MONTH": "mois", "DAY": "jour", "HOUR": "heure"}.get(salary.get("unitText"), "")
            lines.append(f"Salaire : {salary.get('minValue', '')} à {salary.get('maxValue', '')} € par {unit}")
        exp = (data.get("experienceRequirements") or {}).get("monthsOfExperience")
        if exp:
            lines.append(f"Expérience demandée : {exp} mois")
        if data.get("datePosted"):
            lines.append(f"Publiée le : {data['datePosted'][:10]}")
        lines += ["", "Description :", html_to_text(data.get("description", ""))]
        if data.get("qualifications"):
            lines += ["", "Profil recherché :", html_to_text(str(data["qualifications"]))]
        return "\n".join(lines).strip()

    def refine(self, offer: JobOffer) -> JobOffer:
        """Titre et entreprise exacts (le sitemap ne donne que l'URL), lieux de l'offre."""
        data = self._current.get("data") or {}
        if not data or self._current.get("url") != offer.url.split("?")[0]:
            return offer
        places = [p.get("address", {}).get("addressLocality", "") for p in data.get("jobLocation") or []]
        loc = ", ".join(list(dict.fromkeys(p for p in places if p))[:3]) or offer.location
        if self._current.get("remote"):
            loc = f"{loc} (télétravail total)" if loc else "Télétravail total"
        return offer.model_copy(update={
            "title": html.unescape(data.get("title") or offer.title),
            "company": html.unescape((data.get("hiringOrganization") or {}).get("name") or offer.company),
            "location": loc,
        })

    # -- candidature -------------------------------------------------------------
    def is_logged_in(self) -> bool:
        return any(c["name"] == self.AUTH_COOKIE for c in self.session.context.cookies(self.base_url))

    def start_application(self, url: str, on_loaded=None) -> str:
        """Ouvre l'offre et clique sur « Postuler ».

        Candidature sur WTTJ (directApply) : formulaire d'une page dans une fenêtre du site (…/apply), avec
        l'identité et le CV du profil WTTJ déjà renseignés -> "ok". Sinon « Postuler » ouvre le site de
        l'entreprise -> "external". Offre fermée ou sans bouton -> "not_found".
        """
        url = url.split("?")[0]

        def loaded(page: Page) -> None:
            self._parse(page, url)  # directApply, lien externe, offre fermée (has_easy_apply, is_closed…)
            self._save_remote()
            if on_loaded:
                on_loaded(page)

        status = super().start_application(url, on_loaded=loaded)
        if status == "ok":
            try:  # la fenêtre du formulaire se monte après la navigation vers …/apply
                self.session.page.wait_for_selector("[role=dialog] form, [role=dialog] input", timeout=15_000)
            except Exception:
                pass
        return status

    def has_easy_apply(self) -> bool | None:
        """Candidature sur WTTJ (directApply), après fetch_description."""
        return self._current.get("direct")

    def is_closed(self) -> bool:
        return bool(self._current.get("closed"))

    def external_apply_url(self) -> str:
        return self._current.get("external", "")
