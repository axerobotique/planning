"""Application indépendante : consultation et édition du planning atelier.

Source : Google Sheet "Planning" > onglet "PLANNING quotidien". Chaque
technicien occupe un bloc de lignes contiguës (colonne C = nom sur la 1ère
ligne du bloc, vide sur les suivantes) ; chaque ligne du bloc est un
emplacement de tâche, une cellule-jour peut donc empiler plusieurs tâches
(une par ligne du bloc ayant du texte ce jour-là).
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import date, timedelta

from flask import Flask, jsonify, render_template, request

import sheets_client

app = Flask(__name__)

# Ancrage de la reconstruction des dates : la feuille n'affiche que "dd/mm"
# (pas d'année) donc on retrouve l'année réelle en se calant sur le premier
# jour de la feuille (col D = 01/05/2025, un jeudi — confirmé par la ligne
# "JEUDI" du sheet en face de cette date).
SHEET_START_DATE = date(2025, 5, 1)
SHEET_START_COL = 4  # colonne D (A=1, B=2, C=3, D=4)
FIRST_EMPLOYEE_ROW = 5  # 1-based : ligne 3 = dates, ligne 4 = jours, ligne 5 = 1er employé

WEEKS_SHOWN = 4
DAYS_SHOWN = WEEKS_SHOWN * 7

# Garde-fou anti-catastrophe : quoi qu'il arrive côté client (bug de calcul de
# date, requête forgée, etc.), le serveur ne doit jamais pouvoir écrire/vider
# une tâche sur une plage de centaines de colonnes du sheet (cf. incident où
# un bug JS a produit une plage de ~400 jours et l'a écrite telle quelle).
MAX_TASK_SPAN_DAYS = 180

JOURS_COURTS = ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"]

CODES = {
    "ROB": ("Robotique", "#3b6fe0"),
    "CAB": ("Câblage", "#8e44ad"),
    "ELE": ("Électrique", "#e08325"),
    "AUT": ("Automatisme", "#178a76"),
    "MEC": ("Mécanique", "#6b7280"),
    "MAP": ("Mise au point", "#c0392b"),
    "MES": ("Mise en service", "#2472a4"),
    "DEP": ("Dépannage", "#b3541e"),
    "DEV": ("Développement", "#6c5ce7"),
    "FIT": ("Montage/Fitting", "#00897b"),
    "RDV": ("Rendez-vous", "#546e7a"),
    "SN": ("SAV / Support", "#00838f"),
    "ASTREINTE": ("Astreinte", "#c2185b"),
}
STATUT_STYLES = {
    "FÉRIÉ": {"bg": "#eceff1", "fg": "#607d8b", "italic": True},
    "OK": {"bg": "#e6f4ea", "fg": "#1e7e34", "italic": False},
    "CONGES": {"bg": "#eceff1", "fg": "#607d8b", "italic": True},
    "CONGÉS": {"bg": "#eceff1", "fg": "#607d8b", "italic": True},
}

# Le numéro d'affaire d'une tâche est encodé directement dans le texte brut de
# la cellule (ex: "ROB-ITRON [AFF:ITRON-2026-042]"), pas dans une colonne à
# part : ça le fait voyager naturellement avec le texte lors d'un
# déplacement/duplication/édition, sans mapping externe fragile (une ligne
# vidée peut être réutilisée par une tâche totalement différente — cf.
# `place_task`). Le marqueur est retiré avant affichage.
AFFAIRE_MARKER_RE = re.compile(r"\s*\[AFF:([^\]]*)\]\s*$")

TACHES_SHEET = "Taches"
TACHES_HEADER = ["Numéro affaire", "Texte", "Fait", "Assigné"]
TACHES_READ_RANGE = "A2:D2000"


class PlanningError(Exception):
    """Erreur métier renvoyée telle quelle au front (400)."""


def col_letter(n: int) -> str:
    """Numéro de colonne (1-based) -> lettre(s) A1."""
    letters = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def offset_for_date(d: date) -> int:
    return (d - SHEET_START_DATE).days


def date_for_offset(offset: int) -> date:
    return SHEET_START_DATE + timedelta(days=offset)


def col_for_date(d: date) -> int:
    return SHEET_START_COL + offset_for_date(d)


def date_range(d1: date, d2: date) -> list[date]:
    n = (d2 - d1).days + 1
    if n <= 0:
        raise PlanningError("La date de fin doit être postérieure ou égale à la date de début.")
    if n > MAX_TASK_SPAN_DAYS:
        raise PlanningError(f"Plage de dates trop longue ({n} jours, maximum {MAX_TASK_SPAN_DAYS}).")
    return [d1 + timedelta(days=i) for i in range(n)]


def check_span(dates: list[date]) -> None:
    """Même garde-fou que `date_range`, pour les endpoints qui reçoivent une
    liste de dates déjà énumérée par le client plutôt qu'un couple début/fin."""
    if len(dates) > MAX_TASK_SPAN_DAYS:
        raise PlanningError(f"Plage de dates trop longue ({len(dates)} jours, maximum {MAX_TASK_SPAN_DAYS}).")


def parse_iso(s: str) -> date:
    try:
        return date.fromisoformat(s)
    except (TypeError, ValueError):
        raise PlanningError(f"Date invalide : {s!r}")


def strip_affaire_marker(raw: str) -> tuple[str, str]:
    """Sépare le texte affiché du numéro d'affaire éventuellement encodé en fin de cellule."""
    m = AFFAIRE_MARKER_RE.search(raw)
    if not m:
        return raw, ""
    return raw[: m.start()].rstrip(), m.group(1).strip()


def with_affaire_marker(text: str, affaire: str) -> str:
    text = text.strip()
    affaire = (affaire or "").strip()
    return f"{text} [AFF:{affaire}]" if affaire else text


# Palette pour les tâches liées à une affaire mais dont le texte ne suit pas
# la convention "CODE - ..." (ex: "13h55 : garage GG 835 PM") : couleur
# stable par numéro d'affaire (hash), pour qu'une affaire reste identifiable
# visuellement même sans code technique reconnu, plutôt que de rester grise.
AFFAIRE_COLOR_PALETTE = [
    "#2d6a4f", "#7209b7", "#e85d04", "#0077b6", "#9d4edd",
    "#ae2012", "#118ab2", "#7f5539", "#5f0f40", "#386641",
]


def color_for_affaire(affaire: str) -> str:
    digest = hashlib.md5(affaire.encode("utf-8")).digest()
    return AFFAIRE_COLOR_PALETTE[digest[0] % len(AFFAIRE_COLOR_PALETTE)]


def cell_style(text: str, affaire: str = "") -> dict:
    """Détermine code/couleur d'affichage à partir du texte brut d'une tâche."""
    clean = " ".join(text.split())  # collapse les \n internes à une cellule
    upper = clean.upper()
    if upper in STATUT_STYLES:
        s = STATUT_STYLES[upper]
        return {"text": clean, "bg": s["bg"], "fg": s["fg"], "italic": s["italic"]}

    code = None
    for sep in (" - ", "-"):
        if sep in clean:
            candidate = clean.split(sep, 1)[0].strip().upper()
            if candidate in CODES:
                code = candidate
                break
    if code:
        _, color = CODES[code]
        return {"text": clean, "bg": color, "fg": "#ffffff", "italic": False}

    if affaire:
        return {"text": clean, "bg": color_for_affaire(affaire), "fg": "#ffffff", "italic": False}

    # Libellé qui ne suit pas la convention "CODE - ..." et sans n° d'affaire
    # (ex: nom de client collé directement) : gris neutre plutôt qu'une
    # couleur arbitraire par texte, qui rendrait le planning illisible
    # ("effet confetti").
    return {"text": clean, "bg": "#e9edf2", "fg": "#33404d", "italic": False}


def parse_task_parts(clean_text: str) -> dict:
    """Décompose le texte brut d'une tâche (marqueur affaire déjà retiré, \n
    internes conservés) en (code, client, texte) pour l'édition/l'affichage
    structuré : 1ère ligne "CODE - Client" (ou "CODE-Client"), lignes
    suivantes = texte libre. Sans code reconnu, tout va dans `client` (aucune
    perte d'info) : c'est ce qui permet d'éditer aussi les anciennes tâches
    qui ne suivent pas la convention, ou les statuts (OK/FÉRIÉ/CONGÉS)."""
    first_line, _, rest = clean_text.partition("\n")
    first_line = first_line.strip()
    texte = rest.strip()
    if first_line.upper() in STATUT_STYLES:
        return {"code": "", "client": "", "texte": clean_text.strip()}

    code = ""
    client = first_line
    for sep in (" - ", "-"):
        if sep in first_line:
            candidate = first_line.split(sep, 1)[0].strip().upper()
            if candidate in CODES:
                code = candidate
                client = first_line.split(sep, 1)[1].strip()
                break
    if not code and first_line.upper() in CODES:
        # Code seul, sans client (ex: "AUT" tout seul, pas de séparateur à
        # trouver) : sans ce cas, un aller-retour édition→enregistrement le
        # transformait silencieusement en "client" sans code.
        code = first_line.upper()
        client = ""
    return {"code": code, "client": client, "texte": texte}


def sheet_last_row() -> int:
    row_count, _ = sheets_client.get_grid_size()
    return row_count


def sheet_last_col() -> int:
    _, col_count = sheets_client.get_grid_size()
    return col_count


def read_employee_blocks() -> list[dict]:
    """Lit toute la colonne C (noms) -> [{name, start_row, end_row}], indépendant
    de la fenêtre de jours affichée."""
    names = sheets_client.get_range(f"C{FIRST_EMPLOYEE_ROW}:C{sheet_last_row()}")
    blocks: list[dict] = []
    current = None
    for idx, row in enumerate(names):
        sheet_row = FIRST_EMPLOYEE_ROW + idx
        name = (row[0] if row else "").strip()
        if name:
            current = {"name": name, "start_row": sheet_row, "end_row": sheet_row}
            blocks.append(current)
        elif current is not None:
            current["end_row"] = sheet_row
    return blocks


def find_block(blocks: list[dict], employee: str) -> dict:
    for b in blocks:
        if b["name"] == employee:
            return b
    raise PlanningError(f"Technicien inconnu : {employee!r}")


def find_block_for_row(blocks: list[dict], row: int) -> dict:
    for b in blocks:
        if b["start_row"] <= row <= b["end_row"]:
            return b
    raise PlanningError(f"Aucun technicien ne possède la ligne {row}.")


def place_task(employee: str, d_start: date, d_end: date, text: str) -> int:
    """Trouve (ou crée) une ligne libre dans le bloc du technicien couvrant toute
    la plage de dates, y écrit `text`, renvoie le numéro de ligne utilisé."""
    text = text.strip()
    if not text:
        raise PlanningError("Le texte de la tâche est vide.")

    blocks = read_employee_blocks()
    block = find_block(blocks, employee)

    dates = date_range(d_start, d_end)
    cols = [col_for_date(d) for d in dates]
    col_start, col_end = min(cols), max(cols)
    col_start_letter, col_end_letter = col_letter(col_start), col_letter(col_end)

    block_height = block["end_row"] - block["start_row"] + 1
    existing = sheets_client.get_range(
        f"{col_start_letter}{block['start_row']}:{col_end_letter}{block['end_row']}"
    )

    target_row = None
    for i in range(block_height):
        row_vals = existing[i] if i < len(existing) else []
        if all(not (row_vals[k].strip() if k < len(row_vals) and row_vals[k] else "") for k in range(len(cols))):
            target_row = block["start_row"] + i
            break

    if target_row is None:
        target_row = block["end_row"] + 1
        sheets_client.insert_row_before(target_row)

    sheets_client.update_range(
        f"{col_start_letter}{target_row}:{col_end_letter}{target_row}",
        [[text] * len(cols)],
    )
    return target_row


def clear_task(row: int, dates: list[date]) -> None:
    ranges = [f"{col_letter(col_for_date(d))}{row}" for d in dates]
    sheets_client.clear_ranges(ranges)


def build_grid(week_offset: int) -> dict:
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    start_date = monday + timedelta(weeks=week_offset)
    start_offset = offset_for_date(start_date)
    max_offset = sheet_last_col() - SHEET_START_COL

    out_of_range = start_offset < 0 or start_offset > max_offset
    if out_of_range:
        return {
            "employees": [],
            "days": [],
            "week_offset": week_offset,
            "error": "Cette période est hors de la plage couverte par le planning.",
            "warning": None,
        }

    num_days = min(DAYS_SHOWN, max_offset - start_offset + 1)
    end_offset = start_offset + num_days - 1
    col_start_letter = col_letter(SHEET_START_COL + start_offset)
    col_end_letter = col_letter(SHEET_START_COL + end_offset)

    names_rows = sheets_client.get_range(f"C{FIRST_EMPLOYEE_ROW}:C{sheet_last_row()}")
    data_rows = sheets_client.get_range(
        f"{col_start_letter}{FIRST_EMPLOYEE_ROW}:{col_end_letter}{sheet_last_row()}"
    )
    date_check = sheets_client.get_range(f"{col_start_letter}3:{col_start_letter}3")

    # Une tâche peut s'étendre au-delà de la fenêtre affichée (ex : astreinte
    # à durée indéterminée). On lit 1 colonne de part et d'autre pour savoir
    # si un fragment touchant un bord de fenêtre est réellement complet ou
    # tronqué — sans ça, déplacer un fragment tronqué ne connaît pas sa vraie
    # étendue et écrase/duplique la mauvaise plage dans le sheet.
    before_col = SHEET_START_COL + start_offset - 1
    after_col = SHEET_START_COL + end_offset + 1
    before_rows = (
        sheets_client.get_range(f"{col_letter(before_col)}{FIRST_EMPLOYEE_ROW}:{col_letter(before_col)}{sheet_last_row()}")
        if before_col >= SHEET_START_COL else []
    )
    after_rows = (
        sheets_client.get_range(f"{col_letter(after_col)}{FIRST_EMPLOYEE_ROW}:{col_letter(after_col)}{sheet_last_row()}")
        if after_col <= SHEET_START_COL + max_offset else []
    )

    warning = None
    expected = date_for_offset(start_offset).strftime("%d/%m")
    actual = date_check[0][0] if date_check and date_check[0] else ""
    if actual and actual != expected:
        warning = (
            f"La date lue dans le sheet ({actual}) ne correspond pas à celle "
            f"attendue ({expected}) — la structure du planning a peut-être changé."
        )

    days_info = []
    for j in range(num_days):
        d = start_date + timedelta(days=j)
        days_info.append({
            "iso": d.isoformat(),
            "label": d.strftime("%d/%m"),
            "dow": JOURS_COURTS[d.weekday()],
            "is_today": d == today,
            "is_weekend": d.weekday() >= 5,
            "new_week": d.weekday() == 0,
        })

    employees = []
    current = None
    current_texts: list[str] = []
    block_start_idx = 0
    for idx, name_row in enumerate(names_rows):
        sheet_row = FIRST_EMPLOYEE_ROW + idx
        name = (name_row[0] if name_row else "").strip()
        if name:
            current = {"name": name, "cells": [[] for _ in range(num_days)], "_max_slot": -1}
            employees.append(current)
            block_start_idx = idx
        if current is None:
            continue
        slot = idx - block_start_idx
        r = data_rows[idx] if idx < len(data_rows) else []
        texts = [(r[j].strip() if j < len(r) and r[j] else "") for j in range(num_days)]
        before_val = (before_rows[idx][0].strip() if idx < len(before_rows) and before_rows[idx] else "")
        after_val = (after_rows[idx][0].strip() if idx < len(after_rows) and after_rows[idx] else "")

        # Détecte les runs de jours consécutifs à texte identique sur cette
        # ligne -> un seul fragment "étalé" (multi-jours), plutôt qu'un
        # fragment par jour.
        j = 0
        while j < num_days:
            if not texts[j]:
                j += 1
                continue
            k = j
            while k + 1 < num_days and texts[k + 1] == texts[j]:
                k += 1
            clean_text, affaire = strip_affaire_marker(texts[j])
            style = cell_style(clean_text, affaire)
            parts = parse_task_parts(clean_text)
            # Fragment tronqué = la même tâche continue hors fenêtre (à gauche
            # et/ou à droite) : on ne connaît pas sa vraie étendue, donc pas de
            # glisser-déposer dessus (cf. bug de duplication sur ASTREINTE).
            truncated = (j == 0 and before_val == texts[j]) or (k == num_days - 1 and after_val == texts[k])
            frag_base = {
                "row": sheet_row,
                "text": style["text"],
                # `raw_text` (avec \n internes) plutôt que `text` (aplati) pour
                # le glisser-déposer/duplication : sans ça, chaque déplacement
                # fusionnait la ligne "code-client" et le texte libre en une
                # seule ligne (perte de la frontière entre `client` et
                # `texte`, cf. `parse_task_parts`).
                "raw_text": clean_text,
                "code": parts["code"],
                "client": parts["client"],
                "texte": parts["texte"],
                "affaire": affaire,
                "bg": style["bg"],
                "fg": style["fg"],
                "italic": style["italic"],
                "date_start": days_info[j]["iso"],
                "date_end": days_info[k]["iso"],
                "truncated": truncated,
                "slot": slot,
            }
            current["_max_slot"] = max(current["_max_slot"], slot)
            for jj in range(j, k + 1):
                current["cells"][jj].append({
                    **frag_base,
                    "is_start": jj == j,
                    "is_end": jj == k,
                })
            j = k + 1

    # Réordonne les lignes (slots) de chaque bloc technicien : la tâche la
    # plus longue (en jours, sur la période affichée) en haut, la plus
    # courte en bas. La mesure retenue par slot est l'étendue max d'un seul
    # fragment (pas la somme sur toute la période), pour rester intuitif
    # visuellement.
    for emp in employees:
        slot_span: dict[int, int] = {}
        for day_frags in emp["cells"]:
            for f in day_frags:
                span = (date.fromisoformat(f["date_end"]) - date.fromisoformat(f["date_start"])).days + 1
                if span > slot_span.get(f["slot"], 0):
                    slot_span[f["slot"]] = span
        if slot_span:
            ordered_slots = sorted(slot_span, key=lambda s: (-slot_span[s], s))
            slot_map = {old: new for new, old in enumerate(ordered_slots)}
            for day_frags in emp["cells"]:
                for f in day_frags:
                    f["slot"] = slot_map[f["slot"]]
            # Le tri peut avoir "compacté" les numéros de slot (une ligne
            # physique sans aucune tâche cette période n'apparaît dans aucun
            # fragment) : _max_slot doit refléter le nouveau compte, sinon la
            # boucle de placeholders ci-dessous ajoute des lignes vides
            # superflues en bas de bloc.
            emp["_max_slot"] = len(ordered_slots) - 1

    # Une ligne technicien sans tâche un jour donné ne doit pas laisser la
    # ligne suivante remonter prendre sa place visuelle : ça décale les
    # tâches d'un jour à l'autre et casse l'alignement horizontal. On
    # réserve donc un "placeholder" invisible à chaque emplacement (slot) de
    # ligne source qui a au moins une tâche quelque part dans la fenêtre
    # affichée, pour les jours où cette ligne précise est vide.
    for emp in employees:
        max_slot = emp.pop("_max_slot")
        if max_slot < 0:
            emp["rows"] = 1
            continue
        for jj, day_frags in enumerate(emp["cells"]):
            present = {f["slot"] for f in day_frags}
            for slot in range(max_slot + 1):
                if slot not in present:
                    day_frags.append({
                        "row": None,
                        "text": "",
                        "raw_text": "",
                        "code": "",
                        "client": "",
                        "texte": "",
                        "affaire": "",
                        "bg": None,
                        "fg": None,
                        "italic": False,
                        "date_start": days_info[jj]["iso"],
                        "date_end": days_info[jj]["iso"],
                        "truncated": False,
                        "slot": slot,
                        "is_start": True,
                        "is_end": True,
                        "placeholder": True,
                    })
            day_frags.sort(key=lambda f: f["slot"])
        emp["rows"] = max_slot + 1

    return {
        "employees": employees,
        "days": days_info,
        "week_offset": week_offset,
        "error": None,
        "warning": warning,
    }


# True une fois la migration de colonne "Assigné" (ajoutée après coup) vérifiée
# sur l'onglet Taches, pour ne pas réécrire l'en-tête à chaque requête.
_taches_migrated = False


def ensure_taches_sheet() -> None:
    global _taches_migrated
    sheets_client.ensure_sheet(TACHES_SHEET, TACHES_HEADER)
    if not _taches_migrated:
        # Onglet créé avant l'ajout de la colonne "Assigné" (3 colonnes à
        # l'origine) : ensure_sheet() n'écrit l'en-tête qu'à la création, donc
        # on complète ici sans toucher aux données existantes.
        sheets_client.update_range("D1", [["Assigné"]], sheet=TACHES_SHEET)
        _taches_migrated = True


def read_affaire_tasks(numero_affaire: str) -> list[dict]:
    ensure_taches_sheet()
    rows = sheets_client.get_range(TACHES_READ_RANGE, sheet=TACHES_SHEET)
    out = []
    for idx, r in enumerate(rows):
        sheet_row = 2 + idx
        numero = (r[0].strip() if len(r) > 0 and r[0] else "")
        if numero != numero_affaire:
            continue
        texte = (r[1].strip() if len(r) > 1 and r[1] else "")
        if not texte:
            continue  # ligne vidée par une suppression précédente
        fait = bool(r[2].strip()) if len(r) > 2 and r[2] else False
        assigne = (r[3].strip() if len(r) > 3 and r[3] else "")
        out.append({"row": sheet_row, "texte": texte, "fait": fait, "assigne": assigne})
    return out


def add_affaire_task(numero_affaire: str, texte: str, assigne: str = "") -> None:
    ensure_taches_sheet()
    sheets_client.append_row([numero_affaire, texte, "", assigne], sheet=TACHES_SHEET)


def set_affaire_task_done(row: int, fait: bool) -> None:
    sheets_client.update_range(f"C{row}", [["FAIT" if fait else ""]], sheet=TACHES_SHEET)


def set_affaire_task_assignee(row: int, assigne: str) -> None:
    sheets_client.update_range(f"D{row}", [[assigne]], sheet=TACHES_SHEET)


def delete_affaire_task(row: int) -> None:
    sheets_client.clear_ranges([f"A{row}:D{row}"], sheet=TACHES_SHEET)


@app.route("/")
def planning():
    try:
        week_offset = int(request.args.get("s", "0"))
    except ValueError:
        week_offset = 0
    return render_template("planning.html", week_offset=week_offset, legend=CODES)


@app.route("/api/grid")
def api_grid():
    try:
        week_offset = int(request.args.get("s", "0"))
    except ValueError:
        week_offset = 0
    grid = build_grid(week_offset)
    grid["legend"] = {code: {"label": label, "color": color} for code, (label, color) in CODES.items()}
    return jsonify(grid)


@app.route("/api/task/save", methods=["POST"])
def api_task_save():
    body = request.get_json(force=True, silent=True) or {}
    try:
        employee = (body.get("employee") or "").strip()
        text = (body.get("text") or "").strip()
        affaire = (body.get("affaire") or "").strip()
        d_start = parse_iso(body.get("date_start"))
        d_end = parse_iso(body.get("date_end"))
        row = body.get("row")
        old_dates = [parse_iso(d) for d in (body.get("old_dates") or [])]
        check_span(old_dates)

        if not employee:
            raise PlanningError("Technicien manquant.")
        if not text:
            raise PlanningError("Le texte de la tâche est vide.")

        raw_text = with_affaire_marker(text, affaire)

        if row is None:
            place_task(employee, d_start, d_end, raw_text)
        else:
            row = int(row)
            blocks = read_employee_blocks()
            owner = find_block_for_row(blocks, row)
            new_dates = set(date_range(d_start, d_end))
            if owner["name"] == employee:
                stale = [d for d in old_dates if d not in new_dates]
                if stale:
                    clear_task(row, stale)
                cols = [col_for_date(d) for d in sorted(new_dates)]
                col_start_letter, col_end_letter = col_letter(min(cols)), col_letter(max(cols))
                sheets_client.update_range(
                    f"{col_start_letter}{row}:{col_end_letter}{row}",
                    [[raw_text] * len(cols)],
                )
            else:
                if old_dates:
                    clear_task(row, old_dates)
                place_task(employee, d_start, d_end, raw_text)
        return jsonify({"ok": True})
    except PlanningError as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/task/delete", methods=["POST"])
def api_task_delete():
    body = request.get_json(force=True, silent=True) or {}
    try:
        row = int(body.get("row"))
        dates = [parse_iso(d) for d in (body.get("dates") or [])]
        if not dates:
            raise PlanningError("Aucune date à supprimer.")
        check_span(dates)
        clear_task(row, dates)
        return jsonify({"ok": True})
    except PlanningError as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/task/relocate", methods=["POST"])
def api_task_relocate():
    body = request.get_json(force=True, silent=True) or {}
    try:
        row = int(body.get("row"))
        dates = [parse_iso(d) for d in (body.get("dates") or [])]
        text = (body.get("text") or "").strip()
        affaire = (body.get("affaire") or "").strip()
        target_employee = (body.get("target_employee") or "").strip()
        d_start = parse_iso(body.get("date_start"))
        d_end = parse_iso(body.get("date_end"))
        mode = body.get("mode") or "move"

        if not dates:
            raise PlanningError("Aucune date source.")
        check_span(dates)
        if not text:
            raise PlanningError("Texte de la tâche manquant.")
        if not target_employee:
            raise PlanningError("Technicien cible manquant.")
        if mode not in ("move", "duplicate"):
            raise PlanningError(f"Mode inconnu : {mode!r}")

        if mode == "move":
            clear_task(row, dates)
        place_task(target_employee, d_start, d_end, with_affaire_marker(text, affaire))
        return jsonify({"ok": True})
    except PlanningError as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/affaire/tasks")
def api_affaire_tasks():
    numero = (request.args.get("numero") or "").strip()
    if not numero:
        return jsonify({"ok": True, "tasks": []})
    return jsonify({"ok": True, "tasks": read_affaire_tasks(numero)})


@app.route("/api/affaire/tasks/add", methods=["POST"])
def api_affaire_tasks_add():
    body = request.get_json(force=True, silent=True) or {}
    numero = (body.get("numero_affaire") or "").strip()
    texte = (body.get("texte") or "").strip()
    assigne = (body.get("assigne") or "").strip()
    if not numero:
        return jsonify({"ok": False, "error": "Numéro d'affaire manquant."}), 400
    if not texte:
        return jsonify({"ok": False, "error": "Texte de la sous-tâche vide."}), 400
    add_affaire_task(numero, texte, assigne)
    return jsonify({"ok": True, "tasks": read_affaire_tasks(numero)})


@app.route("/api/affaire/tasks/toggle", methods=["POST"])
def api_affaire_tasks_toggle():
    body = request.get_json(force=True, silent=True) or {}
    try:
        row = int(body.get("row"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Ligne invalide."}), 400
    set_affaire_task_done(row, bool(body.get("fait")))
    return jsonify({"ok": True})


@app.route("/api/affaire/tasks/assign", methods=["POST"])
def api_affaire_tasks_assign():
    body = request.get_json(force=True, silent=True) or {}
    try:
        row = int(body.get("row"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Ligne invalide."}), 400
    set_affaire_task_assignee(row, (body.get("assigne") or "").strip())
    return jsonify({"ok": True})


@app.route("/api/affaire/tasks/delete", methods=["POST"])
def api_affaire_tasks_delete():
    body = request.get_json(force=True, silent=True) or {}
    try:
        row = int(body.get("row"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Ligne invalide."}), 400
    delete_affaire_task(row)
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5050"))
    # threaded=True : sans ça, une requête bloquée sur un appel Google Sheets
    # lent gèle tout le serveur pour tous les utilisateurs (le client Sheets
    # utilise déjà un service par thread — cf. `_thread_local` dans
    # sheets_client.py — donc le mode threadé est sûr).
    app.run(host="0.0.0.0", port=port, debug=True, threaded=True)
