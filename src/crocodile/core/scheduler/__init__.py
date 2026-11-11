"""Scheduled low-frequency pulls, and the calendar they answer to.

The coordinator itself is asset-class agnostic: it schedules, catches up on missed periods,
counts failures and records gaps. Each task answers to one clock: a task without a calendar
uses :class:`USMarketCalendar` (the US equity clock), and a task with an exchange calendar
takes its logical dates from that calendar. Crypto tasks pass
``crocodile.core.markets.calendar.AlwaysOpenCalendar``.
"""

from crocodile.core.scheduler.calendar import MARKET_TZ, USMarketCalendar
from crocodile.core.scheduler.coordinator import (
    DEFAULT_CLOSE_DELAY,
    DEFAULT_MAX_DATES_PER_TICK,
    DEFAULT_MAX_FAILURES,
    DEFAULT_RETRY_COOLDOWN,
    PullTask,
    ScheduledPullCoordinator,
)
from crocodile.core.scheduler.state import (
    FailureRecord,
    GapEntry,
    GapKind,
    InMemoryStateStore,
    JSONFileStateStore,
    SchedulerStateStore,
    TaskStateRecord,
)

__all__ = [
    "DEFAULT_CLOSE_DELAY",
    "DEFAULT_MAX_DATES_PER_TICK",
    "DEFAULT_MAX_FAILURES",
    "DEFAULT_RETRY_COOLDOWN",
    "MARKET_TZ",
    "FailureRecord",
    "GapEntry",
    "GapKind",
    "InMemoryStateStore",
    "JSONFileStateStore",
    "PullTask",
    "ScheduledPullCoordinator",
    "SchedulerStateStore",
    "TaskStateRecord",
    "USMarketCalendar",
]
