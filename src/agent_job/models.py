from __future__ import annotations

from pydantic import BaseModel


class JobOffer(BaseModel):
    site: str
    title: str
    company: str = ""
    location: str = ""
    url: str
    description: str = ""
    easy_apply: bool | None = None  # candidature simplifiée (None : inconnu)
