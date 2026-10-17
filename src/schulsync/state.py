"""SQLite state store (DESIGN section 5): items, write intents, per-day source status, runs, report bookkeeping.

Design notes
------------
* Stdlib ``sqlite3`` only. The connection is in autocommit mode (``isolation_level=None``); every multi-statement
  update runs inside :meth:`StateStore.transaction` (``BEGIN IMMEDIATE`` ... ``COMMIT``/``ROLLBACK``).
  ``transaction()`` is re-entrant: all write methods may be called inside an outer ``transaction()`` (the sync engine
  uses this to commit a Google result and ``resolve_intent`` atomically) and commit on their own otherwise.
* PRAGMAs: ``journal_mode=WAL`` (file databases), ``synchronous=FULL`` (an intent is on disk when
  ``open_intent`` returns), ``foreign_keys=ON``, short ``busy_timeout``. In WAL mode readers never block;
  a second *writer* waits at most ``busy_timeout_ms`` and then gets a :class:`TransientError` (German message)
  instead of hanging.
* Schema versioning via ``PRAGMA user_version``; migrations are append-only entries of ``_MIGRATIONS``.
* All timestamps are stored as ISO-8601 with UTC offset (never naive). Because offsets change with DST, ISO strings
  are NOT compared lexicographically for instants; instants are compared after parsing. Calendar days
  (``source_days.day``, ``runs.day``) are plain ``YYYY-MM-DD`` and do sort lexicographically.
  Datetimes are returned in ``Europe/Vienna`` (same instant as stored; ``fold`` is preserved through the offset).
* Not thread-safe: use one store (connection) per thread.
* A corrupt or non-database file raises :class:`ConfigError` (German message + hint); the file is never touched.

Verified against real SQLite only (stdlib ``sqlite3``); there is no external service involved.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterator

from .errors import ConfigError, TransientError
from .models import (
    EventState,
    ItemState,
    Kind,
    OverallStatus,
    RunMode,
    Source,
    SourceItem,
    Target,
    TaskState,
    canonical_json,
)
from .timeutil import now_vienna, to_vienna

# --------------------------------------------------------------------------- schema

_V1 = (
    """CREATE TABLE items (
        uid TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        kind TEXT NOT NULL,
        source_id TEXT NOT NULL,
        item_json TEXT NOT NULL,
        item_hash TEXT NOT NULL,
        cancelled INTEGER NOT NULL,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        event_id TEXT,
        event_gen INTEGER NOT NULL DEFAULT 0,
        event_state TEXT NOT NULL,
        event_applied_hash TEXT,
        applied_cancelled INTEGER NOT NULL,
        dirty INTEGER NOT NULL,
        task_id TEXT,
        task_state TEXT NOT NULL,
        task_applied_hash TEXT,
        task_base_deadline TEXT,
        task_due TEXT,
        task_removed_reason TEXT,
        id_uncertain INTEGER NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    "CREATE INDEX items_source_kind ON items (source, kind)",
    "CREATE INDEX items_dirty ON items (dirty, source)",
    """CREATE TABLE intents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        uid TEXT NOT NULL,
        target TEXT NOT NULL CHECK (target IN ('calendar', 'tasks')),
        action TEXT NOT NULL CHECK (action IN ('create', 'delete')),
        key TEXT NOT NULL,
        payload_hash TEXT,
        created_at TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'resolved', 'aborted')),
        note TEXT
    )""",
    "CREATE INDEX intents_state ON intents (state, uid)",
    """CREATE TABLE source_days (
        day TEXT NOT NULL,
        source TEXT NOT NULL,
        fetched_at TEXT,
        fetch_complete INTEGER NOT NULL DEFAULT 0,
        writes_done_at TEXT,
        PRIMARY KEY (day, source)
    )""",
    """CREATE TABLE source_status (
        source TEXT PRIMARY KEY,
        last_attempt_at TEXT,
        last_error_code TEXT,
        auth_blocked_at TEXT,
        auth_block_code TEXT
    )""",
    """CREATE TABLE runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at TEXT NOT NULL,
        finished_at TEXT NOT NULL,
        day TEXT NOT NULL,
        mode TEXT NOT NULL,
        status TEXT NOT NULL,
        fingerprint TEXT,
        report_path TEXT
    )""",
    "CREATE INDEX runs_day ON runs (day)",
    """CREATE TABLE report_opened (
        day TEXT PRIMARY KEY,
        fingerprint TEXT NOT NULL,
        opened_at TEXT NOT NULL
    )""",
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
)

# v2: report_opened holds SEVERAL fingerprints per day (problem A, then B, then A again must not open a third tab). The v1
# table had `day` as its primary key, so a second fingerprint replaced the first. `seq` orders the rows: the newest
# marking is the one the single-value API `report_opened_fingerprint` returns. Existing rows are carried over.
_V2 = (
    """CREATE TABLE report_opened_v2 (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        day TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        opened_at TEXT NOT NULL,
        UNIQUE (day, fingerprint)
    )""",
    "INSERT INTO report_opened_v2 (day, fingerprint, opened_at) SELECT day, fingerprint, opened_at FROM report_opened ORDER BY day",
    "DROP TABLE report_opened",
    "ALTER TABLE report_opened_v2 RENAME TO report_opened",
)

# Append-only: index i migrates user_version i -> i + 1.
_V3 = ("ALTER TABLE items ADD COLUMN task_attempt INTEGER",)

_MIGRATIONS: tuple[tuple[str, ...], ...] = (_V1, _V2, _V3)
SCHEMA_VERSION = len(_MIGRATIONS)

_LAST_FETCH_KEY = "last_complete_fetch."  # + source value; survives prune_old


# --------------------------------------------------------------------------- value types


@dataclass(frozen=True)
class Intent:
    """A write intent for a non-idempotent Google operation (committed BEFORE the API call)."""

    id: int
    uid: str
    target: Target
    action: str  # "create" | "delete"
    key: str  # event id, or task marker / task id
    payload_hash: str | None
    created_at: datetime
    state: str  # "open" | "resolved" | "aborted"
    note: str | None = None

    @property
    def source(self) -> Source:
        """Source derived from the uid prefix (``<source>:<kind>:<id>``)."""
        return Source(self.uid.split(":", 1)[0])


@dataclass(frozen=True)
class SourceDay:
    """Per-day status of one source. ``fetched_at`` is None if only ``record_writes_done`` ever ran."""

    day: date
    source: Source
    fetched_at: datetime | None
    fetch_complete: bool
    writes_done_at: datetime | None

    @property
    def done(self) -> bool:
        return self.fetch_complete and self.writes_done_at is not None


@dataclass(frozen=True)
class SourceStatusRow:
    source: Source
    last_attempt_at: datetime | None
    last_error_code: str | None
    auth_blocked_at: datetime | None
    auth_block_code: str | None

    @property
    def auth_blocked(self) -> bool:
        return self.auth_blocked_at is not None


@dataclass(frozen=True)
class RunRow:
    id: int
    started_at: datetime
    finished_at: datetime
    day: date
    mode: RunMode
    status: OverallStatus
    fingerprint: str | None
    report_path: str | None


# --------------------------------------------------------------------------- helpers


def _enc(v: datetime | None) -> str | None:
    return None if v is None else to_vienna(v).isoformat()  # raises ValueError for naive datetimes


def _dec(s: str | None) -> datetime | None:
    return None if s is None else to_vienna(datetime.fromisoformat(s))


def _src(source: Source | str) -> str:
    return Source(source).value


def _day(d: date) -> str:
    """Calendar day as ``YYYY-MM-DD``. A datetime (a date subclass) is rejected: its isoformat would poison the day keys."""
    if isinstance(d, datetime):
        raise ValueError("day must be a date, not a datetime")
    return d.isoformat()


def _db_error(path: str, exc: sqlite3.Error) -> ConfigError | TransientError:
    msg = str(exc).lower()
    if "locked" in msg or "busy" in msg:
        return _busy_error()
    if "unable to open" in msg:
        return ConfigError(
            "Die Zustandsdatenbank konnte nicht geöffnet werden.",
            hint="Prüfe, ob der Ordner existiert und beschreibbar ist.",
            detail=f"{Path(path).name}: {exc}",
        )
    if "not a database" in msg or "malformed" in msg or "corrupt" in msg:  # genuine corruption only
        return ConfigError(
            "Die Zustandsdatenbank ist beschädigt oder keine gültige Datenbank.",
            hint=(
                "Die Datei wurde nicht verändert. Verschiebe sie als Sicherung an einen anderen Ort; "
                "SchulSync legt dann beim nächsten Start eine neue an."
            ),
            detail=f"{Path(path).name}: {exc}",
        )
    # Read-only file, disk full, I/O error, ...: NOT corruption. Never advise moving the file away
    # (an empty state would lose event/task ids and create duplicates).
    return ConfigError(
        "Die Zustandsdatenbank kann nicht gelesen oder geschrieben werden.",
        hint=(
            "Prüfe Schreibschutz, freien Speicherplatz und Berechtigungen der Datei. "
            "Die Datei nicht löschen oder verschieben, sonst gehen die Zuordnungen zu Kalendereinträgen verloren."
        ),
        detail=f"{Path(path).name}: {exc}",
    )


def _busy_error() -> TransientError:
    return TransientError(
        "Die Zustandsdatenbank wird gerade von einem anderen SchulSync-Vorgang verwendet.",
        hint="Warte kurz und versuche es erneut; läuft SchulSync bereits, dessen Ende abwarten.",
    )


# --------------------------------------------------------------------------- store


class StateStore:
    """SQLite-backed state. Construct with a file path or ``":memory:"``; usable as a context manager."""

    def __init__(self, path: str | Path = ":memory:", *, busy_timeout_ms: int = 3000):
        self.path = str(path)
        self._depth = 0
        try:
            self._conn = sqlite3.connect(self.path, isolation_level=None)
        except sqlite3.Error as exc:  # pragma: no cover - connect is lazy, errors surface in _configure
            raise _db_error(self.path, exc) from exc
        self._conn.row_factory = sqlite3.Row
        try:
            self._configure(busy_timeout_ms)
            self._migrate()
        except sqlite3.Error as exc:
            self._conn.close()
            raise _db_error(self.path, exc) from exc
        except BaseException:
            self._conn.close()
            raise

    def _configure(self, busy_timeout_ms: int) -> None:
        c = self._conn
        c.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        if self.path != ":memory:":
            c.execute("PRAGMA journal_mode = WAL")  # first read of the file: fails for non-database files
            if c.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError("quick_check failed (database disk image is malformed)")
        c.execute("PRAGMA synchronous = FULL")
        c.execute("PRAGMA foreign_keys = ON")

    def _migrate(self) -> None:
        """Apply missing migrations; idempotent and safe if two processes start at once.

        A current schema is detected without a transaction, so opening a store never needs the write lock.
        """
        if self.schema_version == SCHEMA_VERSION:
            return
        while True:
            with self.transaction():
                version = self._conn.execute("PRAGMA user_version").fetchone()[0]
                if version > SCHEMA_VERSION:
                    raise ConfigError(
                        "Die Zustandsdatenbank stammt von einer neueren SchulSync-Version.",
                        hint="Installiere die aktuelle SchulSync-Version oder verwende eine andere Datenbankdatei.",
                        detail=f"user_version={version}, unterstützt bis {SCHEMA_VERSION}",
                    )
                if version == SCHEMA_VERSION:
                    return
                for stmt in _MIGRATIONS[version]:
                    self._conn.execute(stmt)
                self._conn.execute(f"PRAGMA user_version = {version + 1}")

    # ------------------------------------------------------------------ lifecycle / transactions

    def close(self) -> None:
        """Close the connection (idempotent)."""
        self._conn.close()

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def schema_version(self) -> int:
        """Current ``PRAGMA user_version`` of the open database."""
        return self._conn.execute("PRAGMA user_version").fetchone()[0]

    @contextmanager
    def _translated(self) -> Iterator[None]:
        """Turn raw sqlite3 errors (disk full, read-only, I/O, corruption) into German SchulSyncErrors.

        Programming errors (constraint violations, bad SQL) stay as they are.
        """
        try:
            yield
        except (sqlite3.IntegrityError, sqlite3.ProgrammingError):
            raise
        except sqlite3.Error as exc:
            raise _db_error(self.path, exc) from exc

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """``BEGIN IMMEDIATE`` ... ``COMMIT``; any exception rolls everything back and propagates.

        Re-entrant: nested use runs in a SAVEPOINT of the outer transaction (only the outermost commits; a failed
        inner block is rolled back on its own).
        Raises :class:`TransientError` (German message) if another connection holds the write lock longer
        than ``busy_timeout_ms``; other sqlite3 failures (disk full, read-only, ...) become :class:`ConfigError`.
        SQLite rolls the whole transaction back by itself on some errors (disk full, I/O): then nothing is left
        to roll back, and the original error is propagated unmasked.
        """
        with self._translated():
            if self._depth:
                # Nested: SAVEPOINT, so a failed inner block is undone even if the caller catches the error and the
                # outer transaction continues (per-item error isolation must not commit half-done blocks).
                if not self._conn.in_transaction:  # SQLite rolled back already; a SAVEPOINT would start a new one
                    raise sqlite3.OperationalError("the transaction was rolled back by SQLite after an earlier error")
                name = f"sp{self._depth}"
                self._conn.execute(f"SAVEPOINT {name}")
                self._depth += 1
                try:
                    yield
                except BaseException:
                    if self._conn.in_transaction:  # False after an automatic rollback: savepoint is gone
                        self._conn.execute(f"ROLLBACK TO {name}")
                        self._conn.execute(f"RELEASE {name}")
                    raise
                else:
                    self._conn.execute(f"RELEASE {name}")
                finally:
                    self._depth -= 1
                return
            self._conn.execute("BEGIN IMMEDIATE")  # busy -> OperationalError("database is locked") -> TransientError
            self._depth = 1
            try:
                yield
                self._conn.execute("COMMIT")
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise
            finally:
                self._depth = 0

    # ------------------------------------------------------------------ items

    def get(self, uid: str) -> ItemState | None:
        """Return the stored state of ``uid`` or None."""
        row = self._conn.execute("SELECT * FROM items WHERE uid = ?", (uid,)).fetchone()
        return None if row is None else self._state(row)

    def upsert(self, state: ItemState, at: datetime | None = None) -> None:
        """Insert or fully replace the row of ``state.uid``. Every ItemState field is persisted.

        ``at`` is the ``updated_at`` bookkeeping stamp (default: now). Call inside :meth:`transaction`
        to commit together with other changes.
        """
        item = state.item
        if isinstance(state.task_due, datetime):  # datetime is a date subclass; read() parses a plain date
            raise ValueError("task_due must be a date, not a datetime")
        with self.transaction():
            self._conn.execute(
                """INSERT OR REPLACE INTO items (
                    uid, source, kind, source_id, item_json, item_hash, cancelled, first_seen_at, last_seen_at,
                    event_id, event_gen, event_state, event_applied_hash, applied_cancelled, dirty,
                    task_id, task_state, task_applied_hash, task_base_deadline, task_due, task_removed_reason,
                    id_uncertain, updated_at, task_attempt
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    state.uid, item.source.value, item.kind.value, item.source_id,
                    canonical_json(item.to_dict()), state.item_hash, int(state.cancelled),
                    _enc(state.first_seen_at), _enc(state.last_seen_at),
                    state.event_id, state.event_gen, state.event_state.value, state.event_applied_hash,
                    int(state.applied_cancelled), int(state.dirty),
                    state.task_id, state.task_state.value, state.task_applied_hash, state.task_base_deadline,
                    None if state.task_due is None else state.task_due.isoformat(), state.task_removed_reason,
                    int(state.id_uncertain), _enc(at or now_vienna()), state.task_attempt,
                ),
            )

    def items_for_source(self, source: Source | str) -> list[ItemState]:
        """All items of one source, ordered by uid."""
        return self._states("WHERE source = ? ORDER BY uid", (_src(source),))

    def items_by_source_kind(self, source: Source | str, kind: Kind | str) -> list[ItemState]:
        """All items of one source and kind, ordered by uid."""
        return self._states("WHERE source = ? AND kind = ? ORDER BY uid", (_src(source), Kind(kind).value))

    def all_items(self) -> list[ItemState]:
        """Every stored item, ordered by uid."""
        return self._states("ORDER BY uid", ())

    def delete_item(self, uid: str) -> bool:
        """Remove the row of ``uid`` (used when an item without a stable id is handed over to a new uid). Returns False if
        there was no such row. Joins an outer :meth:`transaction`; intents of the uid are left alone (they are history)."""
        with self.transaction():
            return self._conn.execute("DELETE FROM items WHERE uid = ?", (uid,)).rowcount > 0

    def event_id_owner(self, event_id: str, uid: str) -> str | None:
        """uid of ANOTHER item whose state owns the calendar event id ``event_id`` (``uid`` itself never counts), or None.
        Identity adoption hands an event, with the id derived from the OLD uid, to the new uid; the engine asks this before
        it derives an id for an insert."""
        row = self._conn.execute(
            "SELECT uid FROM items WHERE event_id = ? AND uid != ? LIMIT 1", (event_id, uid)
        ).fetchone()
        return None if row is None else row[0]

    def forget_applied(self, uid: str, target: Target | str, *, base_deadline: str | None = None) -> None:
        """Persist "what Google holds is unknown" BEFORE a patch, so that a crash or a lost response cannot leave the state
        claiming the old content (see sync.py `_forget_applied`). Calendar: ``event_applied_hash`` -> NULL. Tasks:
        ``task_applied_hash`` -> NULL, and if ``base_deadline`` is given ``task_base_deadline`` is set to it (the engine
        passes a sentinel that never equals a real deadline, so the planner treats the next plan as a deadline change)."""
        with self.transaction():
            if Target(target) == Target.CALENDAR:
                self._conn.execute("UPDATE items SET event_applied_hash = NULL WHERE uid = ?", (uid,))
            elif base_deadline is None:
                self._conn.execute("UPDATE items SET task_applied_hash = NULL WHERE uid = ?", (uid,))
            else:
                self._conn.execute(
                    "UPDATE items SET task_applied_hash = NULL, task_base_deadline = ? WHERE uid = ?", (base_deadline, uid)
                )

    def dirty_uids(self, source: Source | str | None = None) -> list[str]:
        """Uids whose Google writes are still pending, ordered by uid (optionally for one source)."""
        sql, args = "SELECT uid FROM items WHERE dirty = 1", ()
        if source is not None:
            sql, args = sql + " AND source = ?", (_src(source),)
        return [r[0] for r in self._conn.execute(sql + " ORDER BY uid", args)]

    def _states(self, tail: str, args: tuple) -> list[ItemState]:
        return [self._state(r) for r in self._conn.execute("SELECT * FROM items " + tail, args)]

    @staticmethod
    def _state(r: sqlite3.Row) -> ItemState:
        return ItemState(
            item=SourceItem.from_dict(json.loads(r["item_json"])),
            item_hash=r["item_hash"],
            cancelled=bool(r["cancelled"]),
            first_seen_at=_dec(r["first_seen_at"]),  # type: ignore[arg-type]
            last_seen_at=_dec(r["last_seen_at"]),  # type: ignore[arg-type]
            event_id=r["event_id"],
            event_gen=r["event_gen"],
            event_state=EventState(r["event_state"]),
            event_applied_hash=r["event_applied_hash"],
            applied_cancelled=bool(r["applied_cancelled"]),
            dirty=bool(r["dirty"]),
            task_id=r["task_id"],
            task_state=TaskState(r["task_state"]),
            task_applied_hash=r["task_applied_hash"],
            task_base_deadline=r["task_base_deadline"],
            task_due=None if r["task_due"] is None else date.fromisoformat(r["task_due"]),
            task_removed_reason=r["task_removed_reason"],
            id_uncertain=bool(r["id_uncertain"]),
            task_attempt=r["task_attempt"],
        )

    # ------------------------------------------------------------------ intents

    def open_intent(
        self,
        uid: str,
        target: Target | str,
        action: str,
        key: str,
        payload_hash: str | None = None,
        at: datetime | None = None,
    ) -> int:
        """Record a write intent and return its id. Committed and fsynced before returning.

        ``action`` is ``"create"`` or ``"delete"``. Must NOT be called inside :meth:`transaction`
        (it would not be durable before the Google call): raises ``RuntimeError`` then.
        """
        if ":" not in uid:
            raise ValueError(f"intent uid must start with '<source>:': {uid!r}")
        Source(uid.split(":", 1)[0])  # raises ValueError for an unknown source prefix
        if self._depth:
            raise RuntimeError("open_intent must be committed immediately; do not call it inside transaction()")
        with self.transaction():
            cur = self._conn.execute(
                "INSERT INTO intents (uid, target, action, key, payload_hash, created_at) VALUES (?,?,?,?,?,?)",
                (uid, Target(target).value, action, key, payload_hash, _enc(at or now_vienna())),
            )
            return int(cur.lastrowid)

    def resolve_intent(self, intent_id: int, note: str | None = None) -> None:
        """Mark an open intent as done (the Google operation happened). Joins an outer transaction."""
        self._close_intent(intent_id, "resolved", note)

    def abort_intent(self, intent_id: int, note: str | None = None) -> None:
        """Mark an open intent as abandoned (the operation provably did not happen)."""
        self._close_intent(intent_id, "aborted", note)

    def _close_intent(self, intent_id: int, state: str, note: str | None) -> None:
        with self.transaction():
            cur = self._conn.execute(
                "UPDATE intents SET state = ?, note = ? WHERE id = ? AND state = 'open'", (state, note, intent_id)
            )
            if cur.rowcount != 1:
                raise KeyError(f"intent {intent_id} is not open")

    def get_intent(self, intent_id: int) -> Intent | None:
        """Return an intent in any state, or None."""
        row = self._conn.execute("SELECT * FROM intents WHERE id = ?", (intent_id,)).fetchone()
        return None if row is None else self._intent(row)

    def open_intents(self, source: Source | str | None = None) -> list[Intent]:
        """Open intents, oldest first; optionally only those whose uid belongs to ``source``."""
        sql, args = "SELECT * FROM intents WHERE state = 'open'", ()
        if source is not None:
            sql, args = sql + " AND uid LIKE ?", (_src(source) + ":%",)
        return [self._intent(r) for r in self._conn.execute(sql + " ORDER BY id", args)]

    @staticmethod
    def _intent(r: sqlite3.Row) -> Intent:
        return Intent(
            id=r["id"], uid=r["uid"], target=Target(r["target"]), action=r["action"], key=r["key"],
            payload_hash=r["payload_hash"], created_at=_dec(r["created_at"]),  # type: ignore[arg-type]
            state=r["state"], note=r["note"],
        )

    def pending_count(self, source: Source | str | None = None) -> int:
        """Pending Google writes = dirty items + open intents (optionally for one source)."""
        if source is None:
            return self._conn.execute(
                "SELECT (SELECT COUNT(*) FROM items WHERE dirty = 1) + (SELECT COUNT(*) FROM intents WHERE state = 'open')"
            ).fetchone()[0]
        s = _src(source)
        return self._conn.execute(
            "SELECT (SELECT COUNT(*) FROM items WHERE dirty = 1 AND source = ?)"
            " + (SELECT COUNT(*) FROM intents WHERE state = 'open' AND uid LIKE ?)",
            (s, s + ":%"),
        ).fetchone()[0]

    # ------------------------------------------------------------------ day tracking

    def record_fetch(self, day: date, source: Source | str, at: datetime, complete: bool) -> None:
        """Record a fetch of ``source`` on ``day``.

        Overwrites ``fetched_at``/``fetch_complete`` and RESETS ``writes_done_at`` (new data means the Google
        writes have to be confirmed again via :meth:`record_writes_done`). A complete fetch also advances
        :meth:`last_complete_fetch`.
        """
        s = _src(source)
        with self.transaction():
            self._conn.execute(
                """INSERT INTO source_days (day, source, fetched_at, fetch_complete, writes_done_at)
                   VALUES (?,?,?,?,NULL)
                   ON CONFLICT (day, source) DO UPDATE SET
                       fetched_at = excluded.fetched_at, fetch_complete = excluded.fetch_complete,
                       writes_done_at = NULL""",
                (_day(day), s, _enc(at), int(complete)),
            )
            if complete:
                prev = self.last_complete_fetch(s)
                # compare INSTANTS: two aware Europe/Vienna datetimes compare by wall clock (the fold is ignored)
                if prev is None or at.astimezone(timezone.utc) > prev.astimezone(timezone.utc):
                    self.set_meta(_LAST_FETCH_KEY + s, _enc(at))  # type: ignore[arg-type]

    def record_writes_done(self, day: date, source: Source | str, at: datetime) -> None:
        """Record that all Google writes of ``source`` for ``day`` are done."""
        with self.transaction():
            self._conn.execute(
                """INSERT INTO source_days (day, source, fetched_at, fetch_complete, writes_done_at)
                   VALUES (?,?,NULL,0,?)
                   ON CONFLICT (day, source) DO UPDATE SET writes_done_at = excluded.writes_done_at""",
                (_day(day), _src(source), _enc(at)),
            )

    def get_source_day(self, day: date, source: Source | str) -> SourceDay | None:
        row = self._conn.execute(
            "SELECT * FROM source_days WHERE day = ? AND source = ?", (_day(day), _src(source))
        ).fetchone()
        if row is None:
            return None
        return SourceDay(
            day=date.fromisoformat(row["day"]), source=Source(row["source"]), fetched_at=_dec(row["fetched_at"]),
            fetch_complete=bool(row["fetch_complete"]), writes_done_at=_dec(row["writes_done_at"]),
        )

    def day_done(self, day: date, source: Source | str) -> bool:
        """True iff the fetch of that day was complete AND all writes were recorded as done."""
        sd = self.get_source_day(day, source)
        return sd is not None and sd.done

    def last_complete_fetch(self, source: Source | str) -> datetime | None:
        """Time of the newest complete fetch of ``source`` on any day (kept across prune_old)."""
        v = self.get_meta(_LAST_FETCH_KEY + _src(source))
        return None if v is None else _dec(v)

    # ------------------------------------------------------------------ source status

    def record_attempt(self, source: Source | str, at: datetime, error_code: str | None = None) -> None:
        """Record a fetch/login attempt; ``error_code=None`` clears the last error. Keeps any auth block."""
        with self.transaction():
            self._conn.execute(
                """INSERT INTO source_status (source, last_attempt_at, last_error_code) VALUES (?,?,?)
                   ON CONFLICT (source) DO UPDATE SET
                       last_attempt_at = excluded.last_attempt_at, last_error_code = excluded.last_error_code""",
                (_src(source), _enc(at), error_code),
            )

    def set_auth_blocked(self, source: Source | str, at: datetime, code: str) -> None:
        """Mark the login of ``source`` as permanently rejected (automatic runs skip it until cleared)."""
        with self.transaction():
            self._conn.execute(
                """INSERT INTO source_status (source, auth_blocked_at, auth_block_code) VALUES (?,?,?)
                   ON CONFLICT (source) DO UPDATE SET
                       auth_blocked_at = excluded.auth_blocked_at, auth_block_code = excluded.auth_block_code""",
                (_src(source), _enc(at), code),
            )

    def clear_auth_block(self, source: Source | str) -> None:
        with self.transaction():
            self._conn.execute(
                "UPDATE source_status SET auth_blocked_at = NULL, auth_block_code = NULL WHERE source = ?",
                (_src(source),),
            )

    def is_auth_blocked(self, source: Source | str) -> bool:
        row = self._conn.execute(
            "SELECT auth_blocked_at FROM source_status WHERE source = ?", (_src(source),)
        ).fetchone()
        return row is not None and row[0] is not None

    def get_source_status(self, source: Source | str) -> SourceStatusRow | None:
        row = self._conn.execute("SELECT * FROM source_status WHERE source = ?", (_src(source),)).fetchone()
        if row is None:
            return None
        return SourceStatusRow(
            source=Source(row["source"]), last_attempt_at=_dec(row["last_attempt_at"]),
            last_error_code=row["last_error_code"], auth_blocked_at=_dec(row["auth_blocked_at"]),
            auth_block_code=row["auth_block_code"],
        )

    # ------------------------------------------------------------------ runs / reports

    def record_run(
        self,
        started_at: datetime,
        finished_at: datetime,
        day: date,
        mode: RunMode | str,
        status: OverallStatus | str,
        fingerprint: str | None = None,
        report_path: str | None = None,
    ) -> int:
        """Append a run record and return its id."""
        with self.transaction():
            cur = self._conn.execute(
                "INSERT INTO runs (started_at, finished_at, day, mode, status, fingerprint, report_path)"
                " VALUES (?,?,?,?,?,?,?)",
                (
                    _enc(started_at), _enc(finished_at), _day(day), RunMode(mode).value,
                    OverallStatus(status).value, fingerprint, report_path,
                ),
            )
            return int(cur.lastrowid)

    def last_run(self) -> RunRow | None:
        """The most recently recorded run (highest id), or None."""
        r = self._conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        if r is None:
            return None
        return RunRow(
            id=r["id"], started_at=_dec(r["started_at"]), finished_at=_dec(r["finished_at"]),  # type: ignore[arg-type]
            day=date.fromisoformat(r["day"]), mode=RunMode(r["mode"]), status=OverallStatus(r["status"]),
            fingerprint=r["fingerprint"], report_path=r["report_path"],
        )

    def report_opened_fingerprints(self, day: date) -> set[str]:
        """ALL fingerprints whose report was opened in the browser on ``day`` (tab-flood protection: a problem that was
        shown earlier today must not open a tab again, even after another problem came in between)."""
        return {r[0] for r in self._conn.execute("SELECT fingerprint FROM report_opened WHERE day = ?", (_day(day),))}

    def report_opened_fingerprint(self, day: date) -> str | None:
        """The most recently marked fingerprint of ``day`` (the single-value API of schema v1), or None."""
        row = self._conn.execute(
            "SELECT fingerprint FROM report_opened WHERE day = ? ORDER BY seq DESC LIMIT 1", (_day(day),)
        ).fetchone()
        return None if row is None else row[0]

    def mark_report_opened(self, day: date, fingerprint: str, at: datetime) -> None:
        """Remember that the report with ``fingerprint`` was opened on ``day``. Earlier fingerprints of the day are kept;
        marking one again just makes it the newest."""
        with self.transaction():
            self._conn.execute("DELETE FROM report_opened WHERE day = ? AND fingerprint = ?", (_day(day), fingerprint))
            self._conn.execute(
                "INSERT INTO report_opened (day, fingerprint, opened_at) VALUES (?,?,?)", (_day(day), fingerprint, _enc(at))
            )

    # ------------------------------------------------------------------ meta

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else row[0]

    def set_meta(self, key: str, value: str) -> None:
        with self.transaction():
            self._conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    # ------------------------------------------------------------------ maintenance

    def copy_to_memory(self) -> "StateStore":
        """Independent in-memory copy (sqlite3 backup API) for the preview mode.

        Only committed data is copied; writes to either side never affect the other. Raises ``RuntimeError``
        inside :meth:`transaction` (the sqlite3 backup would wait forever on the source's own write lock).
        """
        if self._depth:
            raise RuntimeError("copy_to_memory must not be called inside transaction()")
        copy = StateStore(":memory:")
        self._conn.backup(copy._conn)
        return copy

    def prune_old(self, before: date) -> int:
        """Delete ``runs``, ``report_opened`` and ``source_days`` rows with ``day < before``.

        Never touches items, intents, source status or meta (so ``last_complete_fetch`` survives).
        Returns the number of deleted rows.
        """
        cutoff = _day(before)
        with self.transaction():
            return sum(
                self._conn.execute(f"DELETE FROM {table} WHERE day < ?", (cutoff,)).rowcount
                for table in ("runs", "report_opened", "source_days")
            )
