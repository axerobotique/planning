"""Client Google Sheets minimal — lecture seule de la feuille "PLANNING quotidien".

Auth : service account (le même que celui déjà utilisé par le MCP gsheets /
CLAUDE PROSPECTION), résolu dans cet ordre :
  1. variable d'env GOOGLE_APPLICATION_CREDENTIALS
  2. ~/.claude/google-credentials.json
Aucune copie de clé n'est nécessaire : ce fichier est déjà présent sur les
postes qui utilisent Claude Code / le MCP gsheets.
"""

from __future__ import annotations

import os
import threading
import time
from threading import Lock
from typing import Any

import httplib2
from google.oauth2 import service_account
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build

SPREADSHEET_ID = "1hyQc7zrQfEMFycw-BKCUwXKAV52V1RZ0VIO72h5BicM"
SHEET_NAME = "PLANNING quotidien"

# httplib2 n'a pas de timeout par défaut : un problème réseau/API Google peut
# donc bloquer l'appel indéfiniment. Avec le serveur Flask en dev (mono-thread
# tant que threaded=True n'est pas passé à app.run), une seule requête
# suspendue gèle alors TOUTE l'application pour tout le monde — c'est ce qui
# s'est produit en pratique (plus aucune route ne répondait). Un timeout
# transforme ce blocage indéfini en simple erreur 500 après quelques secondes.
REQUEST_TIMEOUT_SECONDS = 30

_CRED_CANDIDATES = [
    os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"),
    os.path.join(os.path.expanduser("~"), ".claude", "google-credentials.json"),
]
CREDENTIALS_PATH = next((p for p in _CRED_CANDIDATES if p and os.path.exists(p)), _CRED_CANDIDATES[-1])

# Lecture/écriture : le compte de service doit être partagé en "Éditeur" (pas
# juste "Lecteur") sur la Google Sheet elle-même, sans quoi les écritures
# échouent en 403 malgré ce scope.
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

CACHE_TTL_SECONDS = 30

_cache: dict[str, tuple[float, Any]] = {}
_cache_lock = Lock()
_thread_local = threading.local()
_sheet_meta_cache: dict[str, Any] = {}


def _get_service():
    svc = getattr(_thread_local, "service", None)
    if svc is None:
        if not os.path.exists(CREDENTIALS_PATH):
            raise FileNotFoundError(
                f"Identifiants Google introuvables ({CREDENTIALS_PATH}). "
                "Définis GOOGLE_APPLICATION_CREDENTIALS ou place le fichier "
                "service account dans ~/.claude/google-credentials.json."
            )
        creds = service_account.Credentials.from_service_account_file(CREDENTIALS_PATH, scopes=SCOPES)
        authed_http = AuthorizedHttp(creds, http=httplib2.Http(timeout=REQUEST_TIMEOUT_SECONDS))
        svc = build("sheets", "v4", http=authed_http, cache_discovery=False)
        _thread_local.service = svc
    return svc


def _cached(key: str, loader):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_TTL_SECONDS:
            return hit[1]
    value = loader()
    with _cache_lock:
        _cache[key] = (now, value)
    return value


def get_range(range_a1: str, sheet: str | None = None) -> list[list[str]]:
    """Lit une plage A1 d'un onglet (PLANNING quotidien par défaut). Renvoie une matrice (ragged)."""
    sheet = sheet or SHEET_NAME
    key = f"values::{sheet}::{range_a1}"

    def loader():
        resp = (
            _get_service()
            .spreadsheets()
            .values()
            .get(
                spreadsheetId=SPREADSHEET_ID,
                range=f"'{sheet}'!{range_a1}",
                valueRenderOption="FORMATTED_VALUE",
            )
            .execute()
        )
        return resp.get("values", [])

    return _cached(key, loader)


def _invalidate_cache() -> None:
    with _cache_lock:
        _cache.clear()


def update_range(range_a1: str, values: list[list[str]], sheet: str | None = None) -> None:
    """Écrit une matrice de valeurs sur une plage A1. Texte brut (pas de formules)."""
    sheet = sheet or SHEET_NAME
    _get_service().spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{sheet}'!{range_a1}",
        valueInputOption="RAW",
        body={"values": values},
    ).execute()
    _invalidate_cache()


def append_row(values: list[str], sheet: str) -> None:
    """Ajoute une ligne à la suite des données existantes d'un onglet (col A:len(values))."""
    last_col = col_letter(len(values))
    _get_service().spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{sheet}'!A1:{last_col}1",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": [values]},
    ).execute()
    _invalidate_cache()


def clear_ranges(ranges_a1: list[str], sheet: str | None = None) -> None:
    """Vide une liste de plages/cellules A1 en un seul appel."""
    if not ranges_a1:
        return
    sheet = sheet or SHEET_NAME
    _get_service().spreadsheets().values().batchClear(
        spreadsheetId=SPREADSHEET_ID,
        body={"ranges": [f"'{sheet}'!{r}" for r in ranges_a1]},
    ).execute()
    _invalidate_cache()


def col_letter(n: int) -> str:
    """Numéro de colonne (1-based) -> lettre(s) A1."""
    letters = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _all_sheets_meta() -> dict[str, dict]:
    if "all" not in _sheet_meta_cache:
        meta = (
            _get_service()
            .spreadsheets()
            .get(spreadsheetId=SPREADSHEET_ID, fields="sheets.properties")
            .execute()
        )
        _sheet_meta_cache["all"] = {
            s["properties"]["title"]: s["properties"] for s in meta.get("sheets", [])
        }
    return _sheet_meta_cache["all"]


def _sheet_properties(sheet: str | None = None) -> dict:
    sheet = sheet or SHEET_NAME
    all_meta = _all_sheets_meta()
    if sheet not in all_meta:
        raise RuntimeError(f"Onglet '{sheet}' introuvable dans le classeur.")
    return all_meta[sheet]


def get_sheet_id(sheet: str | None = None) -> int:
    return _sheet_properties(sheet)["sheetId"]


def get_grid_size(sheet: str | None = None) -> tuple[int, int]:
    """Renvoie (row_count, column_count) de l'onglet demandé (PLANNING quotidien par défaut)."""
    grid = _sheet_properties(sheet)["gridProperties"]
    return grid["rowCount"], grid["columnCount"]


def ensure_sheet(title: str, header: list[str]) -> None:
    """Crée l'onglet `title` avec la ligne d'en-tête donnée s'il n'existe pas déjà."""
    if title in _all_sheets_meta():
        return
    _get_service().spreadsheets().batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={"requests": [{"addSheet": {"properties": {"title": title}}}]},
    ).execute()
    last_col = col_letter(len(header))
    _get_service().spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{title}'!A1:{last_col}1",
        valueInputOption="RAW",
        body={"values": [header]},
    ).execute()
    _sheet_meta_cache.pop("all", None)
    _invalidate_cache()


def insert_row_before(sheet_row_1based: int, sheet: str | None = None) -> None:
    """Insère une ligne vierge juste avant la ligne 1-based donnée (les lignes
    suivantes, y compris celle-ci, sont décalées d'une position vers le bas)."""
    _get_service().spreadsheets().batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={
            "requests": [
                {
                    "insertDimension": {
                        "range": {
                            "sheetId": get_sheet_id(sheet),
                            "dimension": "ROWS",
                            "startIndex": sheet_row_1based - 1,
                            "endIndex": sheet_row_1based,
                        },
                        "inheritFromBefore": False,
                    }
                }
            ]
        },
    ).execute()
    _sheet_meta_cache.pop("all", None)  # rowCount a changé
    _invalidate_cache()
