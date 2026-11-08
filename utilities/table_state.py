"""
Per-table state machine: FREE -> OCCUPIED -> NEEDS_CLEARING -> FREE.

Pure logic, no OpenCV/YOLO. Feed it a person count and a monotonic timestamp
and it returns a list of Events. Easy to unit test without any video.
"""
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from statistics import median


class State(str, Enum):
    FREE = "free"
    OCCUPIED = "occupied"
    NEEDS_CLEARING = "needs_clearing"


@dataclass
class Event:
    type: str            # SEATED | CHECK_ON_TABLE | GUESTS_LEFT | NEEDS_CLEARING | CLEARED
    table: str
    ts: float            # wall-clock epoch seconds
    data: dict = field(default_factory=dict)

    def to_dict(self):
        return {
            "type": self.type,
            "table": self.table,
            "ts": self.ts,
            "time": datetime.fromtimestamp(self.ts).strftime("%H:%M:%S"),
            **self.data,
        }


@dataclass
class TableConfig:
    occupy_seconds: float = 3.0         # people must be present this long to count as seated
    free_seconds: float = 20.0          # table must be empty this long to count as left
    up_lock_seconds: float = 2.0        # accept a higher guest count after this long
    down_lock_seconds: float = 10.0     # accept a lower guest count after this long
    smooth_seconds: float = 3.0         # median window for raw counts
    check_after_seconds: float = 300.0  # "check on table" alert (5 min)
    min_visit_seconds: float = 20.0     # shorter visits are treated as noise
    require_clearing: bool = True       # go through NEEDS_CLEARING before FREE
    auto_clear_seconds: float = 0.0     # 0 = wait for staff to press "cleared"


def _fmt(epoch):
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")


class TableState:
    def __init__(self, name, cfg: TableConfig):
        self.name = name
        self.cfg = cfg
        self.state = State.FREE
        self.guests = 0
        self.peak = 0
        self._reset_visit()
        self._dirty_since = None

    def _reset_visit(self):
        self._present_since = None
        self._empty_since = None
        self._samples = deque()
        self._up_since = None
        self._down_since = None
        self._seated_mono = None
        self._seated_wall = None
        self._check_sent = False

    def _event(self, type_, **data):
        return Event(type_, self.name, time.time(), data)

    # ---------- public API ----------
    def update(self, raw_count: int, now: float):
        """Call once per inference. `now` must be time.monotonic()."""
        events = []
        if self.state is State.OCCUPIED:
            self._update_occupied(raw_count, now, events)
            return events

        if raw_count > 0:
            if self._present_since is None:
                self._present_since = now
            if now - self._present_since >= self.cfg.occupy_seconds:
                if self.state is State.NEEDS_CLEARING:
                    events.append(self._event("CLEARED", auto=True))
                self._seat(raw_count, now, events)
        else:
            self._present_since = None
            if (self.state is State.NEEDS_CLEARING and self.cfg.auto_clear_seconds > 0
                    and now - self._dirty_since >= self.cfg.auto_clear_seconds):
                self.state = State.FREE
                events.append(self._event("CLEARED", auto=True))
        return events

    def mark_cleared(self, now: float):
        """Staff pressed 'cleared' in the dashboard."""
        if self.state is State.NEEDS_CLEARING:
            self.state = State.FREE
            self._dirty_since = None
            return [self._event("CLEARED", auto=False)]
        return []

    def force_end(self, now: float):
        """Close any open visit (shutdown/restart) so it still gets logged."""
        events = []
        if self.state is State.OCCUPIED:
            self._end_visit(now, events, clearing=False)
        return events

    def snapshot(self, now: float):
        occupied = self.state is State.OCCUPIED
        dur = int(now - self._seated_mono) if occupied else 0
        dirty = int(now - self._dirty_since) if self.state is State.NEEDS_CLEARING else 0
        return {
            "state": self.state.value,
            "occupied": occupied,
            "guests": self.guests if occupied else 0,
            "duration_sec": dur,
            "dirty_sec": dirty,
            "check_due": occupied and dur >= self.cfg.check_after_seconds,
        }

    # ---------- internals ----------
    def _seat(self, raw, now, events):
        self._reset_visit()
        self.state = State.OCCUPIED
        self._seated_mono = now
        self._seated_wall = time.time()
        self._samples.append((now, raw))
        self.guests = max(1, raw)
        self.peak = self.guests
        events.append(self._event("SEATED", guests=self.guests))

    def _update_occupied(self, raw, now, events):
        c = self.cfg
        if raw > 0:
            self._empty_since = None
            self._samples.append((now, raw))   # zeros are not sampled: occlusion shouldn't lower the count
        elif self._empty_since is None:
            self._empty_since = now

        cutoff = now - c.smooth_seconds
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

        if self._empty_since is not None and now - self._empty_since >= c.free_seconds:
            self._end_visit(now, events)
            return

        if self._samples:
            smoothed = int(round(median(n for _, n in self._samples)))
            self._update_lock(smoothed, now)

        if not self._check_sent and now - self._seated_mono >= c.check_after_seconds:
            self._check_sent = True
            events.append(self._event("CHECK_ON_TABLE", guests=self.guests,
                                      seated_sec=int(now - self._seated_mono)))

    def _update_lock(self, smoothed, now):
        c = self.cfg
        if smoothed > self.guests:
            self._down_since = None
            if self._up_since is None:
                self._up_since = now
            if now - self._up_since >= c.up_lock_seconds:
                self.guests, self._up_since = smoothed, None
        elif smoothed < self.guests:
            self._up_since = None
            if self._down_since is None:
                self._down_since = now
            if now - self._down_since >= c.down_lock_seconds:
                self.guests, self._down_since = smoothed, None
        else:
            self._up_since = self._down_since = None
        self.peak = max(self.peak, self.guests)

    def _end_visit(self, now, events, clearing=True):
        # People actually left when the table first went empty, not when the timer expired.
        end_mono = self._empty_since if self._empty_since is not None else now
        end_wall = time.time() - (now - end_mono)
        dur = end_mono - self._seated_mono
        valid = dur >= self.cfg.min_visit_seconds

        if valid:
            events.append(self._event(
                "GUESTS_LEFT", guests=max(1, self.peak), duration_sec=round(dur, 1),
                start_ts=_fmt(self._seated_wall), end_ts=_fmt(end_wall)))

        if valid and clearing and self.cfg.require_clearing:
            self.state = State.NEEDS_CLEARING
            self._dirty_since = now
            events.append(self._event("NEEDS_CLEARING", guests=max(1, self.peak)))
        else:
            self.state = State.FREE
        self.guests = 0
        self.peak = 0
        self._reset_visit()