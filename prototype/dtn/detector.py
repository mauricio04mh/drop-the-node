"""Adaptive failure detector: per-store latency estimates, suspicion and readmission.

A missed deadline is a local decision, not proof of a remote fact: a store
that does not answer in time may be dead or just slow. The detector turns
observed latencies into two numbers per store, following Jacobson and Karels
(the TCP retransmission timer):

- hedge delay  d = SRTT + 2·RTTVAR   (≈ 95th percentile of normal latency)
- timeout        = SRTT + 4·RTTVAR, clamped to [min, max]

A store that exceeds its timeout, or whose request fails because it died, is
*suspect*: it drops to the end of every preference order. Probes readmit it
once it answers again. A store that rejects a request as saturated is alive
and answering quickly, so saturation never makes it suspect.

All durations come from a monotonic clock, measured by the caller.
"""

import threading
from dataclasses import dataclass

ALPHA = 1 / 8  # weight of a new sample in SRTT
BETA = 1 / 4  # weight of a new deviation in RTTVAR


@dataclass
class _Estimate:
    srtt: float | None = None
    rttvar: float = 0.0
    suspect: bool = False


class FailureDetector:
    def __init__(
        self,
        stores,
        *,
        timeout_min: float,
        timeout_max: float,
        hedge_delay: float | None = None,
    ):
        """``timeout_min``/``timeout_max`` and ``hedge_delay`` are in seconds;
        a fixed ``hedge_delay`` replaces the adaptive one."""
        self._timeout_min = timeout_min
        self._timeout_max = timeout_max
        self._fixed_hedge = hedge_delay
        self._lock = threading.Lock()
        self._estimates = {s: _Estimate() for s in stores}

    def observe(self, store: str, seconds: float) -> None:
        """Record the latency of a successful request."""
        with self._lock:
            e = self._estimates[store]
            if e.srtt is None:
                e.srtt, e.rttvar = seconds, seconds / 2
            else:
                e.rttvar = (1 - BETA) * e.rttvar + BETA * abs(e.srtt - seconds)
                e.srtt = (1 - ALPHA) * e.srtt + ALPHA * seconds

    @property
    def timeout_cap(self) -> float:
        """The longest timeout any store can get."""
        return self._timeout_max

    def timeout(self, store: str) -> float:
        """How long to wait for ``store`` before suspecting it."""
        with self._lock:
            e = self._estimates[store]
            if e.srtt is None:
                return self._timeout_max  # nothing measured yet: be patient
            raw = e.srtt + 4 * e.rttvar
        return min(max(raw, self._timeout_min), self._timeout_max)

    def hedge_delay(self, store: str) -> float:
        """How long to wait for ``store`` before asking another replica too."""
        if self._fixed_hedge is not None:
            return self._fixed_hedge
        with self._lock:
            e = self._estimates[store]
            if e.srtt is None:
                return self._timeout_min
            raw = e.srtt + 2 * e.rttvar
        return min(raw, self.timeout(store))

    def suspect(self, store: str) -> bool:
        """Mark ``store`` suspect. Returns True if it was not suspect before."""
        with self._lock:
            e = self._estimates[store]
            newly = not e.suspect
            e.suspect = True
            return newly

    def readmit(self, store: str) -> bool:
        """Clear the suspicion. Returns True if ``store`` was suspect."""
        with self._lock:
            e = self._estimates[store]
            was = e.suspect
            e.suspect = False
            return was

    def is_suspect(self, store: str) -> bool:
        with self._lock:
            return self._estimates[store].suspect

    def suspects(self) -> list[str]:
        with self._lock:
            return [s for s, e in self._estimates.items() if e.suspect]

    def prefer(self, order) -> list[str]:
        """``order`` with suspect stores moved to the end, otherwise unchanged."""
        with self._lock:
            healthy = [s for s in order if not self._estimates[s].suspect]
            doubtful = [s for s in order if self._estimates[s].suspect]
        return healthy + doubtful
