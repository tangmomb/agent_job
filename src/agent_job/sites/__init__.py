from __future__ import annotations

from ..browser import BrowserSession
from .base import JobSite
from .indeed import Indeed
from .linkedin import LinkedIn
from .wttj import WelcomeToTheJungle

SITES: dict[str, type[JobSite]] = {"linkedin": LinkedIn, "indeed": Indeed, "wttj": WelcomeToTheJungle}


def get_site(name: str, session: BrowserSession) -> JobSite:
    key = name.lower().replace(" ", "")
    if key not in SITES:
        raise ValueError(f"Site inconnu : {name}. Choix : {', '.join(SITES)}")
    return SITES[key](session)


def site_for_url(url: str, session: BrowserSession) -> JobSite:
    if "linkedin." in url:
        return LinkedIn(session)
    if "indeed." in url:
        return Indeed(session)
    if "welcometothejungle." in url:
        return WelcomeToTheJungle(session)
    raise ValueError(f"URL non prise en charge : {url}")
