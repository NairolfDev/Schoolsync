"""Which Google target failed the target check (ANFORDERUNGEN 4: "die BETROFFENEN Schreibvorgänge zurückstellen").

`GoogleGateway.check_targets` (contract, google_types.py) answers with one `TargetUnavailableError`. The engine has to know
WHICH target is unusable: a missing task list must not keep the reachable calendar "Schule" empty, and a missing calendar must
not keep the task list empty. This module adds that information without touching the contract: a subclass of
`TargetUnavailableError` (so every `except TargetUnavailableError` keeps working) that carries the per-target result.

Contract request (for the orchestrator): move this information into `google_types.TargetCheck` / `errors.TargetUnavailableError`.
"""

from __future__ import annotations

from typing import Iterable, Mapping

from .errors import ErrorInfo, TargetUnavailableError
from .models import Target


class TargetsUnavailableError(TargetUnavailableError):
    """The target check found at least one target unusable.

    `down`      the unusable targets with their error (never empty).
    `verified`  targets that were checked and are usable. A target in neither set was NOT checked (the check stops at the
                first problem of the calendar); the engine defers the calendar then (it is only written after it was
                verified: never the main calendar, write rights) but still reads the task list itself, which is its own check.
    """

    def __init__(self, down: Mapping[Target, ErrorInfo], verified: Iterable[Target] = ()) -> None:
        if not down:
            raise ValueError("at least one failed target")
        infos = list(down.values())
        super().__init__(
            " ".join(dict.fromkeys(i.message for i in infos)),
            hint=" ".join(dict.fromkeys(i.hint for i in infos if i.hint)) or None,
            detail=" | ".join(dict.fromkeys(i.detail for i in infos if i.detail)) or None,
        )
        self.down: dict[Target, ErrorInfo] = dict(down)
        self.verified: frozenset[Target] = frozenset(verified)
