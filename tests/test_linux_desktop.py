import subprocess

from schulsync import google_auth, notify


def test_linux_notification_uses_notify_send_without_shell(monkeypatch):
    monkeypatch.undo()  # restore the real implementation patched by the suite-wide fixture
    calls = []
    monkeypatch.setattr(notify.sys, "platform", "linux")
    monkeypatch.setattr(notify.shutil, "which", lambda name: "/usr/bin/notify-send")
    monkeypatch.setattr(notify.subprocess, "run", lambda args, **kw: calls.append((args, kw)) or subprocess.CompletedProcess(args, 0))
    assert notify.toast("Titel", "Text")
    assert calls[0][0] == ["/usr/bin/notify-send", "Titel", "Text"]
    assert not calls[0][1].get("shell", False)


def test_linux_chrome_is_found_on_path(monkeypatch):
    monkeypatch.setattr(google_auth.sys, "platform", "linux")
    monkeypatch.setattr(google_auth.shutil, "which", lambda name: "/usr/bin/google-chrome" if name == "google-chrome" else None)
    assert google_auth._chrome_executable() == "/usr/bin/google-chrome"
