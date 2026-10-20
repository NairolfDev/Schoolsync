"""Verhalten der fertigen schulsync.exe (PyInstaller): Installation, Konsole, Aufräumen nach einem Update.

Die .exe ist ein Konsolenprogramm. Beim Start über die Aufgabenplanung (`run --auto`) schließt sie ihr Konsolenfenster sofort
(`FreeConsole`); ein kurzes Aufblitzen lässt sich unter Windows nicht ganz vermeiden. Per Doppelklick (kein Befehl) startet sie die
Installation und wartet am Ende auf Enter, damit das Fenster nicht sofort verschwindet.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import sys
from pathlib import Path
from typing import Callable

EXE_NAME = "schulsync.exe"
OLD_SUFFIX = ".old"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def install_dir() -> Path:
    """%LOCALAPPDATA%\\Programs\\SchulSync (ohne Adminrechte beschreibbar)."""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Programs" / "SchulSync"


def resource(name: str) -> Path | None:
    """Mitgelieferte Datei (z. B. config.example.toml): im PyInstaller-Paket, sonst im Projektordner."""
    base = Path(getattr(sys, "_MEIPASS", "")) if is_frozen() else Path(__file__).resolve().parents[2]
    p = base / name
    return p if p.is_file() else None


def exe_path() -> Path:
    return Path(sys.executable).resolve()


def remove_old_exe() -> None:
    """Nach einem Update bleibt die vorherige Version als *.old liegen (eine laufende .exe kann man umbenennen, nicht löschen)."""
    try:
        exe_path().with_name(exe_path().name + OLD_SUFFIX).unlink(missing_ok=True)
    except OSError:
        pass  # wird beim nächsten Start erneut versucht


def hide_console() -> None:
    """Konsolenfenster schließen (Autostart). Danach gibt es keine Standardausgabe mehr; die CLI kommt damit klar."""
    try:
        ctypes.windll.kernel32.FreeConsole()
    except (AttributeError, OSError):
        return
    sys.stdout = sys.stderr = sys.stdin = None  # type: ignore[assignment]


def own_console() -> bool:
    """True, wenn dieses Programm das einzige am Konsolenfenster hängt (Doppelklick): Fenster würde sofort schließen."""
    try:
        buf = (ctypes.c_uint * 4)()
        return ctypes.windll.kernel32.GetConsoleProcessList(buf, 4) == 1
    except (AttributeError, OSError):
        return False


def add_to_user_path(folder: Path) -> bool:
    """Ordner in den Benutzer-PATH (HKCU\\Environment) eintragen; True, wenn neu. Keine Adminrechte nötig."""
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE) as key:
        try:
            current, kind = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            current, kind = "", winreg.REG_EXPAND_SZ
        parts = [p for p in str(current).split(";") if p]
        if any(os.path.normcase(p.rstrip("\\")) == os.path.normcase(str(folder)) for p in parts):
            return False
        winreg.SetValueEx(key, "Path", 0, kind, ";".join([*parts, str(folder)]))
    try:  # laufende Programme (Explorer) über die Änderung informieren
        ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x1A, 0, "Environment", 2, 2000, None)
    except (AttributeError, OSError):
        pass
    return True


def install(say: Callable[[str], None]) -> Path:
    """Kopiert diese .exe nach %LOCALAPPDATA%\\Programs\\SchulSync\\schulsync.exe und trägt den Ordner in den PATH ein."""
    target_dir = install_dir()
    target = target_dir / EXE_NAME
    target_dir.mkdir(parents=True, exist_ok=True)
    if exe_path() != target.resolve():
        try:
            shutil.copy2(exe_path(), target)
        except OSError:  # alte Version läuft gerade: umbenennen geht, überschreiben nicht
            target.replace(target.with_name(target.name + OLD_SUFFIX))
            shutil.copy2(exe_path(), target)
    say(f"SchulSync wurde nach {target_dir} installiert.")
    if add_to_user_path(target_dir):
        say("Der Ordner steht jetzt im PATH. Öffne ein NEUES PowerShell-Fenster, dann funktioniert der Befehl 'schulsync' überall.")
    return target


def remove_from_user_path(folder: Path) -> bool:
    """Ordner aus dem Benutzer-PATH entfernen; True, wenn er enthalten war."""
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE) as key:
        try:
            current, kind = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            return False
        parts = [p for p in str(current).split(";") if p]
        keep = [p for p in parts if os.path.normcase(p.rstrip("\\")) != os.path.normcase(str(folder))]
        if len(keep) == len(parts):
            return False
        winreg.SetValueEx(key, "Path", 0, kind, ";".join(keep))
    return True


def _delete_later(folders: list[Path]) -> None:
    """Löscht Ordner nach dem Beenden dieses Programms (die laufende .exe kann sich nicht selbst löschen)."""
    import subprocess

    for f in folders:
        if f.name.lower() not in ("schulsync",):  # nie einen fremden Ordner löschen
            raise ValueError(f"Unerwarteter Ordner: {f}")
    cmd = " & ".join(f'rmdir /s /q "{f}"' for f in folders)
    subprocess.Popen(["cmd.exe", "/c", f"ping -n 3 127.0.0.1 >nul & {cmd}"], creationflags=0x08000000 | 0x00000008, close_fds=True)


def uninstall(say: Callable[[str], None], *, data_dir: Path | None = None) -> None:
    """PATH-Eintrag entfernen und den Programmordner (auf Wunsch auch den Datenordner) nach dem Beenden löschen."""
    target = install_dir()
    if remove_from_user_path(target):
        say("Der Eintrag im PATH wurde entfernt.")
    folders = [target] + ([data_dir] if data_dir else [])
    _delete_later(folders)
    say("Gelöscht wird nach dem Beenden dieses Fensters: " + ", ".join(str(f) for f in folders))
    if data_dir is None:
        say("Deine Einstellungen, Passwörter und Daten bleiben erhalten (deinstallieren mit Daten: 'schulsync uninstall --data').")


def ensure_path() -> None:
    """Läuft die .exe aus dem Programmordner, sorgt sie dafür, dass der Ordner im Benutzer-PATH steht (still, ohne Adminrechte)."""
    try:
        if is_frozen() and exe_path().parent == install_dir().resolve():
            add_to_user_path(install_dir())
    except OSError:
        pass
