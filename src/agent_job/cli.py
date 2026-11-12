"""Interface en ligne de commande : `agent-job --help`."""
from __future__ import annotations

import sys

# Console Windows (cp1252) : forcer l'UTF-8 pour les accents et symboles.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

import os
import traceback
from datetime import datetime

import typer
from rich.console import Console
from rich.table import Table

from .browser import AUTH_COOKIES, BrowserSession, save_storage_state, session_file
from .config import load_profile
from .sites import SITES, get_site
from .tracker import Tracker

app = typer.Typer(help="Agent IA autonome de recherche et candidature d'emploi.", no_args_is_help=True)
console = Console()

@app.command()
def login(
    site: str = typer.Argument(..., help="linkedin, indeed ou wttj"),
    browser: str = typer.Option("chrome", help="chrome, msedge, ou chromium (celui de Playwright)"),
    normal: bool = typer.Option(False, "--normal", help="Chrome ouvert normalement, sans pilotage pendant la "
                                "connexion (sites qui refusent la connexion dans un navigateur piloté : Google, Indeed)"),
) -> None:
    """À faire sur un poste avec écran : connexion manuelle, puis enregistrement de la session en JSON."""
    if normal:
        state = _login_normal_chrome(SITES[site].login_url)
    else:
        channel = None if browser == "chromium" else browser
        with BrowserSession(headless=False, persist=False, channel=channel) as session:
            session.goto(get_site(site, session).login_url)
            input("Connectez-vous dans le navigateur, puis appuyez sur Entrée ici… ")
            state = session.context.storage_state()
    auth = AUTH_COOKIES.get(site)
    if auth and not any(c["name"] == auth for c in state["cookies"]):
        console.print(f"[red]Connexion {site} non détectée (cookie {auth} absent) : rien n'est enregistré. Réessayez.")
        raise typer.Exit(1)
    save_storage_state(state, only_site=site)
    console.print(f"[green]Session enregistrée : {session_file(site)}")


CHROME_PATHS = (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "/usr/bin/google-chrome")


def _login_normal_chrome(url: str) -> dict:
    """Ouvre Chrome comme un utilisateur le ferait (profil à part, aucun pilotage), puis relit ses cookies.

    Playwright ne s'y attache qu'après la connexion, par le port de débogage, pour copier les cookies.
    """
    import shutil
    import subprocess

    from playwright.sync_api import sync_playwright

    from .config import ROOT

    chrome = next((p for p in CHROME_PATHS if os.path.exists(p)), None) or shutil.which("chrome") \
        or shutil.which("google-chrome")
    if not chrome:
        console.print("[red]Chrome introuvable.")
        raise typer.Exit(1)
    profile = ROOT / "data" / "chrome-login"  # profil séparé (Chrome refuse le débogage sur le profil par défaut)
    port = 9333
    proc = subprocess.Popen([chrome, f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
                             "--no-first-run", "--no-default-browser-check", url])
    try:
        input("Connectez-vous dans la fenêtre Chrome, puis appuyez sur Entrée ici (sans fermer Chrome)… ")
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            state = browser.contexts[0].storage_state()
            browser.close()
        return state
    finally:
        proc.terminate()


@app.command()
def search(
    site: str = typer.Option("linkedin", help=f"Site : {', '.join(SITES)}"),
    keywords: str = typer.Option(..., "--keywords", "-k"),
    location: str = typer.Option("", "--location", "-l"),
    limit: int = 10,
    save: bool = typer.Option(False, help="Ajouter les offres au suivi Google Sheets"),
    headed: bool = typer.Option(False, help="Afficher le navigateur"),
) -> None:
    """Recherche simple sans IA (utile pour tester les sélecteurs)."""
    tracker = Tracker() if save else None
    with BrowserSession(headless=not headed) as session:
        offers = get_site(site, session).search(keywords, location, limit)
    table = Table("Titre", "Entreprise", "Lieu", "URL")
    for o in offers:
        table.add_row(o.title, o.company, o.location, o.url)
        if tracker:
            tracker.upsert(o.url, site=o.site, titre=o.title, entreprise=o.company, lieu=o.location)
    console.print(table)
    console.print(f"{len(offers)} offre(s).")


@app.command()
def run(
    headed: bool = typer.Option(False, help="Afficher le navigateur (débogage en local)"),
    max_candidatures: int = typer.Option(None, "--max-candidatures", help="Plafond ponctuel pour cette session"),
) -> None:
    """Une session : découvre de nouvelles offres, les fait évaluer par le LLM (lot), puis postule."""
    from . import scheduler as sch
    from .pipeline import run_session

    load_profile()  # erreur claire si le profil manque
    try:  # même verrou que le mode continu : une seule session à la fois, et `agent-job stop` fonctionne
        sch.acquire_lock()
    except sch.AlreadyRunning as e:
        console.print(f"[red]{e}")
        raise typer.Exit(1)
    sch.clear_stop()
    sch.write_state(pid=os.getpid(), status="session en cours", last_run=datetime.now())
    try:
        run_session(headless=False if headed else None, max_applications=max_candidatures)
    except KeyboardInterrupt:
        console.print("Interrompu (Ctrl+C).")
    finally:
        sch.clear_stop()
        sch.release_lock()
        sch.write_state(status="arrêté", stopped=datetime.now(), next_run=None)


@app.command()
def daemon(
    now: bool = typer.Option(True, help="Première session dès le lancement (si dans les heures actives)"),
) -> None:
    """Tourne en continu jusqu'à `agent-job stop` (ou Ctrl+C), avec des sessions très espacées.

    Rythme réglé dans la section `rythme` du profil (intervalle, heures et jours actifs).
    """
    from . import scheduler as sch
    from .pipeline import run_session

    try:
        sch.acquire_lock()
    except sch.AlreadyRunning as e:
        console.print(f"[red]{e}")
        raise typer.Exit(1)
    sch.clear_stop()
    r = sch.rythme(load_profile())
    console.print(
        f"Agent démarré (PID {os.getpid()}) : une session toutes les {r['intervalle_minutes'][0]:g}-"
        f"{r['intervalle_minutes'][1]:g} min"
        + (f", {r['heures_actives']} h" if r.get("heures_actives") else "")
        + (f", jours {', '.join(r['jours_actifs'])}" if r.get("jours_actifs") else "")
        + ". Arrêt : `agent-job stop` ou Ctrl+C."
    )
    sch.write_state(pid=os.getpid(), started=datetime.now(), status="démarré")
    try:
        first = True
        while not sch.stop_requested():
            current = datetime.now()
            if sch.is_active(current, r) and (now or not first):
                console.rule(f"Session du {current:%Y-%m-%d %H:%M}")
                sch.write_state(status="session en cours", last_run=current)
                try:
                    run_session()
                except Exception:
                    console.print("[red]Session en échec :")
                    console.print(traceback.format_exc())
                r = sch.rythme(load_profile())  # relit le profil : réglages modifiables sans redémarrer
                nxt = sch.next_run(datetime.now(), r)
            elif sch.is_active(current, r):
                nxt = sch.next_run(current, r)
            else:
                nxt = sch.next_window(current, r)
            first = False
            if sch.stop_requested():
                break
            console.print(f"Prochaine session : {sch.JOURS[nxt.weekday()]} {nxt:%d/%m à %H:%M}.")
            sch.write_state(status="en attente", next_run=nxt)
            if not sch.sleep_until(nxt):
                break
    except KeyboardInterrupt:
        console.print("Interrompu (Ctrl+C).")
    finally:
        sch.clear_stop()
        sch.release_lock()
        sch.write_state(status="arrêté", stopped=datetime.now(), next_run=None)
        console.print("[green]Agent arrêté.")


@app.command()
def stop() -> None:
    """Demande l'arrêt de l'agent en cours (immédiat s'il attend, sinon après la candidature en cours)."""
    from . import scheduler as sch

    pid = sch.running_pid()
    if not pid:
        console.print("Aucun agent en cours.")
        sch.clear_stop()
        return
    sch.request_stop()
    console.print(f"Arrêt demandé à l'agent (PID {pid}). Il s'arrête sous 10 s s'il attend, "
                  "sinon dès la fin de l'action en cours.")


@app.command()
def status() -> None:
    """Affiche si l'agent tourne, son état et la prochaine session prévue."""
    from . import scheduler as sch

    pid = sch.running_pid()
    st = sch.read_state()
    console.print(f"Agent : {'[green]en marche (PID ' + str(pid) + ')' if pid else '[yellow]arrêté'}")
    for key, label in [("status", "État"), ("last_run", "Dernière session"), ("next_run", "Prochaine session")]:
        if st.get(key):
            console.print(f"{label} : {st[key]}")
    t = Tracker()
    console.print(f"Candidatures envoyées aujourd'hui : {t.count_applied_today()}")


@app.command()
def tracker(status: str = typer.Option("", help="Filtrer par statut")) -> None:
    """Affiche le suivi des candidatures."""
    rows = Tracker().rows(status or None)
    table = Table("Date", "Site", "Titre", "Entreprise", "Match", "Statut")
    for r in rows:
        table.add_row(str(r["date_ajout"]), str(r["site"]), str(r["titre"]), str(r["entreprise"] or ""),
                      str(r["match"] or ""), str(r["statut"]))
    console.print(table)


@app.command(name="ats")
def ats_report(
    completer: int = typer.Option(0, help="Relever l'ATS de N offres déjà au Sheet (sans candidature simplifiée)"),
    tout: bool = typer.Option(False, "--tout", help="Relever l'ATS de toutes ces offres"),
) -> None:
    """Outils de recrutement derrière « Postuler » : combien d'offres passent par chacun."""
    import random
    from collections import Counter

    from . import ats
    from .scheduler import rythme
    from .tracker import APPLY_URL_COL, ATS_COL, CLOSED_STATUT, EASY_APPLY_COL

    t = Tracker()
    if completer or tout:
        # 1. Sans navigateur : candidatures simplifiées (déjà envoyées par l'agent, ou « oui ») -> lien = l'offre
        for x in t.rows():
            if x["site"] == "linkedin" and not x[ATS_COL] and (
                    x[EASY_APPLY_COL] == "oui" or (not x[EASY_APPLY_COL] and x["statut"] == "candidature envoyée")):
                t.upsert(x["url"], **{EASY_APPLY_COL: "oui", ATS_COL: ats.LINKEDIN_EASY, APPLY_URL_COL: x["url"]})
        # 2. Offres à ouvrir : candidature simplifiée inconnue, ou « non » sans ATS (« inconnu », relais : nouvel essai)
        # (offres jugées « faible » non ouvertes : elles ne seront pas candidatées)
        todo = [x for x in t.rows() if x["site"] == "linkedin" and x[ATS_COL] != ats.CLOSED and x["match"] != "faible" and (
            not x[EASY_APPLY_COL] or (x[EASY_APPLY_COL] == "non" and (x[ATS_COL] in ("", ats.UNKNOWN) or x[ATS_COL] in ats.RELAYS)))]
        if not tout:
            todo = todo[-completer:]  # les plus récentes (offres plus souvent encore en ligne)
        console.print(f"{len(todo)} offres à ouvrir, 10 à 30 s entre chacune, 2 à 4 min toutes les 10.")
        with BrowserSession(persist=False) as session:
            session.slow = float(rythme(load_profile())["ralentissement"])
            site = get_site("linkedin", session)
            for i, r in enumerate(todo):
                if i:
                    pause = random.uniform(120, 240) if i % 10 == 0 else random.uniform(10, 30)
                    session.page.wait_for_timeout(pause * 1000)  # entre deux offres (plus longue toutes les 10)
                try:
                    site.fetch_description(r["url"])
                except Exception as e:
                    console.print(f"[red]{r['titre']} : {type(e).__name__}: {e}")
                    break  # blocage ou session expirée : on s'arrête
                session.pause(4, 12)  # temps de lecture avant « Postuler »
                if site.is_closed():
                    fields = {ATS_COL: ats.CLOSED, APPLY_URL_COL: ""}
                else:
                    easy = site.has_easy_apply() if not r[EASY_APPLY_COL] else r[EASY_APPLY_COL] == "oui"
                    if easy:
                        fields = {EASY_APPLY_COL: "oui", ATS_COL: ats.LINKEDIN_EASY, APPLY_URL_COL: r["url"]}
                    elif easy is False:
                        link = ats.unwrap(site.external_apply_url())
                        fields = {EASY_APPLY_COL: "non", ATS_COL: ats.detect(link) if link else ats.UNKNOWN,
                                  APPLY_URL_COL: link}
                    else:
                        fields = {ATS_COL: ats.UNKNOWN}  # ni bouton simplifié ni « Postuler » reconnu
                t.upsert(r["url"], **fields)
                if fields[ATS_COL] == ats.CLOSED and r["statut"] != CLOSED_STATUT:
                    t.upsert(r["url"], statut=CLOSED_STATUT)
                    t.append_note(r["url"], "Offre fermée aux candidatures (LinkedIn).")
                console.print(f"{i + 1}/{len(todo)} {r['titre']} — {r['entreprise']} : {fields[ATS_COL]}")
    rows = [r for r in t.rows() if r[ATS_COL]]
    if not rows:
        console.print("Aucun ATS relevé pour l'instant (offres sans candidature simplifiée trouvées par les prochaines sessions).")
        return
    counts = Counter(r[ATS_COL] for r in rows)
    retained = Counter(r[ATS_COL] for r in rows if r["statut"].startswith("à postuler") or r["match"] in ("bon", "moyen"))
    table = Table("ATS", "Offres", "dont bon/moyen", "Compte candidat")
    for name, n in counts.most_common():
        table.add_row(name, str(n), str(retained[name]), "souvent" if name in ats.ACCOUNT_REQUIRED else "")
    console.print(table)
    console.print(f"{len(rows)} offres relevées.")


@app.command()
def mails(
    jours: int = typer.Option(None, help="Relire les e-mails des N derniers jours (défaut : depuis le dernier relevé)"),
    simulation: bool = typer.Option(False, "--simulation", help="Afficher ce qui serait fait, sans toucher au Sheet ni aux boîtes"),
    relire: bool = typer.Option(False, "--relire", help="Relire aussi les e-mails déjà traités (pour les ranger)"),
) -> None:
    """Relève les boîtes mail (.env : MAIL_ACCOUNTS…) : accusés de réception, refus et entretiens."""
    from .mail import accounts, check_mail
    from .pipeline import mistral_client

    if not accounts():
        console.print("Aucune boîte configurée : voir MAIL_ACCOUNTS dans .env.example et la section E-mails du README.")
        raise typer.Exit(1)
    report = check_mail(mistral_client(), Tracker(), days=jours, dry_run=simulation, reread=relire)
    console.print(f"[bold]📧 {report.short()}[/]" + (" (simulation : Sheet et boîtes inchangés)" if simulation else ""))
    console.print(f"[cyan]💶 Tri des e-mails : {report.usage.short()}")
    for line in report.unmatched:
        console.print(f"  hors Sheet : {line}")


if __name__ == "__main__":
    app()
