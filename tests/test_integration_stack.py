"""Full-stack integration: REAL Eduvidual + WebUntis adapters (via `factories.build_deps`) -> real runner, engine, state,
rules and report -> FakeGoogle. Only the three outside worlds are fakes: the school HTTP servers (`FakeHttp` with the
synthetic fixtures of the adapter tests), Google (`FakeGoogle`), and time/sleep/browser/network (injected).

Why this exists: the unit and acceptance tests drive the runner with `FakeAdapter`s. These tests prove that the real adapters'
`FetchResult`/`AuthError`/`TransientError` shapes are understood by the runner and the engine, end to end.

HONESTY: the school responses are SYNTHETIC (mirroring documented Moodle/WebUntis structures, see the fixture READMEs).
Nothing here proves that the real servers answer like this; `schulsync check` on the user's PC does.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from schulsync import cli, factories, google_auth, runner
from schulsync.config import Config, EduvidualConfig, GoogleConfig, Paths, RulesConfig, WebUntisConfig, set_config_value
from schulsync.models import OverallStatus, RunMode, Source, SourceStatus
from schulsync.redact import clear_registered_secrets
from schulsync.secrets_store import MemorySecretStore
from schulsync.state import StateStore
from tests.support.fake_google import FakeGoogle
from tests.support.fake_http import FakeHttp, json_response
from tests.test_eduvidual import PASSWORD as EDU_PASSWORD, SECRETS as EDU_SECRETS, add_public_config, add_ws, jf
from tests.test_webuntis import BEARER, PW as WU_PASSWORD, SESSION_ID, NOW, World as WuWorld

EDU, WU = Source.EDUVIDUAL, Source.WEBUNTIS
SEP = chr(10)
ALL_SECRETS = [*EDU_SECRETS, WU_PASSWORD, SESSION_ID, BEARER]


class Stack:
    """One PC: the school servers, Google and the data directory persist; every `go()` is one program start."""

    def __init__(self, tmp_path: Path) -> None:
        self.paths = Paths(tmp_path / "SchulSync")
        self.paths.ensure()
        self.now: datetime = NOW
        self.mono = 0.0
        self.sleeps: list[float] = []
        self.opened: list[Path] = []
        self.texts: list[str] = []  # everything a user could see on a console
        self.wu = WuWorld()
        self.http: FakeHttp = self.wu.http
        add_ws(self.http)
        add_public_config(self.http)  # only `check` reads it
        self.google = FakeGoogle()
        self.cfg = Config(
            eduvidual=EduvidualConfig(username="max.mustermann", method="webservice"),
            webuntis=WebUntisConfig(username="beispiel.max"),
            google=GoogleConfig(calendar_id="cal-id", tasklist_id="list-id"),
            rules=RulesConfig(window_days=21, assignment_lead_days=3, assignment_calendar=True),
        )
        self.secrets = MemorySecretStore({factories.SECRET_EDUVIDUAL: EDU_PASSWORD, factories.SECRET_WEBUNTIS: WU_PASSWORD})

    def deps(self) -> runner.Deps:
        deps = factories.build_deps(self.paths, self.cfg, self.secrets, http_factory=lambda: self.http)
        return dataclasses.replace(
            deps,
            gateway_factory=lambda: self.google,  # the real gateway needs a Google login: FakeGoogle stands in
            clock=lambda: self.now,
            sleep=self._sleep,
            monotonic=lambda: self.mono,
            open_report=lambda p: self.opened.append(p) or True,
            network_ready=lambda: True,
        )

    def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.mono += seconds

    def go(self, mode: RunMode = RunMode.AUTO, *, force: bool = False) -> runner.RunOutcome:
        self.mono = 0.0
        out = runner.run(self.deps(), mode, force=force)
        self.texts.append(out.console_text)
        return out

    def state(self) -> StateStore:
        return StateStore(self.paths.db)

    def writes(self) -> int:
        return len(self.google.write_log)

    def requests(self) -> int:
        return len(self.http.requests)

    def everything_visible(self) -> str:
        """Console texts, all report files and all log files."""
        files = [*self.paths.reports.glob("*.html"), *self.paths.logs.glob("*.log")]
        return "\n".join([*self.texts, *(p.read_text(encoding="utf-8", errors="replace") for p in files)])


@pytest.fixture(autouse=True)
def _no_registered_secrets_leak_into_other_tests():
    clear_registered_secrets()
    yield
    clear_registered_secrets()


@pytest.fixture
def stack(tmp_path) -> Stack:
    return Stack(tmp_path)


# --------------------------------------------------------------------------- a normal day with both real adapters


def test_a_whole_day_with_both_real_adapters_creates_everything_once(stack):
    out = stack.go(RunMode.AUTO)
    rep = out.report
    assert out.exit_code == 0 and rep.status == OverallStatus.SUCCESS, out.console_text
    assert {s: r.status for s, r in rep.sources.items()} == {EDU: SourceStatus.SUCCESS, WU: SourceStatus.SUCCESS}
    assert stack.opened == []  # success: no browser page
    edu, wu = rep.sources[EDU], rep.sources[WU]
    # 5 assignments are in the window; "due today 00:00" (assign:4108) is already over at 08:00 and is not created
    assert (edu.calendar.new, edu.tasks.new) == (4, 4) and edu.calendar.skipped_reasons == {"expired": 1}
    assert edu.calendar.failed == edu.tasks.failed == 0
    # WebUntis, EXACTLY (a lower bound would also pass if entries were lost): 2 homework + 2 exams + 2 cancelled lessons as
    # events, 2 homework + 2 exams as tasks. Left out on purpose: homework due 04.11. (the window ends BEFORE that day), an
    # already expired one and one without a date; the exam of 10.11. (outside the window).
    assert (wu.calendar.new, wu.tasks.new) == (6, 4) and wu.calendar.failed == wu.tasks.failed == 0
    wu_events = {
        e["summary"] for e in stack.google.events.values() if e["extendedProperties"]["private"]["schulsync_source"] == "webuntis"
    }
    assert wu_events == {
        "Abgabe: Gedichtanalyse & Interpretation",
        "Abgabe: Übungsblatt 4,",
        "Entfall: Deutsch",
        "Entfall: Programmieren und Softwareentwicklung",
        "Schularbeit: 2. Schularbeit",
        "Test: NWP",
    }
    assert not any("Projektdokumentation abgeben" in e["summary"] or "Schon abgelaufen" in e["summary"] for e in stack.google.events.values())
    # the homework without a date is not an entry but a notice that names it (ANFORDERUNGEN 5: "Kein Datum: Eintrag ueberspringen
    # und im Bericht auffuehren"), proven here with the REAL adapter, runner, engine and report
    assert [n.title for n in rep.notices if n.code == "no_date" and n.source == WU]
    events, open_tasks = len(stack.google.events), len(stack.google.open_tasks)
    assert events == edu.calendar.new + wu.calendar.new and open_tasks == edu.tasks.new + wu.tasks.new
    assert all(e["summary"] for e in stack.google.events.values())
    assert stack.google.misrouted_writes() == []  # nothing ever lands in the primary calendar

    # the same data again (manual run, no day gate): no duplicate, no write at all
    before = stack.writes()
    again = stack.go(RunMode.MANUAL)
    assert again.report.status == OverallStatus.SUCCESS and stack.writes() == before
    assert len(stack.google.events) == events and len(stack.google.open_tasks) == open_tasks
    assert again.report.sources[EDU].calendar.new == 0 and again.report.sources[EDU].calendar.unchanged == 4

    # a second Windows logon the same day: ends silently and does not even ask the schools again
    asked = stack.requests()
    quiet = stack.go(RunMode.AUTO)
    assert quiet.silent and quiet.report is None and quiet.exit_code == 0 and stack.requests() == asked

    # the next morning: fetched again; known entries stay byte-identical, only entries that newly entered the moved
    # 21-day window are added (04.11. is now inside), and nothing is created twice
    snapshot = {k: dict(v) for k, v in stack.google.events.items()}
    stack.now += timedelta(days=1)
    nxt = stack.go(RunMode.AUTO)
    assert nxt.exit_code == 0 and not nxt.silent and stack.requests() > asked
    added = sum(r.calendar.new for r in nxt.report.sources.values())
    assert added == len(stack.google.events) - events > 0
    assert all(stack.google.events[k] == v for k, v in snapshot.items()) and stack.google.misrouted_writes() == []
    with stack.state() as st:
        assert st.pending_count() == 0 and st.get_source_day(stack.now.date(), EDU).done
    for secret in ALL_SECRETS:
        assert secret not in stack.everything_visible()


def test_preview_with_real_adapters_plans_everything_but_writes_and_books_nothing(stack):
    out = stack.go(RunMode.PREVIEW)
    assert out.report.mode == RunMode.PREVIEW and out.report_path == stack.paths.reports / "vorschau.html"
    assert stack.google.write_log == [] and stack.opened == []
    assert "Geplante Änderungen:" in out.console_text and "Anlegen (Kalender)" in out.console_text
    with stack.state() as st:
        assert st.all_items() == [] and st.last_run() is None and st.get_source_day(stack.now.date(), EDU) is None
    real = stack.go(RunMode.AUTO)  # and the real run afterwards still writes everything (nothing was suppressed)
    assert real.report.status == OverallStatus.SUCCESS and len(stack.google.events) > 0 and len(stack.google.open_tasks) > 0


# --------------------------------------------------------------------------- rejected credentials


def test_rejected_login_blocks_only_that_source_and_is_never_retried(stack):
    token_route = next(r for r in stack.http.routes if "login/token" in r.pattern.pattern)
    token_route.handler = json_response(jf("token_invalid_login.json"))
    out = stack.go(RunMode.AUTO)
    rep = out.report
    assert out.exit_code == 1 and rep.status == OverallStatus.PARTIAL
    assert rep.sources[EDU].status == SourceStatus.FAILED and rep.sources[WU].status == SourceStatus.SUCCESS
    assert len(stack.http.calls(r"/login/token\.php")) == 1 and stack.sleeps == []  # one attempt, no waiting, no retry
    assert [e.code for e in rep.sources[EDU].errors] == ["auth"]
    assert len(stack.opened) == 1  # a problem: the report opens once
    assert len(stack.google.events) > 0  # the other source was written
    with stack.state() as st:
        assert st.is_auth_blocked(EDU) and not st.is_auth_blocked(WU)
        assert st.all_items() and all(i.item.source == WU for i in st.all_items())  # EDU never produced anything

    # next logon the same day: the blocked source is skipped without any login attempt, the report is not opened again
    n_tokens = len(stack.http.calls(r"/login/token\.php"))
    again = stack.go(RunMode.AUTO)
    assert len(stack.http.calls(r"/login/token\.php")) == n_tokens and again.exit_code == 1
    assert "überspringen" in again.console_text and len(stack.opened) == 1

    # the user fixes the password; a manual run tries again, succeeds, and lifts the block
    token_route.handler = json_response(jf("token_ok.json"))
    wu_events = len(stack.google.events)
    fixed = stack.go(RunMode.MANUAL)
    assert fixed.report.status == OverallStatus.SUCCESS and fixed.exit_code == 0
    assert fixed.report.sources[EDU].calendar.new == 4 and len(stack.google.events) == wu_events + 4  # WebUntis not duplicated
    with stack.state() as st:
        assert not st.is_auth_blocked(EDU)
    for secret in ALL_SECRETS:
        assert secret not in stack.everything_visible()


# --------------------------------------------------------------------------- outage: never a cancellation, always a recovery


def test_network_outage_cancels_and_deletes_nothing_and_the_next_start_recovers(stack):
    assert stack.go(RunMode.AUTO).exit_code == 0
    snapshot = {k: dict(v) for k, v in stack.google.events.items()}
    tasks_before = [(t["id"], t["title"], t.get("status")) for t in stack.google.tasks]
    writes = stack.writes()

    stack.now += timedelta(days=1)
    stack.http.fail_network = True
    down = stack.go(RunMode.AUTO)
    assert down.exit_code == 1 and down.report.status == OverallStatus.FAILED
    assert all(r.status == SourceStatus.FAILED and r.stale for r in down.report.sources.values())
    assert stack.sleeps == [15.0, 60.0, 180.0, 15.0, 60.0, 180.0]  # 4 attempts per source with growing waits, injected sleeps
    assert stack.writes() == writes  # no cancellation, no deletion, no write at all after a failed fetch
    assert {k: dict(v) for k, v in stack.google.events.items()} == snapshot
    assert [(t["id"], t["title"], t.get("status")) for t in stack.google.tasks] == tasks_before
    with stack.state() as st:
        assert not st.is_auth_blocked(EDU) and not st.is_auth_blocked(WU)  # an outage is not a rejected login
        today = st.get_source_day(stack.now.date(), EDU)
        assert today is None or not today.done  # a failed fetch is no day success
    assert len(stack.opened) == 1

    stack.http.fail_network = False
    stack.sleeps.clear()
    up = stack.go(RunMode.AUTO)  # the next logon the same day
    assert up.exit_code == 0 and up.report.status == OverallStatus.SUCCESS and stack.sleeps == []
    assert all(stack.google.events[k] == v for k, v in snapshot.items())  # every known entry is untouched
    assert len(stack.google.events) == len(snapshot) + sum(r.calendar.new for r in up.report.sources.values())  # no duplicates
    for secret in ALL_SECRETS:
        assert secret not in stack.everything_visible()


# --------------------------------------------------------------------------- `schulsync check` with the real adapters


def _check_env(stack: Stack, lines: list[str]) -> cli.CliEnv:
    """The real CLI against the stack: configuration from a real config.toml, the real factories, fake outside worlds."""
    cli._write_default_config(stack.paths.config)
    for section, key, value in (
        ("eduvidual", "username", "max.mustermann"),
        ("eduvidual", "method", "webservice"),
        ("webuntis", "username", "beispiel.max"),
        ("google", "calendar_id", "cal-id"),
        ("google", "tasklist_id", "list-id"),
    ):
        set_config_value(stack.paths.config, section, key, value)
    secrets = MemorySecretStore(
        {
            factories.SECRET_EDUVIDUAL: EDU_PASSWORD,
            factories.SECRET_WEBUNTIS: WU_PASSWORD,
            google_auth.SECRET_TOKEN: '{"refresh_token": "dummy"}',
        }
    )

    def build(paths, cfg, sec):
        deps = factories.build_deps(paths, cfg, sec, http_factory=lambda: stack.http)
        return dataclasses.replace(deps, gateway_factory=lambda: stack.google)

    return cli.CliEnv(say=lines.append, paths=stack.paths, secrets=secrets, clock=lambda: stack.now, build_deps=build)


def test_check_with_real_adapters_lists_every_category_and_lifts_an_auth_block(stack):
    lines: list[str] = []
    env = _check_env(stack, lines)
    with stack.state() as st:
        st.set_auth_blocked(EDU, stack.now, "auth")
    assert cli.main(["check"], env=env) == 0, SEP.join(lines)
    text = SEP.join(lines)
    for expected in (
        "Eduvidual:", "WebUntis:", "Aufgaben und Abgaben: OK", "Hausaufgaben: OK", "Prüfungen und Schularbeiten: OK",
        "Unterrichtsentfälle: OK", "Kalender 'Schule' ist erreichbar", "Ergebnis: Alles in Ordnung",
        "Die Anmeldung bei Eduvidual funktioniert wieder; die Sperre automatischer Läufe wurde aufgehoben.",
    ):
        assert expected in text, text
    with stack.state() as st:
        assert not st.is_auth_blocked(EDU)
    assert stack.google.write_log == [] and not list(stack.paths.reports.glob("*.html"))  # check writes nothing, no report
    for secret in ALL_SECRETS:
        assert secret not in text


def test_check_with_a_rejected_password_fails_clearly_and_keeps_the_block(stack):
    next(r for r in stack.http.routes if "login/token" in r.pattern.pattern).handler = json_response(jf("token_invalid_login.json"))
    lines: list[str] = []
    env = _check_env(stack, lines)
    with stack.state() as st:
        st.set_auth_blocked(EDU, stack.now, "auth")
    assert cli.main(["check"], env=env) == 1
    text = SEP.join(lines)
    assert "[FEHLER] Anmeldung" in text and "Ergebnis: Es gibt Probleme" in text and "schulsync setup --eduvidual" in text
    assert "WebUntis:" in text and "Hausaufgaben: OK" in text  # the other source is still checked and fine
    assert len(stack.http.calls(r"/login/token\.php")) == 1  # one attempt only (no account lockout by retrying)
    with stack.state() as st:
        assert st.is_auth_blocked(EDU)
    for secret in ALL_SECRETS:
        assert secret not in text


# --------------------------------------------------------------------------- the Eduvidual WEB route end to end


def test_the_web_route_runs_through_runner_and_engine_and_follows_a_deadline_moved_far_out(stack):
    """Until the audit the stack used only method="webservice": the web route (the fallback) was never run through runner,
    engine, state and report. Day 1 creates the entries, day 2 follows an assignment whose deadline moved MONTHS out of the
    window (ANFORDERUNGEN 2: known open entries are followed outside the window) and the run says what the route cannot see."""
    from tests.test_eduvidual import add_web
    from schulsync.adapters import eduvidual as ev

    stack.cfg = dataclasses.replace(stack.cfg, eduvidual=dataclasses.replace(stack.cfg.eduvidual, method="web"))
    events = jf("calendar_due_events.json")
    add_web(stack.http, events=events)

    first = stack.go(RunMode.AUTO)
    assert first.exit_code == 0 and first.report.status == OverallStatus.SUCCESS, first.console_text
    edu = first.report.sources[EDU]
    assert (edu.calendar.new, edu.tasks.new) == (4, 4) and edu.calendar.failed == edu.tasks.failed == 0
    codes = [n.code for n in first.report.notices]
    assert "no_date_unseen" in codes  # said in every run: assignments without a deadline are invisible on this route
    assert not any(r.method == "POST" and "webservice/rest" in r.url for r in stack.http.requests)  # only the web route was used
    uid = "eduvidual:assignment:assign:4101"
    (event,) = [e for e in stack.google.events.values() if e["extendedProperties"]["private"]["schulsync_uid"] == uid]
    assert event["start"]["dateTime"].startswith("2026-10-16")

    for e in events:  # the school moves the deadline of 4101 to the middle of March
        if e["instance"] == 4101:
            e["timestart"] = int(datetime(2027, 3, 15, 9, 0, tzinfo=stack.now.tzinfo).timestamp())
    stack.now += timedelta(days=1)
    before = len(stack.http.requests)
    second = stack.go(RunMode.AUTO)
    month_calls = [r for r in stack.http.requests[before:] if isinstance(r.json, list) and r.json and r.json[0].get("methodname") == ev.FN_MONTH]
    assert len(month_calls) == ev.WEB_MAX_MONTHS  # tracked entries: the whole documented range is read
    assert second.exit_code == 0
    (moved,) = [e for e in stack.google.events.values() if e["extendedProperties"]["private"]["schulsync_uid"] == uid]
    assert moved["start"]["dateTime"].startswith("2027-03-15") and len(stack.google.events) >= 4
    task = [t for t in stack.google.open_tasks if f"schulsync-id: {uid}" in t["notes"]]
    assert len(task) == 1 and task[0]["due"].startswith("2027-03-12")  # three calendar days before the new deadline
    for secret in ALL_SECRETS:
        assert secret not in stack.everything_visible()


def test_scheduled_18_oclock_run_follows_moved_homework_and_exam_and_a_withdrawn_cancellation_without_duplicates(stack):
    """Real WebUntis adapter through runner and engine: the school moves a homework to next Monday, an exam two days later,
    and a cancelled lesson takes place after all. The 18:00 run of the same day must update the SAME events/tasks, mark the
    cancellation 'Abgesagt:' and create nothing twice."""
    first = stack.go(RunMode.AUTO)
    assert first.report.status == OverallStatus.SUCCESS, first.console_text
    n_events, n_tasks = len(stack.google.events), len(stack.google.open_tasks)

    def event(prefix: str) -> dict:
        (e,) = [e for e in stack.google.events.values() if e["summary"].endswith(prefix)]
        return e

    def task(title: str) -> dict:
        (t,) = [t for t in stack.google.open_tasks if t["title"].endswith(title)]
        return t

    hw_id, exam_id = event("Übungsblatt 4,")["id"], event("2. Schularbeit")["id"]
    for h in stack.wu.homeworks["data"]["homeworks"]:
        if h["id"] == 9001:
            h["dueDate"] = 20261019  # Friday -> Monday next week
    for x in stack.wu.exams["data"]["exams"]:
        if x["id"] == 5501:
            x["examDate"], x["startTime"], x["endTime"] = 20261023, 1000, 1150
    for l in stack.wu.lessons:
        if l["id"] == 701005:
            l["code"] = None  # the cancelled lesson takes place after all

    stack.now = stack.now.replace(hour=18, minute=0)  # the fixed 18:00 run of the same day
    second = stack.go(RunMode.AUTO)
    assert not second.silent and second.exit_code == 0, second.console_text

    assert len(stack.google.events) == n_events and stack.google.misrouted_writes() == []  # nothing duplicated
    hw = stack.google.events[hw_id]
    assert hw["start"] == {"date": "2026-10-19"} and hw["end"] == {"date": "2026-10-20"}
    exam = stack.google.events[exam_id]
    assert exam["start"]["dateTime"].startswith("2026-10-23T10:00") and exam["end"]["dateTime"].startswith("2026-10-23T11:50")
    assert task("Übungsblatt 4,")["due"].startswith("2026-10-16")  # 3 calendar days before Monday 19.10.
    assert task("2. Schularbeit")["due"].startswith("2026-10-16")  # 7 calendar days before 23.10.
    assert len(stack.google.open_tasks) == n_tasks
    entfall = event("Programmieren und Softwareentwicklung")
    assert entfall["summary"].startswith("Abgesagt:") and entfall["reminders"]["overrides"] == []
