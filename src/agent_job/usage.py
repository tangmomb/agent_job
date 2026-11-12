"""Consommation réelle de l'API du LLM (tokens) et coût estimé.

L'API renvoie à chaque appel le nombre de tokens envoyés (dont ceux servis depuis son cache,
facturés moins cher) et reçus (dont le raisonnement). Les tarifs se règlent dans .env, en $ par million de tokens.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

# Tarifs en $ par million de tokens : (entrée, entrée en cache, sortie). Modifiables dans .env.
PRICES = {
    "mistral-large-4": (1.36, 0.14, 4.18),
    "zai-glm-5-3-flash": (0.15, 0.03, 0.50),
    "deepseek-v4-1-flash-1017": (0.30, 0.007, 1.20),  # servi par Mistral
}
for _name, _env in (("mistral-large-4", "MISTRAL"), ("zai-glm-5-3-flash", "GLM"), ("deepseek-v4-1-flash-1017", "DEEPSEEK")):
    _p = PRICES[_name]
    PRICES[_name] = tuple(float(os.getenv(f"{_env}_PRICE_{k}", v)) for k, v in zip(("INPUT", "CACHED", "OUTPUT"), _p))
BATCH_DISCOUNT = float(os.getenv("MISTRAL_BATCH_DISCOUNT", "0.5"))  # l'API Batch coûte 50 % du tarif normal


def _get(obj: Any, key: str, default: Any = 0) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    value = getattr(obj, key, None)
    if value is None:
        value = (getattr(obj, "additional_properties", None) or {}).get(key, default)
    return value if value is not None else default


@dataclass
class Usage:
    calls: int = 0
    prompt: int = 0      # tokens envoyés (cache compris)
    cached: int = 0      # dont tokens servis depuis le cache du fournisseur
    completion: int = 0  # tokens reçus (raisonnement compris)
    reasoning: int = 0   # dont raisonnement
    batch: bool = False
    model: str = "mistral-large-4"

    def add(self, usage: Any) -> None:
        """Ajoute l'usage d'un appel (objet du SDK ou dictionnaire d'une réponse de lot)."""
        if usage is None:
            return
        self.calls += 1
        self.prompt += int(_get(usage, "prompt_tokens"))
        self.completion += int(_get(usage, "completion_tokens"))
        self.cached += int(_get(_get(usage, "prompt_tokens_details", None), "cached_tokens"))
        self.reasoning += int(_get(_get(usage, "completion_tokens_details", None), "reasoning_tokens"))

    def __iadd__(self, other: "Usage") -> "Usage":
        for f in ("calls", "prompt", "cached", "completion", "reasoning"):
            setattr(self, f, getattr(self, f) + getattr(other, f))
        return self

    @property
    def cost(self) -> float:
        """Coût estimé en dollars."""
        p_in, p_cached, p_out = PRICES.get(self.model, PRICES["mistral-large-4"])
        fresh = self.prompt - self.cached
        dollars = (fresh * p_in + self.cached * p_cached + self.completion * p_out) / 1_000_000
        return dollars * (BATCH_DISCOUNT if self.batch else 1.0)

    def short(self) -> str:
        if not self.calls:
            return "aucun appel"
        n = lambda v: f"{v:,}".replace(",", " ")
        return (f"{self.model} · {self.calls} appel(s), {n(self.prompt)} tokens envoyés (dont {n(self.cached)} en cache), "
                f"{n(self.completion)} reçus ≈ {self.cost:.3f} $")
