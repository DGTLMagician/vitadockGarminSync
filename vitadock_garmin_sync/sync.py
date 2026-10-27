"""Copy VitaDock measurements to Garmin Connect, remembering what was sent."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .vitadock import MODULE_CARDIODOCK, MODULE_TARGETSCALE, AccessToken, VitaDockClient

log = logging.getLogger(__name__)

# Re-read this much history on every run, so late-arriving or edited
# measurements are still picked up; already-synced ids are skipped.
OVERLAP = timedelta(days=2)


def pick(record: dict[str, Any], *names: str) -> Any:
    """Case-insensitive field lookup; returns the first present, non-null value."""
    lower = {k.lower(): v for k, v in record.items()}
    for n in names:
        v = lower.get(n.lower())
        if v is not None:
            return v
    return None


def positive(value: Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def measured_at(record: dict[str, Any], tz: tzinfo, wallclock: bool) -> datetime:
    """Measurement time as an aware datetime.

    VitaDock stores ``measurementDate`` as epoch millis. Depending on the
    device the value is either real UTC or the local wall-clock time encoded
    as if it were UTC; ``wallclock`` selects the latter interpretation.
    """
    raw = pick(record, "measurementDate", "measurement_date", "date")
    if isinstance(raw, str) and not raw.isdigit():
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    else:
        dt = datetime.fromtimestamp(int(raw) / 1000, timezone.utc)
    if wallclock:
        return dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def is_active(record: dict[str, Any]) -> bool:
    v = pick(record, "active", "isActive")
    return v is None or v is True or str(v).lower() in ("1", "true")


def record_id(module: str, record: dict[str, Any]) -> str:
    rid = pick(record, "id", "_id", "moduleid", "measurementId")
    if rid is None:
        rid = pick(record, "measurementDate")
    return f"{module}:{rid}"


@dataclass
class State:
    path: Path
    access_token: str | None = None
    access_secret: str | None = None
    since: dict[str, int] = field(default_factory=dict)
    synced: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> State:
        if not path.exists():
            return cls(path)
        data = json.loads(path.read_text())
        return cls(path, data.get("access_token"), data.get("access_secret"),
                   data.get("since", {}), data.get("synced", {}))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"access_token": self.access_token, "access_secret": self.access_secret,
                "since": self.since, "synced": self.synced}
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state-")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    @property
    def access(self) -> AccessToken | None:
        if self.access_token and self.access_secret:
            return AccessToken(self.access_token, self.access_secret)
        return None


@dataclass
class Syncer:
    vitadock: VitaDockClient
    garmin: Any  # garminconnect.Garmin
    state: State
    tz: tzinfo = field(default_factory=lambda: ZoneInfo("Europe/Amsterdam"))
    wallclock: bool = False
    initial_since: datetime | None = None
    dry_run: bool = False
    modules: tuple[str, ...] = (MODULE_TARGETSCALE, MODULE_CARDIODOCK)

    def run(self) -> int:
        uploaded = 0
        for module in self.modules:
            uploaded += self._sync_module(module)
        return uploaded

    def _sync_module(self, module: str) -> int:
        start_ms = self.state.since.get(module)
        if start_ms is None:
            start_ms = int(self.initial_since.timestamp() * 1000) if self.initial_since else 0
        records = self.vitadock.measurements(module, start_ms)
        log.info("%s: %d record(s) since %s", module, len(records),
                 datetime.fromtimestamp(start_ms / 1000, timezone.utc).isoformat())

        done = set(self.state.synced.get(module, []))
        newest = start_ms
        uploaded = 0
        for rec in sorted(records, key=lambda r: pick(r, "measurementDate") or 0):
            rid = record_id(module, rec)
            when = measured_at(rec, self.tz, self.wallclock)
            if self.initial_since and when < self.initial_since:
                continue
            if rid in done or not is_active(rec):
                continue
            if self._upload(module, rec, when):
                uploaded += 1
            if not self.dry_run:
                done.add(rid)
                self.state.synced[module] = sorted(done)
                self.state.save()
            changed = pick(rec, "updatedDate", "updated_date", "measurementDate")
            if isinstance(changed, (int, float)):
                newest = max(newest, int(changed))

        if not self.dry_run:
            self.state.since[module] = max(0, newest - int(OVERLAP.total_seconds() * 1000))
            self.state.save()
        return uploaded

    def _upload(self, module: str, rec: dict[str, Any], when: datetime) -> bool:
        if module == MODULE_TARGETSCALE:
            return self._upload_weight(rec, when)
        if module == MODULE_CARDIODOCK:
            return self._upload_blood_pressure(rec, when)
        return False

    def _upload_weight(self, rec: dict[str, Any], when: datetime) -> bool:
        weight = positive(pick(rec, "bodyWeight", "weight"))
        if weight is None:
            log.warning("skipping scale record without weight: %s", rec)
            return False
        kcal = positive(pick(rec, "kcal", "basalMetabolism"))
        values = {
            "percent_fat": positive(pick(rec, "bodyFat")),
            "percent_hydration": positive(pick(rec, "bodyWater")),
            "muscle_mass": _pct_to_kg(positive(pick(rec, "muscleMass")), weight),
            "bone_mass": positive(pick(rec, "boneMass")),
            "basal_met": kcal,
            "bmi": positive(pick(rec, "bmi")),
        }
        log.info("weight %s: %.2f kg %s", when.isoformat(), weight,
                 {k: v for k, v in values.items() if v is not None})
        if not self.dry_run:
            self.garmin.add_body_composition(when.isoformat(), weight, **values)
        return True

    def _upload_blood_pressure(self, rec: dict[str, Any], when: datetime) -> bool:
        sys_ = positive(pick(rec, "systole", "systolic"))
        dia = positive(pick(rec, "diastole", "diastolic"))
        if sys_ is None or dia is None:
            log.warning("skipping blood pressure record without values: %s", rec)
            return False
        pulse = positive(pick(rec, "pulse"))
        log.info("blood pressure %s: %d/%d pulse %s", when.isoformat(), sys_, dia, pulse)
        if not self.dry_run:
            self.garmin.set_blood_pressure(int(sys_), int(dia), int(pulse) if pulse else None,
                                           timestamp=when.isoformat(), notes="VitaDock")
        return True


def _pct_to_kg(muscle: float | None, weight: float) -> float | None:
    """VitaDock reports muscle mass as a percentage of body weight; Garmin wants kg."""
    if muscle is None:
        return None
    return round(weight * muscle / 100, 2)
