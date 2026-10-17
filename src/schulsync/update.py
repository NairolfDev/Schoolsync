"""`schulsync update`: prüft die neueste GitHub-Release und ersetzt die installierte schulsync.exe.

Sicherheit: nur HTTPS zu github.com bzw. den GitHub-Download-Hosts; die Datei wird erst ersetzt, wenn ihre SHA-256-Prüfsumme zur
veröffentlichten Datei `<Name>.sha256` derselben Release passt. Es wird nichts ausgeführt, nur ersetzt; die neue Version startet
beim nächsten Aufruf. Die Quelle ist die Release des Repositories (eigener Fork: `SCHULSYNC_REPO=besitzer/repo`).
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

import requests

from . import __version__, frozen
from .errors import NetworkError, SchemaError, SchulSyncError

DEFAULT_REPO = "NairolfDev/Schoolsync"
ASSET = "SchulSync-Setup.exe"
_TIMEOUT = (10, 120)


@dataclass(frozen=True)
class Release:
    version: str
    exe_url: str
    sha_url: str


def repo() -> str:
    r = os.environ.get("SCHULSYNC_REPO", DEFAULT_REPO)
    return r if re.fullmatch(r"[\w.-]+/[\w.-]+", r) else DEFAULT_REPO


def parse_version(text: str) -> tuple[int, ...]:
    m = re.match(r"v?(\d+(?:\.\d+)*)", text.strip())
    if not m:
        raise SchemaError(f"Unbekannte Versionsnummer: {text[:30]!r}")
    return tuple(int(p) for p in m.group(1).split("."))


def _https_github(url: str) -> str:
    host = urlsplit(url).hostname or ""
    if urlsplit(url).scheme != "https" or not (host == "github.com" or host.endswith(".githubusercontent.com")):
        raise SchemaError("Die Release enthält eine unerwartete Download-Adresse.", detail=host)
    return url


def latest_release(get: Callable[..., requests.Response] = requests.get) -> Release:
    try:
        resp = get(f"https://api.github.com/repos/{repo()}/releases/latest", timeout=_TIMEOUT,
                   headers={"Accept": "application/vnd.github+json", "User-Agent": "SchulSync-Update"})
    except requests.RequestException as exc:
        raise NetworkError("GitHub ist nicht erreichbar (Internet prüfen).", detail=type(exc).__name__) from exc
    if resp.status_code == 404:
        raise SchulSyncError("Es gibt noch keine veröffentlichte Version.", hint="Das Repository muss öffentlich sein und eine Release enthalten.", detail="HTTP 404")
    if resp.status_code != 200:
        raise NetworkError(f"GitHub antwortet mit Fehler {resp.status_code}.", hint="Später erneut versuchen.")
    try:
        data = resp.json()
        assets = {a["name"]: a["browser_download_url"] for a in data["assets"]}
        return Release(str(data["tag_name"]), _https_github(assets[ASSET]), _https_github(assets[ASSET + ".sha256"]))
    except (ValueError, KeyError, TypeError) as exc:
        raise SchemaError(f"Die neueste Release enthält {ASSET} oder die Prüfsumme nicht.", detail=type(exc).__name__) from exc


def _download(url: str, get: Callable[..., requests.Response]) -> bytes:
    try:
        resp = get(_https_github(url), timeout=_TIMEOUT, headers={"User-Agent": "SchulSync-Update"})
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise NetworkError("Der Download ist fehlgeschlagen.", hint="Internet prüfen und erneut versuchen.", detail=type(exc).__name__) from exc
    return resp.content


def replace_exe(current: Path, new_bytes: bytes) -> None:
    """Neue Version neben die laufende schreiben und austauschen. Eine laufende .exe lässt sich umbenennen, nicht überschreiben."""
    new = current.with_name(current.name + ".new")
    old = current.with_name(current.name + frozen.OLD_SUFFIX)
    new.write_bytes(new_bytes)
    try:
        old.unlink(missing_ok=True)
        current.replace(old)
        new.replace(current)
    except OSError:
        if not current.exists() and old.exists():  # halb fertig: alte Version zurück
            old.replace(current)
        new.unlink(missing_ok=True)
        raise


def run_update(say: Callable[[str], None], *, check_only: bool = False, get: Callable[..., requests.Response] = requests.get,
               current: Path | None = None, is_frozen: bool | None = None) -> int:
    """Exit-Code: 0 aktuell/aktualisiert (bei --check: 0 aktuell, 10 neue Version vorhanden), 1 Quellcode-Version."""
    rel = latest_release(get)
    newer = parse_version(rel.version) > parse_version(__version__)
    say(f"Installiert: {__version__}   Neueste Version: {rel.version.lstrip('v')}")
    if not newer:
        say("SchulSync ist auf dem neuesten Stand.")
        return 0
    if check_only:
        say("Eine neue Version ist verfügbar. Mit 'schulsync update' installieren.")
        return 10
    if not (frozen.is_frozen() if is_frozen is None else is_frozen):
        say("Du nutzt die Quellcode-Version: aktualisiere sie mit 'git pull' und 'pip install -e .'.")
        return 1
    data = _download(rel.exe_url, get)
    expected = _download(rel.sha_url, get).decode("ascii", "replace").split()[0].lower()
    if hashlib.sha256(data).hexdigest() != expected:
        raise SchulSyncError("Die heruntergeladene Datei passt nicht zur Prüfsumme und wird nicht installiert.", hint="Später erneut versuchen.")
    replace_exe(current or frozen.exe_path(), data)
    say(f"SchulSync wurde auf {rel.version.lstrip('v')} aktualisiert. Die neue Version gilt ab dem nächsten Start; deine Einstellungen und Daten bleiben erhalten.")
    return 0
