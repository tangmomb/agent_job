"""Outil de recrutement (ATS) derrière le bouton « Postuler » d'une offre hors candidature simplifiée.

Sert à mesurer quels ATS reviennent le plus souvent (colonne « ats » du Sheet, commande `agent-job ats`)
avant d'écrire un module de candidature pour l'un d'eux.
"""
from __future__ import annotations

from urllib.parse import parse_qs, unquote, urlparse

# Domaine (ou suffixe de domaine) -> nom de l'ATS
ATS_DOMAINS = {
    "myworkdayjobs.com": "workday", "myworkdaysite.com": "workday", "workday.com": "workday",
    "taleo.net": "taleo",
    "successfactors.com": "successfactors", "successfactors.eu": "successfactors", "sapsf.com": "successfactors",
    "sapsf.eu": "successfactors",
    "oraclecloud.com": "oracle", "icims.com": "icims", "csod.com": "cornerstone",
    "smartrecruiters.com": "smartrecruiters", "lever.co": "lever", "greenhouse.io": "greenhouse",
    "teamtailor.com": "teamtailor", "recruitee.com": "recruitee", "workable.com": "workable",
    "ashbyhq.com": "ashby", "jobvite.com": "jobvite", "breezy.hr": "breezy", "personio.de": "personio",
    "personio.com": "personio", "join.com": "join", "eightfold.ai": "eightfold", "softgarden.io": "softgarden",
    "welcometothejungle.com": "wttj", "jobteaser.com": "jobteaser", "hellowork.com": "hellowork",
    "indeed.com": "indeed", "indeed.fr": "indeed", "apec.fr": "apec", "francetravail.fr": "france travail",
    "digitalrecruiters.com": "digitalrecruiters", "talent-soft.com": "talentsoft", "talentsoft.com": "talentsoft",
    "cegid.com": "talentsoft", "beetween.com": "beetween", "flatchr.io": "flatchr", "jobaffinity.fr": "jobaffinity",
    "werecruit.io": "werecruit", "taleez.com": "taleez", "gestmax.fr": "gestmax", "altays-progiciels.com": "altays",
    "inrecruiting.com": "inrecruiting", "zohorecruit.com": "zoho", "zohorecruit.eu": "zoho",
    "tally.so": "formulaire (tally)", "typeform.com": "formulaire (typeform)",
    "forms.gle": "formulaire (google)", "docs.google.com": "formulaire (google)",
    "trakstar.com": "trakstar", "easyapply.jobs": "easyapply.jobs",
    "bamboohr.com": "bamboohr", "pinpointhq.com": "pinpoint", "phenompeople.com": "phenom",
}

# Demandent en général un compte candidat par entreprise (mot de passe, code par e-mail)
ACCOUNT_REQUIRED = {"workday", "taleo", "successfactors", "oracle", "icims", "cornerstone", "phenom"}

# Relais de redirection : l'adresse relevée n'est pas encore la destination (`agent-job ats --tout` les reprend)
RELAYS = {"easyapply.jobs"}

LINKEDIN_EASY = "linkedin (simplifiée)"  # candidature simplifiée : formulaire dans LinkedIn, lien = l'offre
WTTJ_DIRECT = "wttj (directe)"  # candidature sur Welcome to the Jungle (directApply), lien = l'offre
INDEED_EASY = "indeed (simplifiée)"  # Candidature simplifiée Indeed (Indeed Apply), lien = l'offre
CLOSED = "offre fermée"  # « Les candidatures ne sont pas acceptées pour le moment »
UNKNOWN = "inconnu"  # bouton présent mais adresse non relevée

# Redirections de suivi : la vraie destination est dans un paramètre de l'URL
REDIRECT_PARAMS = ("url", "u", "dest", "destination", "redirect", "target")


def unwrap(url: str) -> str:
    """linkedin.com/safety/go/?url=… et redirections de suivi -> URL de destination."""
    for _ in range(3):
        q = parse_qs(urlparse(url).query)
        inner = next((q[p][0] for p in REDIRECT_PARAMS if p in q and q[p][0].startswith("http")), None)
        if not inner:
            break
        url = unquote(inner) if "%" in inner[:12] else inner
    return url


def host_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def detect(url: str) -> str:
    """Nom de l'ATS, sinon le domaine du site (site carrière maison : careers.entreprise.com -> entreprise.com)."""
    host = host_of(unwrap(url))
    if not host:
        return ""
    for domain, name in ATS_DOMAINS.items():
        if host == domain or host.endswith("." + domain):
            return name
    parts = host.split(".")
    return ".".join(parts[-3:] if len(parts) > 2 and parts[-2] in ("co", "com") else parts[-2:])
