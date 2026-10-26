"""Status layer (SPEC A3.3.2) with rule 27.

A status that restricts movement (closed, obstructed, flooded, out of service) applies at
once, whatever its source. A status that lifts a restriction (``ok``) applies only if it
comes from the source of that restriction or from the operator; otherwise it is rejected,
so a false restrictive report can only lengthen a route. Robot reports expire after
``status_ttl_min`` unless they say otherwise.
"""

from datetime import datetime, timedelta

from a6g_common.schemas import StatusReport


class StatusLayer:
    def __init__(self, ttl_min: float = 60.0) -> None:
        self.ttl = timedelta(minutes=ttl_min)
        self._active: dict[str, list[StatusReport]] = {}  # subject -> active restrictions
        self.history: list[tuple[StatusReport, bool, str]] = []  # (report, accepted, reason)

    def _expire(self, now: datetime) -> None:
        for subject, reports in list(self._active.items()):
            alive = [r for r in reports if r.valid_until is None or r.valid_until > now]
            if alive:
                self._active[subject] = alive
            else:
                del self._active[subject]

    def put(self, report: StatusReport, now: datetime) -> tuple[bool, str]:
        """Apply ``report`` under rule 27: (accepted, reason)."""
        self._expire(now)
        if report.valid_until is None and report.source.startswith("robot:"):
            report = report.model_copy(update={"valid_until": report.stamp + self.ttl})
        active = self._active.get(report.subject, [])
        if report.restrictive:
            # One restriction per source and subject: a new report from the same source
            # replaces its previous one (e.g. obstructed -> flooded).
            kept = [r for r in active if r.source != report.source]
            self._active[report.subject] = [*kept, report]
            result = (True, "restrictive: applies at once (rule 27)")
        elif not active:
            result = (True, "no restriction in effect")
        elif report.source == "operator":
            del self._active[report.subject]
            result = (True, "lifted by the operator")
        elif any(r.source == report.source for r in active):
            rest = [r for r in active if r.source != report.source]
            if rest:
                self._active[report.subject] = rest
            else:
                del self._active[report.subject]
            others = sorted({r.source for r in rest})
            result = (
                True,
                "lifted by its source" + (f"; still restricted by {others}" if others else ""),
            )
        else:
            sources = sorted({r.source for r in active})
            result = (
                False,
                f"rejected: only {sources} or the operator can lift this restriction (rule 27)",
            )
        self.history.append((report, *result))
        return result

    def effective(self, subject: str, now: datetime) -> tuple[str, list[StatusReport]]:
        """(status, restrictions in force) for ``subject``; ``ok`` when none."""
        self._expire(now)
        active = self._active.get(subject, [])
        if not active:
            return "ok", []
        latest = max(active, key=lambda r: r.stamp)
        return latest.status, sorted(active, key=lambda r: r.status_id)

    def at(self, when: datetime) -> "StatusLayer":
        """The layer as it stood at ``when``: the accepted reports stamped up to then,
        replayed in order (route compliance is judged against what was in force)."""
        past = StatusLayer()
        past.ttl = self.ttl
        for report, accepted, _ in self.history:
            if accepted and report.stamp <= when:
                past.put(report, report.stamp)
        past.history = []
        return past

    def restricted(self, now: datetime) -> dict[str, list[StatusReport]]:
        self._expire(now)
        return {s: list(r) for s, r in self._active.items()}
