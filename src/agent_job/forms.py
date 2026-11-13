"""Lecture et remplissage des formulaires de candidature, pilotés par l'agent.

Python lit la page avec `stable_snapshot()` (champs visibles, chacun avec une
référence `ref`, une fois la page stable) et l'envoie au LLM, qui agit dessus avec
fill / select / check / upload / click.
Les actions sont limitées aux domaines de la plateforme en cours.
"""
from __future__ import annotations

import re
import time
from datetime import datetime
from pathlib import Path

from .browser import BrowserSession
from .config import ROOT

CAPTURES_DIR = ROOT / "data" / "captures"

SNAPSHOT_JS = r"""
() => {
  const root = document.querySelector('[role=dialog][aria-modal=true], [role=dialog], .jobs-easy-apply-modal')
            || document.querySelector('main form, form') || document.body;
  const SEL = 'input, select, textarea, button, a[role=button], [role=radio], [role=checkbox], [role=combobox], [role=option]';
  // Options d'une liste ouverte, souvent affichées hors du formulaire (portail en fin de page)
  const els = [...root.querySelectorAll(SEL),
               ...[...document.querySelectorAll('[role=option]')].filter(o => !root.contains(o))];
  // Enveloppe [role=combobox] d'un champ à autocomplétion : le champ texte qu'elle contient suffit
  const comboOf = (el) => (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') && el.closest('[role=combobox]');
  const labelOf = (el) => {
    let t = el.getAttribute('aria-label') || '';
    if (!t && el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) t = l.innerText; }
    if (!t) { const by = el.getAttribute('aria-labelledby'); if (by) t = by.split(' ').map(i => (document.getElementById(i)||{}).innerText||'').join(' '); }
    if (!t) { const l = el.closest('label'); if (l) t = l.innerText; }
    if (!t) { const f = el.closest('fieldset'); const lg = f && f.querySelector('legend'); if (lg) t = lg.innerText; }
    if (!t && ['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName)) {  // libellé voisin : <label> d'un conteneur proche qui ne contient que ce champ
      let a = el.parentElement;
      for (let k = 0; k < 4 && a && !t; k++, a = a.parentElement) {
        if (a.querySelectorAll('input:not([type=hidden]), textarea, select').length > 1) break;
        const l = [...a.querySelectorAll('label, legend')].find(n => !n.contains(el) && n.innerText.trim());
        if (l) t = l.innerText;
      }
    }
    if (!t) t = el.placeholder || el.innerText || el.value || el.name || '';
    return t.replace(/\s+/g, ' ').trim().slice(0, 200);
  };
  // Message d'erreur affiché par le site sous un champ (« Ce champ est obligatoire ») : aria-describedby, sinon
  // texte court d'un conteneur proche qui n'englobe pas d'autres champs ; à défaut aria-invalid
  const shown = (e) => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const ERR = /(ce champ est (obligatoire|requis)|champ (obligatoire|requis)|this field is required|is required|veuillez (saisir|s[ée]lectionner|indiquer|renseigner|choisir|entrer)|please (enter|select|provide|choose|make)|invalide|invalid)/i;
  const errOf = (el) => {
    const by = el.getAttribute('aria-describedby');
    let msg = by ? by.split(' ').map(i => (document.getElementById(i) || {}).innerText || '').join(' ').trim() : '';
    if (!ERR.test(msg)) msg = '';
    for (let a = el.parentElement, k = 0; !msg && a && k < 3; k++, a = a.parentElement) {
      if (a.querySelectorAll('input:not([type=hidden]), textarea, select').length > 1) break;
      const n = [...a.querySelectorAll('span, p, div, small')].find(n => !n.querySelector('input, select, textarea')
                  && n.innerText && n.innerText.length < 120 && ERR.test(n.innerText) && shown(n));
      if (n) msg = n.innerText.trim();
    }
    if (!msg && el.getAttribute('aria-invalid') === 'true') msg = 'valeur refusée par le site';
    return msg.replace(/\s+/g, ' ').slice(0, 120);
  };
  // Références stables : un élément garde sa ref d'une lecture à l'autre (une liste qui s'ouvre ne décale rien)
  window.__agentRefN = window.__agentRefN || 0;
  const used = new Set();
  const fields = [];
  for (const el of els) {
    if (el.getAttribute('role') === 'combobox' && el.querySelector('input, textarea')) continue;
    const r = el.getBoundingClientRect();
    let visible = r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden';
    // Cases et radios souvent masquées (taille nulle) derrière un <label> stylé : visible si le libellé l'est
    if (!visible && (el.type === 'checkbox' || el.type === 'radio') && el.labels)
      visible = [...el.labels].some(l => { const b = l.getBoundingClientRect(); return b.width > 0 && b.height > 0; });
    if (el.type === 'hidden' || (!visible && el.type !== 'file') || el.disabled) continue;
    let ref = el.getAttribute('data-agent-ref');
    if (!ref || used.has(ref)) { ref = 'f' + (window.__agentRefN++); el.setAttribute('data-agent-ref', ref); }
    used.add(ref);
    const wrap = comboOf(el);
    const auto = wrap || ['list', 'both'].includes(el.getAttribute('aria-autocomplete'));
    const type = auto ? 'autocomplete' : (el.getAttribute('role') || el.type || el.tagName).toLowerCase();
    let label = labelOf(el);
    if (wrap && (!label || label === el.placeholder)) label = labelOf(wrap) || label;
    // Croix d'un champ à autocomplétion (« Close », « Clear ») : elle efface la valeur, elle ne ferme pas une liste
    const owner = el.tagName === 'BUTTON' && el.closest('[role=combobox]');
    const ownerInput = owner && owner.querySelector('input, textarea');
    if (ownerInput) label = 'Effacer la valeur du champ « ' + (labelOf(ownerInput) === ownerInput.placeholder
                              ? labelOf(owner) : labelOf(ownerInput)) + ' » (ne pas cliquer pour fermer une liste)';
    if (type === 'radio' || type === 'checkbox') {  // préfixer par la question du groupe
      const group = el.closest('fieldset, [role=radiogroup], [role=group]');
      const q = group && (group.querySelector('legend') || group.querySelector('[id$=title], span, label'));
      if (q && !label.startsWith(q.innerText.trim())) label = (q.innerText.trim().slice(0, 150) + ' → ' + label);
    }
    const item = { ref, kind: el.tagName.toLowerCase(), type, label,
                   required: !!(el.required || el.getAttribute('aria-required') === 'true') };
    // WTTJ : question dans une <section> à un seul champ, marquée par un texte « Requis »
    const sec = el.closest('section');
    if (!item.required && sec && sec.querySelectorAll('input:not([type=hidden]), textarea, select').length === 1
        && [...sec.querySelectorAll('p, span')].some(n => /^(requis|required|obligatoire)$/i.test(n.innerText.trim())))
      item.required = true;
    if (['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName) || el.getAttribute('role')) {
      const err = errOf(el);
      if (err) { item.error = err; if (/obligatoire|requis|required/i.test(err)) item.required = true; }
    }
    if (type === 'checkbox' || type === 'radio') item.checked = !!(el.checked || el.getAttribute('aria-checked') === 'true');
    else if (el.tagName !== 'BUTTON' && el.tagName !== 'A') item.value = (el.value || '').slice(0, 120);
    if (el.tagName === 'SELECT') item.options = [...el.options].map(o => o.text.trim()).filter(Boolean).slice(0, 40);
    fields.push(item);
  }
  const heading = root.querySelector('h1, h2, h3');
  const errors = [...new Set([...root.querySelectorAll('[role=alert], .artdeco-inline-feedback--error, [class*=error]')]
                   .map(e => e.innerText.trim()).filter(Boolean)
                   .concat(fields.filter(f => f.error).map(f => `${f.label.slice(0, 60)} : ${f.error}`)))].slice(0, 8);
  // Avancement du formulaire (« 25 % » chez LinkedIn) : barre de progression visible avec une valeur, sinon
  // premier pourcentage juste sous le titre de la fenêtre. Python s'en sert pour repérer le changement d'étape.
  const bar = [...root.querySelectorAll('[role=progressbar][aria-valuenow], progress[value]')].find(shown)
           || [...document.querySelectorAll('[role=progressbar][aria-valuenow], progress[value]')].find(shown);
  let progress = bar ? Number(bar.getAttribute('aria-valuenow') ?? bar.value) : null;
  if (progress === null || Number.isNaN(progress)) {
    const m = root.innerText.slice(0, 120).match(/\b(\d{1,3})\s?%/);
    progress = m && Number(m[1]) <= 100 ? Number(m[1]) : null;
  }
  return { url: location.href, title: document.title, heading: heading ? heading.innerText.trim() : '',
           progress, text: root.innerText.replace(/\s+/g, ' ').slice(0, 1500), errors, fields: fields.slice(0, 80) };
}
"""


class FormDriver:
    def __init__(self, session: BrowserSession, cv_path: Path | None) -> None:
        self.session = session
        self.cv_path = cv_path
        self.capture_dir: Path | None = None  # dossier de l'offre en cours (data/captures/<offre>/)

    def start_offer(self, jid: str, company: str = "", title: str = "") -> None:
        """Les captures suivantes iront dans data/captures/<entreprise>_<poste>_<id>/."""
        slug = lambda t: re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-")
        name = "_".join(p for p in (slug(company)[:30], slug(title)[:40], jid) if p)
        self.capture_dir = CAPTURES_DIR / name

    def save_offer(self, url: str, title: str = "", company: str = "") -> None:
        """Dans le dossier de l'offre : raccourci cliquable vers l'annonce et capture de la page entière."""
        if not self.capture_dir:
            return
        try:
            self.capture_dir.mkdir(parents=True, exist_ok=True)
            # Raccourci Internet Windows (double-clic = ouverture dans le navigateur)
            (self.capture_dir / "offre.url").write_text(f"[InternetShortcut]\nURL={url}\n", encoding="utf-8")
            (self.capture_dir / "offre.txt").write_text(
                f"{title}\n{company}\n{url}\nCandidature tentée le {datetime.now():%d/%m/%Y à %H:%M}\n", encoding="utf-8-sig")
            self.session.page.screenshot(path=str(self.capture_dir / "00-annonce.png"), full_page=True)
        except Exception:
            pass

    def end_offer(self) -> None:
        self.capture_dir = None

    def _el(self, ref: str):
        loc = self.session.page.locator(f"[data-agent-ref='{ref}']")
        if not loc.count():
            raise ValueError(f"Référence {ref} introuvable sur la page : voir l'état actuel du formulaire.")
        return loc.first

    def snapshot(self) -> dict:
        return self.session.page.evaluate(SNAPSHOT_JS)

    def stable_snapshot(self, timeout: float = 30.0, interval: float = 1.0) -> dict:
        """Lecture envoyée au LLM : attend deux lectures identiques de suite (la page ne bouge plus). Garde-fou :
        au bout de `timeout` secondes, la dernière lecture est renvoyée telle quelle."""
        previous, snap = None, self.snapshot()
        deadline = time.monotonic() + timeout
        while snap != previous and time.monotonic() < deadline:
            self.session.page.wait_for_timeout(interval * 1000)
            previous, snap = snap, self.snapshot()
        return snap

    def fill(self, ref: str, value: str) -> str | None:
        """Saisit la valeur. Pour une liste à autocomplétion, renvoie la valeur finalement retenue."""
        el = self._el(ref)
        el.fill(value, timeout=5_000)
        combo = el.get_attribute("role") == "combobox" or el.get_attribute("aria-autocomplete") in ("list", "both")
        if combo:  # champ à autocomplétion : cliquer la 1re suggestion (pas d'Entrée à l'aveugle : risque d'envoi)
            option = self.session.page.locator("[role=option]:visible").first
            try:
                option.wait_for(state="visible", timeout=4_000)
                option.click(timeout=3_000)
            except Exception:
                pass  # aucune suggestion : la saisie reste telle quelle
            self.session.page.keyboard.press("Escape") if self.session.page.locator("[role=option]:visible").count() else None
        self.session.pause(0.3, 0.9)
        if combo:
            try:
                return el.input_value(timeout=2_000)
            except Exception:
                return ""
        return None

    def label(self, ref: str) -> str:
        el = self._el(ref)
        try:
            return ((el.inner_text(timeout=1_000) or "") + " " + (el.get_attribute("aria-label") or "")).strip()
        except Exception:
            return el.get_attribute("aria-label") or ""

    def select(self, ref: str, option: str) -> None:
        self._el(ref).select_option(label=option, timeout=5_000)
        self.session.pause(0.3, 0.9)

    def check(self, ref: str, checked: bool = True) -> None:
        el = self._el(ref)
        if el.get_attribute("role") in ("radio", "checkbox") or el.evaluate("e => e.tagName") != "INPUT":
            # case personnalisée : un clic inverse l'état, donc seulement si aria-checked diffère (ou est inconnu)
            aria = el.get_attribute("aria-checked")
            if aria is None or (aria == "true") != checked:
                el.click(timeout=5_000)
        elif not el.is_visible():
            # case masquée : cliquer sur son libellé (déclenche les événements attendus par le site)
            if el.is_checked() != checked:
                el.evaluate("e => ((e.labels && e.labels[0]) || e).click()")
        else:
            el.set_checked(checked, timeout=5_000, force=True)
        self.session.pause(0.3, 0.9)

    def upload_cv(self, ref: str) -> None:
        """Joint le CV ; la lecture stable qui suit montre au LLM ce que le site affiche (nom du CV, erreur)."""
        if not self.cv_path or not self.cv_path.exists():
            raise FileNotFoundError(f"CV introuvable : {self.cv_path}")
        self._el(ref).set_input_files(str(self.cv_path), timeout=10_000)
        self.session.pause(1.5, 3)

    def capture(self, label: str) -> None:
        """Capture d'écran horodatée dans le dossier de l'offre (contrôle a posteriori de ce qui a été rempli)."""
        try:
            folder = self.capture_dir or CAPTURES_DIR / "divers"
            folder.mkdir(parents=True, exist_ok=True)
            name = f"{datetime.now():%Y%m%d-%H%M%S}-{re.sub(r'[^a-z0-9]+', '-', label.lower())[:40]}.png"
            self.session.page.screenshot(path=str(folder / name), full_page=False)
        except Exception:
            pass

    def click(self, ref: str) -> None:
        el = self._el(ref)
        try:
            label = el.inner_text(timeout=1_000) or ref
        except Exception:
            label = ref
        self.capture(f"avant {label}")  # état du formulaire avant chaque clic (Suivant, Envoyer…)
        el.click(timeout=8_000)
        self.session.pause(1.5, 3)
        self.capture(f"apres {label}")
