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
import secrets
from datetime import date, timedelta

from flask import Flask, Response, jsonify, render_template, request

import sheets_client

app = Flask(__name__)

# Protection minimale par mot de passe partagé : l'app n'a aucune notion
# d'utilisateur/session, donc dès qu'elle est exposée publiquement (Cloud
# Run, etc.) n'importe qui avec le lien pourrait sinon lire ET modifier le
# planning. PLANNING_PASSWORD doit être défini en variable d'env sur tout
# déploiement public ; en local, si elle est absente, l'accès reste libre
# (confort de dev).
PLANNING_PASSWORD = os.environ.get("PLANNING_PASSWORD")


@app.before_request
def _require_password():
    if not PLANNING_PASSWORD:
        return
    auth = request.authorization
    if not auth or not secrets.compare_digest(auth.password or "", PLANNING_PASSWORD):
        return Response(
            "Authentification requise.",
            401,
            {"WWW-Authenticate": 'Basic realm="Planning ARA"'},
        )

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

# Amorçage de l'onglet "Codes" (cf. plus bas) à sa création : au-delà de ce
# premier amorçage, le sheet est la seule source de vérité pour les codes —
# ceci ne sert plus qu'à peupler l'onglet une fois. Palette par famille :
# ROB/CAB/ELE/AUT/DEV en bleus froids, MAP/MES/DEP en couleurs chaudes.
DEFAULT_CODES = [
    ("ROB", "Robotique", "#2563eb"),
    ("CAB", "Câblage", "#0ea5e9"),
    ("ELE", "Électrique", "#0891b2"),
    ("AUT", "Automatisme", "#1e40af"),
    ("DEV", "Développement", "#4338ca"),
    ("MEC", "Mécanique", "#6b7280"),
    ("MAP", "Mise au point", "#dc2626"),
    ("MES", "Mise en service", "#ea580c"),
    ("DEP", "Dépannage", "#b45309"),
    ("FIT", "Montage/Fitting", "#00897b"),
    ("RDV", "Rendez-vous", "#546e7a"),
    ("SN", "SAV / Support", "#00838f"),
    ("ASTREINTE", "Astreinte", "#c2185b"),
]
DEFAULT_CODES_MAP = {code: (label, color) for code, label, color in DEFAULT_CODES}
STATUT_STYLES = {
    "FÉRIÉ": {"bg": "#eceff1", "fg": "#607d8b", "italic": True},
    "OK": {"bg": "#e6f4ea", "fg": "#1e7e34", "italic": False},
    "CONGES": {"bg": "#eceff1", "fg": "#607d8b", "italic": True},
    "CONGÉS": {"bg": "#eceff1", "fg": "#607d8b", "italic": True},
}

# Le numéro d'affaire et l'éventuel identifiant de groupe multi-technicien
# d'une tâche sont encodés directement dans le texte brut de la cellule (ex:
# "ROB-ITRON [AFF:ITRON-2026-042] [GRP:a1b2c3d4]"), pas dans une colonne à
# part : ça le fait voyager naturellement avec le texte lors d'un
# déplacement/duplication/édition, sans mapping externe fragile (une ligne
# vidée peut être réutilisée par une tâche totalement différente — cf.
# `place_task`). Les marqueurs sont retirés avant affichage.
MARKER_RE = re.compile(r"\s*\[(AFF|GRP):([^\]]*)\]\s*$")

TACHES_SHEET = "Taches"
TACHES_HEADER = ["Numéro affaire", "Texte", "Fait", "Assigné"]
TACHES_READ_RANGE = "A2:D2000"

# Les codes de la légende (ROB, MEC, ...) sont gérés depuis un onglet dédié
# plutôt que codés en dur : ajout/édition/suppression depuis l'UI, partagés
# par tous et persistants entre redéploiements (même principe que l'onglet
# "Taches"). DEFAULT_CODES ci-dessus ne sert plus qu'à amorcer cet onglet à
# sa création.
CODES_SHEET = "Codes"
CODES_HEADER = ["Code", "Libellé", "Couleur"]
CODES_READ_RANGE = "A2:C300"
HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
# Identifiant de code : lettres/chiffres seulement, sans espace ni tiret — un
# "-" casserait le parsing "CODE - Client" / "CODE-Client" qui isole le code
# en tête de cellule (cf. `cell_style`/`parse_task_parts`).
CODE_ID_RE = re.compile(r"^[A-Z0-9ÀÂÄÉÈÊËÎÏÔÖÙÛÜÇ]{1,20}$")


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


def strip_markers(raw: str) -> tuple[str, str, str]:
    """Sépare le texte affiché du numéro d'affaire et de l'identifiant de
    groupe éventuellement encodés en fin de cellule (dans n'importe quel
    ordre)."""
    text = raw
    affaire = ""
    group = ""
    while True:
        m = MARKER_RE.search(text)
        if not m:
            break
        key, val = m.group(1), m.group(2).strip()
        text = text[: m.start()].rstrip()
        if key == "AFF":
            affaire = val
        else:
            group = val
    return text, affaire, group


def with_markers(text: str, affaire: str = "", group: str = "") -> str:
    text = text.strip()
    affaire = (affaire or "").strip()
    group = (group or "").strip()
    if affaire:
        text = f"{text} [AFF:{affaire}]"
    if group:
        text = f"{text} [GRP:{group}]"
    return text


def new_group_id() -> str:
    return secrets.token_hex(4)


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


def contrasting_fg(hex_color: str) -> str:
    """Texte blanc ou sombre selon la luminosité du fond : une couleur de code
    personnalisée par l'utilisateur peut être claire, contrairement aux
    couleurs par défaut (toutes sombres) qui supportaient du blanc sans
    vérification."""
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255
    return "#1f2430" if luminance > 0.6 else "#ffffff"


# True une fois l'amorçage de l'onglet "Codes" vérifié pour ce process, pour
# ne pas relire le sheet à chaque requête juste pour ça (même principe que
# `_taches_migrated`).
_codes_seeded = False


def ensure_codes_sheet() -> None:
    global _codes_seeded
    sheets_client.ensure_sheet(CODES_SHEET, CODES_HEADER)
    if _codes_seeded:
        return
    rows = sheets_client.get_range(CODES_READ_RANGE, sheet=CODES_SHEET)
    if not rows:
        sheets_client.update_range(
            f"A2:C{1 + len(DEFAULT_CODES)}",
            [[code, label, color] for code, label, color in DEFAULT_CODES],
            sheet=CODES_SHEET,
        )
    _codes_seeded = True


def read_codes() -> dict[str, tuple[str, str]]:
    """dict code -> (libellé, couleur), lu depuis l'onglet "Codes" — seule
    source de vérité pour la légende (plus de dict en dur, cf. DEFAULT_CODES
    qui ne sert qu'à l'amorçage)."""
    ensure_codes_sheet()
    rows = sheets_client.get_range(CODES_READ_RANGE, sheet=CODES_SHEET)
    codes: dict[str, tuple[str, str]] = {}
    for r in rows:
        code = (r[0].strip().upper() if len(r) > 0 and r[0] else "")
        if not code:
            continue
        label = (r[1].strip() if len(r) > 1 and r[1] else code)
        color = (r[2].strip() if len(r) > 2 and r[2] else "")
        if not HEX_COLOR_RE.match(color):
            color = "#6b7280"
        codes[code] = (label, color)
    return codes


def validate_code_id(raw: str) -> str:
    code = (raw or "").strip().upper()
    if not CODE_ID_RE.match(code):
        raise PlanningError(
            f"Code invalide : {raw!r} (lettres/chiffres uniquement, sans espace ni tiret)."
        )
    return code


def save_code(raw_code: str, label: str, color: str) -> None:
    """Crée le code s'il n'existe pas encore, sinon met à jour son libellé et
    sa couleur. L'identifiant du code lui-même n'est jamais modifié une fois
    créé (renommer casserait la reconnaissance des tâches déjà écrites dans
    le sheet sous l'ancien code) : pour "renommer", il faut créer le nouveau
    code puis supprimer l'ancien."""
    code = validate_code_id(raw_code)
    label = (label or "").strip()
    color = (color or "").strip()
    if not label:
        raise PlanningError("Libellé manquant.")
    if not HEX_COLOR_RE.match(color):
        raise PlanningError(f"Couleur invalide : {color!r}")

    ensure_codes_sheet()
    rows = sheets_client.get_range(CODES_READ_RANGE, sheet=CODES_SHEET)
    for idx, r in enumerate(rows):
        existing = (r[0].strip().upper() if len(r) > 0 and r[0] else "")
        if existing == code:
            sheets_client.update_range(f"A{2 + idx}:C{2 + idx}", [[code, label, color]], sheet=CODES_SHEET)
            return
    sheets_client.append_row([code, label, color], sheet=CODES_SHEET)


def delete_code(raw_code: str) -> None:
    code = validate_code_id(raw_code)
    ensure_codes_sheet()
    rows = sheets_client.get_range(CODES_READ_RANGE, sheet=CODES_SHEET)
    for idx, r in enumerate(rows):
        existing = (r[0].strip().upper() if len(r) > 0 and r[0] else "")
        if existing == code:
            sheets_client.clear_ranges([f"A{2 + idx}:C{2 + idx}"], sheet=CODES_SHEET)
            return
    raise PlanningError(f"Code inconnu : {code!r}")


def cell_style(text: str, affaire: str = "", codes: dict | None = None) -> dict:
    """Détermine code/couleur d'affichage à partir du texte brut d'une tâche."""
    codes = codes if codes is not None else DEFAULT_CODES_MAP
    clean = " ".join(text.split())  # collapse les \n internes à une cellule
    upper = clean.upper()
    if upper in STATUT_STYLES:
        s = STATUT_STYLES[upper]
        return {"text": clean, "bg": s["bg"], "fg": s["fg"], "italic": s["italic"]}

    # Détection sur la 1ère ligne seulement (comme `parse_task_parts`) : une
    # tâche "MEC\nFin de montage…" a son code seul sur la 1ère ligne, sans
    # séparateur avant le texte libre qui suit — le chercher dans `clean`
    # (toutes les lignes aplaties en une seule, espaces compris) le manquait
    # et affichait ces tâches en gris neutre au lieu de la couleur du code.
    first_line = text.split("\n", 1)[0].strip()
    code = None
    for sep in (" - ", "-"):
        if sep in first_line:
            candidate = first_line.split(sep, 1)[0].strip().upper()
            if candidate in codes:
                code = candidate
                break
    if not code and first_line.upper() in codes:
        code = first_line.upper()
    if code:
        _, color = codes[code]
        return {"text": clean, "bg": color, "fg": contrasting_fg(color), "italic": False}

    if affaire:
        return {"text": clean, "bg": color_for_affaire(affaire), "fg": "#ffffff", "italic": False}

    # Libellé qui ne suit pas la convention "CODE - ..." et sans n° d'affaire
    # (ex: nom de client collé directement) : gris neutre plutôt qu'une
    # couleur arbitraire par texte, qui rendrait le planning illisible
    # ("effet confetti").
    return {"text": clean, "bg": "#e9edf2", "fg": "#33404d", "italic": False}


def parse_task_parts(clean_text: str, codes: dict | None = None) -> dict:
    """Décompose le texte brut d'une tâche (marqueur affaire déjà retiré, \n
    internes conservés) en (code, client, texte) pour l'édition/l'affichage
    structuré : 1ère ligne "CODE - Client" (ou "CODE-Client"), lignes
    suivantes = texte libre. Sans code reconnu, tout va dans `client` (aucune
    perte d'info) : c'est ce qui permet d'éditer aussi les anciennes tâches
    qui ne suivent pas la convention, ou les statuts (OK/FÉRIÉ/CONGÉS)."""
    codes = codes if codes is not None else DEFAULT_CODES_MAP
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
            if candidate in codes:
                code = candidate
                client = first_line.split(sep, 1)[1].strip()
                break
    if not code and first_line.upper() in codes:
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


def build_grid(week_offset: int, codes: dict | None = None) -> dict:
    codes = codes if codes is not None else DEFAULT_CODES_MAP
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
            "groups": {},
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
    # Regroupe les fragments qui partagent le même identifiant de groupe
    # (tâche affectée à plusieurs techniciens à la fois, cf. `with_markers`) :
    # le front-end s'en sert pour retrouver, en éditant une instance, sur
    # quelles autres lignes/techniciens propager le changement.
    groups: dict[str, list[dict]] = {}
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
            clean_text, affaire, group = strip_markers(texts[j])
            style = cell_style(clean_text, affaire, codes)
            parts = parse_task_parts(clean_text, codes)
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
                "group": group,
                "bg": style["bg"],
                "fg": style["fg"],
                "italic": style["italic"],
                "date_start": days_info[j]["iso"],
                "date_end": days_info[k]["iso"],
                "truncated": truncated,
                "slot": slot,
            }
            if group:
                groups.setdefault(group, []).append({
                    "employee": current["name"],
                    "row": sheet_row,
                    "date_start": days_info[j]["iso"],
                    "date_end": days_info[k]["iso"],
                })
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
                        "group": "",
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
        "groups": groups,
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
    return render_template("planning.html", week_offset=week_offset, legend=read_codes())


@app.route("/api/grid")
def api_grid():
    try:
        week_offset = int(request.args.get("s", "0"))
    except ValueError:
        week_offset = 0
    codes = read_codes()
    grid = build_grid(week_offset, codes)
    grid["legend"] = {code: {"label": label, "color": color} for code, (label, color) in codes.items()}
    return jsonify(grid)


def _legend_json() -> dict:
    codes = read_codes()
    return {code: {"label": label, "color": color} for code, (label, color) in codes.items()}


@app.route("/api/codes/save", methods=["POST"])
def api_codes_save():
    """Crée un nouveau code, ou met à jour le libellé/couleur d'un code
    existant (cf. `save_code` — l'identifiant du code n'est jamais renommé)."""
    body = request.get_json(force=True, silent=True) or {}
    try:
        save_code(body.get("code"), body.get("label"), body.get("color"))
        return jsonify({"ok": True, "legend": _legend_json()})
    except PlanningError as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/codes/delete", methods=["POST"])
def api_codes_delete():
    body = request.get_json(force=True, silent=True) or {}
    try:
        delete_code(body.get("code"))
        return jsonify({"ok": True, "legend": _legend_json()})
    except PlanningError as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/task/save", methods=["POST"])
def api_task_save():
    """Crée/édite une tâche, éventuellement affectée à plusieurs techniciens
    à la fois. Chaque technicien occupe sa propre ligne (contrainte du
    sheet — cf. docstring du module) ; les instances liées entre elles par un
    même identifiant de groupe (`with_markers`) sont créées/mises à
    jour/effacées ensemble ici pour rester synchronisées."""
    body = request.get_json(force=True, silent=True) or {}
    try:
        employees = []
        for e in (body.get("employees") or []):
            e = (e or "").strip()
            if e and e not in employees:
                employees.append(e)
        text = (body.get("text") or "").strip()
        affaire = (body.get("affaire") or "").strip()
        d_start = parse_iso(body.get("date_start"))
        d_end = parse_iso(body.get("date_end"))
        row = body.get("row")
        old_dates = [parse_iso(d) for d in (body.get("old_dates") or [])]
        check_span(old_dates)
        group_id = (body.get("group_id") or "").strip()

        members = []
        for m in (body.get("members") or []):
            m_employee = (m.get("employee") or "").strip()
            if not m_employee:
                continue
            m_dates = [parse_iso(d) for d in (m.get("old_dates") or [])]
            check_span(m_dates)
            members.append({"employee": m_employee, "row": int(m.get("row")), "old_dates": m_dates})

        if not employees:
            raise PlanningError("Technicien manquant.")
        if not text:
            raise PlanningError("Le texte de la tâche est vide.")

        # L'identifiant de groupe n'a de sens qu'à partir de 2 techniciens :
        # s'il n'en reste qu'un après édition, on l'abandonne (la tâche
        # redevient une tâche simple).
        group_id = (group_id or new_group_id()) if len(employees) > 1 else ""
        raw_text = with_markers(text, affaire, group_id)

        blocks = read_employee_blocks()
        new_dates = set(date_range(d_start, d_end))
        cols = [col_for_date(d) for d in sorted(new_dates)]
        col_start_letter, col_end_letter = col_letter(min(cols)), col_letter(max(cols))

        # Instances déjà en place pour cette tâche (une par technicien déjà
        # assigné) : la ligne éditée elle-même, plus les autres membres du
        # groupe transmis par le front-end (cf. `groups` dans `build_grid`).
        existing: dict[str, dict] = {}
        if row is not None:
            row = int(row)
            owner = find_block_for_row(blocks, row)
            existing[owner["name"]] = {"row": row, "old_dates": old_dates}
        for m in members:
            existing.setdefault(m["employee"], {"row": m["row"], "old_dates": m["old_dates"]})

        for emp in employees:
            inst = existing.pop(emp, None)
            if inst is None:
                place_task(emp, d_start, d_end, raw_text)
                continue
            owner = find_block_for_row(blocks, inst["row"])
            if owner["name"] == emp:
                stale = [d for d in inst["old_dates"] if d not in new_dates]
                if stale:
                    clear_task(inst["row"], stale)
                sheets_client.update_range(
                    f"{col_start_letter}{inst['row']}:{col_end_letter}{inst['row']}",
                    [[raw_text] * len(cols)],
                )
            else:
                # Ligne réattribuée entre-temps à un autre technicien (état
                # front obsolète) : on libère l'ancien emplacement plutôt que
                # d'écraser la tâche d'un tiers.
                if inst["old_dates"]:
                    clear_task(inst["row"], inst["old_dates"])
                place_task(emp, d_start, d_end, raw_text)

        # Techniciens qui étaient sur cette tâche et ont été désélectionnés.
        for inst in existing.values():
            if inst["old_dates"]:
                clear_task(inst["row"], inst["old_dates"])

        return jsonify({"ok": True})
    except PlanningError as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/task/delete", methods=["POST"])
def api_task_delete():
    """Supprime une ou plusieurs instances d'une tâche (toutes les instances
    liées d'une tâche multi-technicien, ou une seule pour une tâche simple)."""
    body = request.get_json(force=True, silent=True) or {}
    try:
        instances = body.get("instances") or []
        if not instances:
            raise PlanningError("Aucune instance à supprimer.")
        for inst in instances:
            row = int(inst.get("row"))
            dates = [parse_iso(d) for d in (inst.get("dates") or [])]
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
        # Le glisser-déposer agit toujours sur une seule instance : si la
        # tâche source faisait partie d'un groupe multi-technicien, cette
        # copie s'en détache (pas de propagation du déplacement aux autres
        # techniciens du groupe).
        place_task(target_employee, d_start, d_end, with_markers(text, affaire))
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
    # debug=True active le débogueur Werkzeug (exécution de code arbitraire
    # via son interface web) : à réserver au poste local, jamais à un
    # déploiement accessible publiquement (Render, etc.).
    debug = os.environ.get("FLASK_DEBUG") == "1"
    # threaded=True : sans ça, une requête bloquée sur un appel Google Sheets
    # lent gèle tout le serveur pour tous les utilisateurs (le client Sheets
    # utilise déjà un service par thread — cf. `_thread_local` dans
    # sheets_client.py — donc le mode threadé est sûr).
    app.run(host="0.0.0.0", port=port, debug=debug, threaded=True)
