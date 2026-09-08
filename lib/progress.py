"""Progress lines for long loops (param scan, MCPT, deep-history fetch).

One line to stdout at every 10 % of the work — `[scan] 36/120 cells, 2m10s elapsed,
~5m left` — so a run redirected to a log, or cut short by a tool timeout, still shows
where it got to and the agent can relay an ETA. Silent when the whole job projects to
finish under MIN_SECONDS: a 3-second automatic MCPT must not add ten lines to every
backtest's output.

    prog = Progress('scan', total, 'cells')
    for ...: work(); prog.tick()
"""
import time

MIN_SECONDS = 5.0   # jobs projected to finish faster than this print nothing
STEPS = 10          # one line per 1/STEPS of the work (plus the final one)


def fmt_duration(seconds):
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


class Progress:
    def __init__(self, tag, total, unit='items', min_seconds=None):
        self.tag = tag
        self.total = max(int(total), 0)
        self.unit = unit
        self.min_seconds = MIN_SECONDS if min_seconds is None else min_seconds
        self.done = 0
        self.t0 = time.monotonic()
        self.every = max(1, -(-self.total // STEPS))   # ceil(total / STEPS)

    def tick(self, n=1):
        if n <= 0 or self.total == 0 or self.done >= self.total:
            return  # nothing to count / already reported the final line
        self.done = min(self.done + n, self.total)   # an overshoot reports as total, once
        if self.done % self.every == 0 or self.done >= self.total:
            self._report()

    def _report(self):
        elapsed = time.monotonic() - self.t0
        if self.done >= self.total:
            if elapsed < self.min_seconds:
                return
            line = f"[{self.tag}] {self.done}/{self.total} {self.unit} done in {fmt_duration(elapsed)}"
        else:
            per_item = elapsed / self.done
            if per_item * self.total < self.min_seconds:
                return
            left = per_item * (self.total - self.done)
            line = (f"[{self.tag}] {self.done}/{self.total} {self.unit}, "
                    f"{fmt_duration(elapsed)} elapsed, ~{fmt_duration(left)} left")
        print(line, flush=True)
