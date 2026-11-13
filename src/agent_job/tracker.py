"""Suivi des offres et candidatures dans un Google Sheet.

Authentification (GOOGLE_CREDENTIALS_FILE) :
- compte de service : fichier JSON du compte de service, puis partager le Sheet
  avec son adresse (xxx@xxx.iam.gserviceaccount.com) en tant qu'éditeur ;
- ou OAuth : fichier client OAuth « application de bureau » ; une fenêtre de
  connexion Google s'ouvre au premier lancement, le jeton est gardé dans GOOGLE_TOKEN_FILE.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from typing import Any

import gspread
from gspread.utils import rowcol_to_a1

from .config import settings

COLUMNS = [
    "id", "date_ajout", "site", "titre", "entreprise", "lieu", "candidature_simplifiee", "url",
    "match", "statut", "date_candidature", "lettre", "notes", "texte_lettre", "ats", "lien_candidature",
    "echanges_mail",
]
STATUTS = ["à étudier", "à postuler", "à postuler (manuel)", "candidature envoyée",
           "relance", "action requise", "entretien", "refus", "écartée", "offre fermée"]
CLOSED_STATUT = "offre fermée"  # « Les candidatures ne sont pas acceptées pour le moment » (comme la colonne ats)
NIVEAUX = ["bon", "moyen", "faible"]  # adéquation CV / offre
EASY_APPLY_COL = "candidature_simplifiee"  # oui / non (vide : inconnu)
LETTER_TEXT_COL = "texte_lettre"  # texte complet de la lettre (le fichier reste dans data/lettres/)
ATS_COL = "ats"  # outil de recrutement derrière « Postuler » (offres sans candidature simplifiée)
MAIL_LINKS_COL = "echanges_mail"  # liens des e-mails de l'offre (accusé, action requise, refus, entretien), un par ligne
APPLY_URL_COL = "lien_candidature"  # adresse de ce formulaire
MANUAL_SHEET = "ajout_lien_manuel"  # liens d'offres collés à la main, lus avant les recherches
MANUAL_HEADER = "lien"
JOURNAL_SHEET = "journal"
JOURNAL_COLUMNS = ["début", "fin", "candidatures envoyées", "abandons", "incidents", "récapitulatif",
                   "appels évaluation", "tokens évaluation", "appels candidatures", "tokens candidatures",
                   "dont en cache", "coût estimé ($)"]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def job_id(url: str) -> str:
    """Identifiant stable et lisible d'une offre : site + identifiant donné par le site.

    Sert de clé dans le Sheet (colonne id) et de nom de fichier (data/offres, data/lettres, data/captures).
    - LinkedIn : linkedin-4488320570 (numéro de l'offre, que le lien soit fr./www., avec ou sans titre)
    - Indeed : indeed-<jk> (paramètre jk de /viewjob?jk=...)
    - WTTJ : wttj-<entreprise>-<offre> (/companies/<entreprise>/jobs/<offre>)
    - autre : empreinte SHA-1 de l'URL sans paramètres (10 caractères)
    """
    m = re.search(r"linkedin\.com/jobs/view/(?:[^/?]*-)?(\d+)", url)
    if m:
        return f"linkedin-{m.group(1)}"
    m = re.search(r"indeed\.[a-z.]+/.*[?&](?:jk|vjk)=([0-9a-zA-Z]+)", url)
    if m:
        return f"indeed-{m.group(1).lower()}"
    m = re.search(r"welcometothejungle\.com/(?:[a-z]{2}/)?companies/([^/?#]+)/jobs/([^/?#]+)", url)
    if m:
        return f"wttj-{_slug(m.group(1))}-{_slug(m.group(2))}"
    return hashlib.sha1(url.split("?")[0].encode()).hexdigest()[:10]


def google_client() -> gspread.Client:
    path = settings.google_credentials_file
    if not path.exists():
        raise FileNotFoundError(
            f"Identifiants Google introuvables : {path}. Voir la section Google Sheets du README."
        )
    if json.loads(path.read_text(encoding="utf-8")).get("type") == "service_account":
        return gspread.service_account(filename=str(path))
    return gspread.oauth(credentials_filename=str(path), authorized_user_filename=str(settings.google_token_file))


class Tracker:
    def __init__(self, sheet_id: str | None = None, worksheet: str | None = None) -> None:
        sheet_id = sheet_id or settings.google_sheet_id
        if not sheet_id:
            raise ValueError("GOOGLE_SHEET_ID manquant dans .env")
        self.spreadsheet = google_client().open_by_key(sheet_id)
        name = worksheet or settings.google_worksheet
        self.ws = self.spreadsheet.worksheet(name) if name else self.spreadsheet.sheet1
        self._load()
        self._migrate_score_column()
        self._migrate_easy_apply_column()
        self._migrate_letter_text_column()
        self._migrate_trailing_columns()
        if not self._header_ok():
            self._init_header()
        self._ensure_match_colors()
        self._ensure_row_colors()
        self._ensure_statut_values()
        self._single_line_rows()
        self._extend_filter()

    # -- mise en forme ---------------------------------------------------------
    MATCH_COLORS = {  # niveau : (fond, texte)
        "bon": ({"red": 0.85, "green": 0.94, "blue": 0.83}, {"red": 0.10, "green": 0.45, "blue": 0.15}),
        "moyen": ({"red": 0.99, "green": 0.91, "blue": 0.78}, {"red": 0.70, "green": 0.40, "blue": 0.05}),
        "faible": ({"red": 0.98, "green": 0.85, "blue": 0.85}, {"red": 0.70, "green": 0.15, "blue": 0.18}),
    }

    def _ensure_match_colors(self) -> None:
        """Colonne « match » colorée : bon en vert, moyen en orange, faible en rouge (règles ajoutées une fois)."""
        meta = self.spreadsheet.fetch_sheet_metadata(
            {"fields": "sheets(properties(sheetId),conditionalFormats(ranges))"})
        col = COLUMNS.index("match")
        for sheet in meta.get("sheets", []):
            if sheet["properties"]["sheetId"] != self.ws.id:
                continue
            for rule in sheet.get("conditionalFormats", []):
                if any(r.get("startColumnIndex") == col for r in rule.get("ranges", [])):
                    return  # déjà en place
        requests = []
        for i, (level, (bg, fg)) in enumerate(self.MATCH_COLORS.items()):
            requests.append({"addConditionalFormatRule": {"index": i, "rule": {
                "ranges": [{"sheetId": self.ws.id, "startRowIndex": 1,
                            "startColumnIndex": col, "endColumnIndex": col + 1}],
                "booleanRule": {
                    "condition": {"type": "TEXT_EQ", "values": [{"userEnteredValue": level}]},
                    "format": {"backgroundColor": bg, "textFormat": {"foregroundColor": fg, "bold": True}},
                },
            }}})
        self.spreadsheet.batch_update({"requests": requests})

    def _ensure_statut_values(self) -> None:
        """Liste déroulante du statut à jour (statuts ajoutés par une version plus récente, ex. « offre fermée »)."""
        meta = self.spreadsheet.fetch_sheet_metadata(
            {"ranges": [f"'{self.ws.title}'!{rowcol_to_a1(2, COLUMNS.index('statut') + 1)}"],
             "fields": "sheets(data(rowData(values(dataValidation))))"})
        try:
            rule = meta["sheets"][0]["data"][0]["rowData"][0]["values"][0]["dataValidation"]
            values = [v.get("userEnteredValue") for v in rule["condition"]["values"]]
        except (KeyError, IndexError):
            values = []
        if values != STATUTS:
            self._set_dropdown(COLUMNS.index("statut"), STATUTS)

    # Ligne entière colorée selon le statut (mise en forme conditionnelle : la couleur suit le statut)
    ROW_COLORS = {
        "candidature envoyée": {"red": 0.89, "green": 0.96, "blue": 0.89},  # vert léger
        "action requise": {"red": 1.0, "green": 0.93, "blue": 0.80},       # orange léger : à toi de répondre
        "entretien": {"red": 0.86, "green": 0.92, "blue": 0.99},           # bleu léger
        "refus": {"red": 0.99, "green": 0.89, "blue": 0.89},                # rouge léger
        "offre fermée": {"red": 0.99, "green": 0.89, "blue": 0.89},         # rouge léger, comme un refus
    }

    def _ensure_row_colors(self) -> None:
        """Une règle par statut de ROW_COLORS, ajoutée si elle manque.

        Que l'agent ou vous changiez le statut, la couleur suit. Placées après les règles de la colonne match,
        qui gardent leurs couleurs.
        """
        col = rowcol_to_a1(1, COLUMNS.index("statut") + 1).rstrip("1")
        meta = self.spreadsheet.fetch_sheet_metadata(
            {"fields": "sheets(properties(sheetId),conditionalFormats(booleanRule(condition)))"})
        rules = next((x.get("conditionalFormats", []) for x in meta.get("sheets", [])
                      if x["properties"]["sheetId"] == self.ws.id), [])
        existing = {v.get("userEnteredValue", "") for r in rules
                    for v in r.get("booleanRule", {}).get("condition", {}).get("values", [])}
        requests = []
        for statut, color in self.ROW_COLORS.items():
            formula = f'=${col}2="{statut}"'
            if formula in existing:
                continue
            requests.append({"addConditionalFormatRule": {"index": len(rules) + len(requests), "rule": {
                "ranges": [{"sheetId": self.ws.id, "startRowIndex": 1, "startColumnIndex": 0}],
                "booleanRule": {
                    "condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": formula}]},
                    "format": {"backgroundColor": color},
                },
            }}})
        if requests:
            self.spreadsheet.batch_update({"requests": requests})

    # -- lecture -------------------------------------------------------------
    def _load(self) -> None:
        """Copie locale du tableau pour limiter les appels à l'API (quota)."""
        self._values: list[list[str]] = self.ws.get_all_values()

    def _migrate_score_column(self) -> None:
        """Ancien format : colonne « score » (0-100) -> « match » (bon / moyen / faible)."""
        if not self._values or "score" not in self._values[0]:
            return
        col = self._values[0].index("score")

        def level(v: str) -> str:
            try:
                n = int(float(v))
            except ValueError:
                return v
            return "bon" if n >= 80 else "moyen" if n >= 60 else "faible"

        letter = rowcol_to_a1(1, col + 1).rstrip("1")
        column = [["match"]] + [[level(r[col]) if col < len(r) else ""] for r in self._values[1:]]
        self.ws.update(column, f"{letter}1:{letter}{len(column)}", raw=True)
        self._set_dropdown(col, NIVEAUX)
        self._load()

    def _migrate_easy_apply_column(self) -> None:
        """Ancien format sans colonne « candidature_simplifiee » : on l'insère à sa place (données décalées par Sheets)."""
        if not self._values or EASY_APPLY_COL in self._values[0]:
            return
        old = [c for c in COLUMNS if c not in (EASY_APPLY_COL, LETTER_TEXT_COL)]
        if self._values[0][: len(old)] != old:
            return
        col = COLUMNS.index(EASY_APPLY_COL)
        self.ws.insert_cols([[EASY_APPLY_COL]], col + 1)
        self._set_dropdown(col, ["oui", "non"])
        self._load()

    def _migrate_letter_text_column(self) -> None:
        """Ancien format sans colonne « texte_lettre » (la dernière) : on ajoute son en-tête."""
        if not self._values or LETTER_TEXT_COL in self._values[0]:
            return
        col = COLUMNS.index(LETTER_TEXT_COL)
        if self._values[0][:col] != COLUMNS[:col]:
            return
        if self.ws.col_count < col + 1:
            self.ws.add_cols(col + 1 - self.ws.col_count)
        self.ws.update([[LETTER_TEXT_COL]], rowcol_to_a1(1, col + 1))
        self._clip_column(col)
        self._load()

    def _migrate_trailing_columns(self) -> None:
        """Colonnes ajoutées en fin de tableau par une version plus récente (ats, lien_candidature…) : en-têtes ajoutés."""
        if not self._values:
            return
        header = [h for h in self._values[0]]
        while header and not header[-1]:
            header.pop()
        n = len(header)
        if n >= len(COLUMNS) or header != COLUMNS[:n] or LETTER_TEXT_COL not in header:
            return
        if self.ws.col_count < len(COLUMNS):
            self.ws.add_cols(len(COLUMNS) - self.ws.col_count)
        self.ws.update([COLUMNS[n:]], f"{rowcol_to_a1(1, n + 1)}:{rowcol_to_a1(1, len(COLUMNS))}")
        self.ws.format(f"{rowcol_to_a1(1, n + 1)}:{rowcol_to_a1(1, len(COLUMNS))}", {
            "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
            "backgroundColor": {"red": 0.19, "green": 0.33, "blue": 0.59},
        })
        self._load()

    ROW_HEIGHT = 21  # hauteur par défaut d'une ligne Google Sheets (pixels)

    def _single_line_rows(self) -> None:
        """Une offre = une ligne : texte coupé à l'affichage dans toutes les cellules et hauteur fixe.

        Refait à chaque ouverture (un seul appel API) : les lignes ajoutées par l'agent et les lettres
        sur plusieurs lignes ne doivent pas agrandir le tableau. Le texte entier reste dans la cellule.
        """
        self.spreadsheet.batch_update({"requests": [
            {"repeatCell": {
                "range": {"sheetId": self.ws.id, "startRowIndex": 1},
                "cell": {"userEnteredFormat": {"wrapStrategy": "CLIP", "verticalAlignment": "TOP"}},
                "fields": "userEnteredFormat.wrapStrategy,userEnteredFormat.verticalAlignment",
            }},
            {"updateDimensionProperties": {
                "range": {"sheetId": self.ws.id, "dimension": "ROWS", "startIndex": 1,
                          "endIndex": max(self.ws.row_count, len(self._values) + 1)},
                "properties": {"pixelSize": self.ROW_HEIGHT},
                "fields": "pixelSize",
            }},
        ]})

    def _extend_filter(self) -> None:
        """Filtre du tableau (s'il y en a un) étendu à toutes les colonnes et sans limite de lignes.

        Sheets fige la plage du filtre à sa création : les colonnes ajoutées ensuite (ats, lien_candidature…)
        et les nouvelles lignes en sortaient. Critères et tri sont conservés.
        """
        meta = self.spreadsheet.fetch_sheet_metadata({"fields": "sheets(properties(sheetId),basicFilter)"})
        sheet = next((x for x in meta.get("sheets", []) if x["properties"]["sheetId"] == self.ws.id), {})
        flt = sheet.get("basicFilter")
        if not flt:
            return
        rng = flt.get("range", {})
        if rng.get("endColumnIndex", 0) >= len(COLUMNS) and rng.get("endRowIndex", self.ws.row_count) >= self.ws.row_count:
            return  # déjà bon (Sheets renvoie la dernière ligne de la grille quand la plage est ouverte)
        flt["range"] = {"sheetId": self.ws.id, "startRowIndex": rng.get("startRowIndex", 0),
                        "startColumnIndex": 0, "endColumnIndex": max(len(COLUMNS), rng.get("endColumnIndex", 0))}
        flt.pop("criteria", None)  # ancien format (doublon de filterSpecs, refusé s'il est envoyé avec)
        self.spreadsheet.batch_update({"requests": [{"setBasicFilter": {"filter": flt}}]})

    def _clip_column(self, col: int) -> None:
        """Texte long coupé à l'affichage (la ligne garde sa hauteur ; le texte entier reste dans la cellule)."""
        self.ws.spreadsheet.batch_update({"requests": [{"repeatCell": {
            "range": {"sheetId": self.ws.id, "startRowIndex": 1, "startColumnIndex": col, "endColumnIndex": col + 1},
            "cell": {"userEnteredFormat": {"wrapStrategy": "CLIP"}},
            "fields": "userEnteredFormat.wrapStrategy",
        }}]})

    def _set_dropdown(self, col: int, values: list[str]) -> None:
        self.ws.spreadsheet.batch_update({"requests": [{
            "setDataValidation": {
                "range": {"sheetId": self.ws.id, "startRowIndex": 1,
                          "startColumnIndex": col, "endColumnIndex": col + 1},
                "rule": {"condition": {"type": "ONE_OF_LIST",
                                       "values": [{"userEnteredValue": v} for v in values]},
                         "showCustomUi": True, "strict": False},
            }
        }]})

    def _header_ok(self) -> bool:
        return bool(self._values) and self._values[0][: len(COLUMNS)] == COLUMNS

    def _find_row(self, jid: str) -> int | None:
        for i, row in enumerate(self._values[1:], start=2):
            if row and row[0] == jid:
                return i
        return None

    def get(self, url: str) -> dict[str, Any]:
        row = self._find_row(job_id(url))
        if row is None:
            return {}
        current = self._values[row - 1]
        return dict(zip(COLUMNS, current + [""] * (len(COLUMNS) - len(current))))

    def exists(self, url: str) -> bool:
        return self._find_row(job_id(url)) is not None

    def rows(self, statut: str | None = None) -> list[dict[str, Any]]:
        out = []
        for row in self._values[1:]:
            rec = dict(zip(COLUMNS, row + [""] * (len(COLUMNS) - len(row))))
            if rec["id"] and (statut is None or rec["statut"] == statut):
                out.append(rec)
        return out

    def count_applied_today(self) -> int:
        today = date.today().isoformat()
        return sum(1 for r in self.rows("candidature envoyée") if r["date_candidature"] == today)

    # -- écriture ------------------------------------------------------------
    def _init_header(self) -> None:
        if self._values and any(self._values[0]):
            raise RuntimeError(
                "La première ligne du Sheet ne correspond pas aux colonnes attendues : "
                f"{', '.join(COLUMNS)}. Videz-la ou utilisez un autre onglet (GOOGLE_WORKSHEET)."
            )
        end = rowcol_to_a1(1, len(COLUMNS))
        self.ws.update([COLUMNS], f"A1:{end}")
        self.ws.format(f"A1:{end}", {
            "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
            "backgroundColor": {"red": 0.19, "green": 0.33, "blue": 0.59},
        })
        self.ws.freeze(rows=1)
        # Listes déroulantes sur les colonnes statut et match
        self._set_dropdown(COLUMNS.index("statut"), STATUTS)
        self._set_dropdown(COLUMNS.index("match"), NIVEAUX)
        self._set_dropdown(COLUMNS.index(EASY_APPLY_COL), ["oui", "non"])
        self._clip_column(COLUMNS.index(LETTER_TEXT_COL))
        self._values = [COLUMNS[:]]

    def log_run(self, started, ended, applied: int, abandoned: int, incidents: str, summary: str,
                usage_eval=None, usage_apply=None) -> None:
        """Ajoute une ligne dans l'onglet « journal » (créé au besoin) : suivi des sessions à distance."""
        try:
            ws = self.spreadsheet.worksheet(JOURNAL_SHEET)
        except gspread.WorksheetNotFound:
            ws = self.spreadsheet.add_worksheet(JOURNAL_SHEET, rows=1000, cols=len(JOURNAL_COLUMNS))
            ws.freeze(rows=1)
        if ws.row_values(1) != JOURNAL_COLUMNS:  # en-têtes (complétés si l'onglet date d'une version précédente)
            if ws.col_count < len(JOURNAL_COLUMNS):
                ws.add_cols(len(JOURNAL_COLUMNS) - ws.col_count)
            ws.update([JOURNAL_COLUMNS], f"A1:{rowcol_to_a1(1, len(JOURNAL_COLUMNS))}")
            ws.format(f"A1:{rowcol_to_a1(1, len(JOURNAL_COLUMNS))}", {"textFormat": {"bold": True}})
        fmt = "%Y-%m-%d %H:%M"
        ue, ua = usage_eval, usage_apply
        tokens = lambda u: (u.prompt + u.completion) if u else 0
        ws.append_row([started.strftime(fmt), ended.strftime(fmt), applied, abandoned,
                       incidents[:2000], summary[:5000],
                       ue.calls if ue else 0, tokens(ue), ua.calls if ua else 0, tokens(ua),
                       (ue.cached if ue else 0) + (ua.cached if ua else 0),
                       round((ue.cost if ue else 0) + (ua.cost if ua else 0), 4)],
                      value_input_option="RAW")

    # -- onglet ajout_lien_manuel ------------------------------------------------
    def _manual_ws(self) -> gspread.Worksheet:
        """Onglet « ajout_lien_manuel » (créé au besoin), une seule colonne : lien."""
        try:
            ws = self.spreadsheet.worksheet(MANUAL_SHEET)
        except gspread.WorksheetNotFound:
            ws = self.spreadsheet.add_worksheet(MANUAL_SHEET, rows=200, cols=1)
            ws.freeze(rows=1)
        if ws.acell("A1").value != MANUAL_HEADER:
            ws.update([[MANUAL_HEADER]], "A1")
            ws.format("A1", {"textFormat": {"bold": True}})
        self._manual = ws
        return ws

    def manual_links(self) -> list[tuple[int, str]]:
        """Liens d'offre collés à la main dans l'onglet : (n° de ligne, lien)."""
        rows = self._manual_ws().col_values(1)[1:]
        return [(i, v.strip()) for i, v in enumerate(rows, start=2) if v.strip().startswith("http")]

    def remove_manual(self, rows: list[int]) -> None:
        """Retire de l'onglet les liens traités (l'offre est passée dans le suivi)."""
        ws = getattr(self, "_manual", None) or self._manual_ws()
        for row in sorted(set(rows), reverse=True):  # du bas vers le haut : les numéros restent valables
            ws.delete_rows(row)

    def append_note(self, url: str, text: str) -> None:
        """Ajoute un texte aux notes d'une offre (sans effacer l'existant)."""
        row = self._find_row(job_id(url))
        if row is None:
            return
        current = self._values[row - 1]
        notes = current[COLUMNS.index("notes")] if len(current) > COLUMNS.index("notes") else ""
        self.upsert(url, notes=(f"{notes} | {text}" if notes else text)[:1000])

    def upsert(self, url: str, **fields: Any) -> str:
        """Ajoute ou met à jour une ligne (clé = URL de l'offre), en un seul appel API."""
        jid = job_id(url)
        row = self._find_row(jid)
        if row is None:
            record = {c: "" for c in COLUMNS} | {
                "id": jid, "date_ajout": date.today().isoformat(), "url": url, "statut": "à étudier",
            }
            row = len(self._values) + 1
        else:
            current = self._values[row - 1]
            record = dict(zip(COLUMNS, current + [""] * (len(COLUMNS) - len(current))))
        for name, value in fields.items():
            if name in COLUMNS and name != "id" and value is not None:
                record[name] = value
        values = [record[c] for c in COLUMNS]
        self.ws.update([values], f"A{row}:{rowcol_to_a1(row, len(COLUMNS))}", raw=True)
        cached = [str(v) for v in values]
        if row > len(self._values):
            self._values.append(cached)
        else:
            self._values[row - 1] = cached
        return jid
