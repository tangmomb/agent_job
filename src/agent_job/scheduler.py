"""Fonctionnement en continu : verrou d'instance unique, arrêt demandé, planification espacée.

- Un seul agent à la fois (data/agent.lock) : deux instances sur le même compte LinkedIn
  doubleraient l'activité et le risque.
- `agent-job stop` dépose data/STOP : l'agent s'arrête pendant l'attente (sous 10 s) ou, en
  pleine session, dès qu'il n'est plus au milieu d'une candidature.
- Les horaires viennent de la section `rythme` du profil.
"""
from __future__ import annotations

import json
import os
import random
import time
from datetime import datetime, timedelta
from typing import Any

import psutil

from .config import ROOT

DATA = ROOT / "data"
LOCK_FILE = DATA / "agent.lock"
STOP_FILE = DATA / "STOP"
STATE_FILE = DATA / "agent_state.json"

JOURS = ["lun", "mar", "mer", "jeu", "ven", "sam", "dim"]

DEFAULT_RYTHME = {
    "intervalle_minutes": [20, 60],  # délai aléatoire entre deux sessions
    "heures_actives": None,   # ex. "9-19" ; vide = aucune restriction
    "jours_actifs": None,     # ex. [lun, mar, mer, jeu, ven] ; vide = tous les jours
    "max_offres_lues_par_session": 20,
    "parts_par_site": {"linkedin": 8, "wttj": 8, "indeed": 4},  # répartition du plafond ci-dessus entre les sites
    "pages_par_recherche": 5,  # pages de résultats parcourues par recherche, tant que la part du site (parts_par_site) n'est pas atteinte
    "ralentissement": 2.0,
    "attente_lot_minutes": None,  # attente max du lot LLM ; vide = jusqu'à la fin du lot
    "pause_entre_candidatures_minutes": [8, 12],  # délai aléatoire entre deux candidatures
}


def rythme(profile: dict) -> dict[str, Any]:
    custom = dict(profile.get("rythme") or {})
    if "intervalle_heures" in custom and "intervalle_minutes" not in custom:  # ancien réglage, en heures
        custom["intervalle_minutes"] = [h * 60 for h in custom["intervalle_heures"]]
    return DEFAULT_RYTHME | custom


# -- verrou ------------------------------------------------------------------
class AlreadyRunning(Exception):
    pass


def running_pid() -> int | None:
    """PID de l'agent en cours, s'il tourne encore (le verrou d'un processus mort est ignoré)."""
    try:
        pid = int(LOCK_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None
    return pid if psutil.pid_exists(pid) and pid != os.getpid() else None


def acquire_lock() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    pid = running_pid()
    if pid:
        raise AlreadyRunning(f"Un agent tourne déjà (PID {pid}). `agent-job stop` pour l'arrêter.")
    LOCK_FILE.write_text(str(os.getpid()))


def release_lock() -> None:
    try:
        if LOCK_FILE.read_text().strip() == str(os.getpid()):
            LOCK_FILE.unlink()
    except FileNotFoundError:
        pass


# -- arrêt -------------------------------------------------------------------
def request_stop() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    STOP_FILE.write_text(datetime.now().isoformat())


def stop_requested() -> bool:
    return STOP_FILE.exists()


def clear_stop() -> None:
    STOP_FILE.unlink(missing_ok=True)


def sleep_until(when: datetime, step: float = 10.0) -> bool:
    """Attend jusqu'à `when` par tranches courtes. Renvoie False si un arrêt a été demandé."""
    while datetime.now() < when:
        if stop_requested():
            return False
        time.sleep(min(step, max(0.0, (when - datetime.now()).total_seconds())))
    return not stop_requested()


# -- planification -----------------------------------------------------------
def _hours(r: dict) -> tuple[int, int]:
    start, end = (int(x) for x in str(r["heures_actives"]).split("-"))
    return start, end


def is_active(now: datetime, r: dict) -> bool:
    if r.get("jours_actifs") and JOURS[now.weekday()] not in r["jours_actifs"]:
        return False
    if r.get("heures_actives"):
        start, end = _hours(r)
        return start <= now.hour < end
    return True


def next_window(now: datetime, r: dict) -> datetime:
    """Début de la prochaine plage active, avec un décalage aléatoire de 0 à 45 min."""
    start, _ = _hours(r) if r.get("heures_actives") else (now.hour, 24)
    day = now.replace(hour=start, minute=0, second=0, microsecond=0)
    if day <= now:
        day += timedelta(days=1)
    while r.get("jours_actifs") and JOURS[day.weekday()] not in r["jours_actifs"]:
        day += timedelta(days=1)
    return day + timedelta(minutes=random.uniform(0, 45))


def next_run(now: datetime, r: dict) -> datetime:
    lo, hi = r["intervalle_minutes"]
    candidate = now + timedelta(minutes=random.uniform(lo, hi))
    return candidate if is_active(candidate, r) else next_window(candidate, r)


# -- état (affiché par `agent-job status`) -----------------------------------
def write_state(**fields: Any) -> None:
    state = read_state() | {k: (v.isoformat(timespec="minutes") if isinstance(v, datetime) else v)
                            for k, v in fields.items()}
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def read_state() -> dict[str, Any]:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}
