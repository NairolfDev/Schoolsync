"""Terminal key reading preserves the shell state even on interruption."""
from types import SimpleNamespace

import pytest

from schulsync import settings


@pytest.mark.parametrize("payload, expected", [
    (b"\x1b[A", "up"), (b"\x1b[B", "down"), (b" ", "space"),
    (b"\r", "enter"), (b"\n", "enter"), (b"\x1b", "escape"), (b"\x03", "interrupt"),
])
def test_posix_key_reader_translates_keys_and_restores_terminal(monkeypatch, payload, expected):
    calls = []
    termios = SimpleNamespace(tcgetattr=lambda fd: ["original"], TCSADRAIN=1,
                              tcsetattr=lambda fd, when, value: calls.append((fd, when, value)))
    tty = SimpleNamespace(setraw=lambda fd: calls.append(("raw", fd)))
    data = iter(payload)
    monkeypatch.setitem(__import__("sys").modules, "termios", termios)
    monkeypatch.setitem(__import__("sys").modules, "tty", tty)
    monkeypatch.setattr(settings.os, "read", lambda fd, size: bytes([next(data)]))
    monkeypatch.setattr("select.select", lambda *args: ([7], [], []) if expected in ("up", "down") else ([], [], []))
    reader = settings._posix_keys(SimpleNamespace(fileno=lambda: 7))
    assert reader() == expected
    assert calls == [("raw", 7), (7, 1, ["original"])]


def test_posix_key_reader_restores_terminal_on_read_failure(monkeypatch):
    calls = []
    termios = SimpleNamespace(tcgetattr=lambda fd: ["original"], TCSADRAIN=1,
                              tcsetattr=lambda fd, when, value: calls.append(value))
    monkeypatch.setitem(__import__("sys").modules, "termios", termios)
    monkeypatch.setitem(__import__("sys").modules, "tty", SimpleNamespace(setraw=lambda fd: None))
    monkeypatch.setattr(settings.os, "read", lambda *args: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        settings._posix_keys(SimpleNamespace(fileno=lambda: 7))()
    assert calls == [["original"]]


def test_linux_tty_gets_key_reader_but_piped_input_uses_text_fallback(monkeypatch):
    termios = SimpleNamespace(tcgetattr=lambda fd: ["original"])
    monkeypatch.setitem(__import__("sys").modules, "termios", termios)
    monkeypatch.setattr(settings, "os", SimpleNamespace(name="posix"))
    terminal = SimpleNamespace(isatty=lambda: True, fileno=lambda: 7)
    monkeypatch.setattr(settings, "sys", SimpleNamespace(stdin=terminal, stdout=terminal))
    sentinel = lambda: "up"
    monkeypatch.setattr(settings, "_posix_keys", lambda stream: sentinel)
    assert settings._keys() is sentinel
    settings.sys.stdin = SimpleNamespace(isatty=lambda: False)
    assert settings._keys() is None
