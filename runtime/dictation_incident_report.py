#!/usr/bin/env python3
"""Offline incident trend report for dictation watchdog self-heals.

Reads historical log files only. It does not touch the live service, send
notifications, or mutate any runtime state.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import re
from pathlib import Path
from typing import Iterable


HERE = Path(__file__).resolve().parent
DEFAULT_LOGS = [
    Path("/tmp/dictation.log"),
    HERE / "server.log",
]
TIMESTAMP_RE = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\s+")
TRIP_PREFIX = "watchdog: TRIPPED ("


@dataclasses.dataclass(frozen=True)
class Incident:
    timestamp: dt.datetime
    source: Path
    kind: str
    reason: str
    details: str


def parse_incidents(paths: Iterable[Path]) -> list[Incident]:
    incidents: list[Incident] = []
    for path in paths:
        if not path.exists():
            continue
        with path.open(encoding="utf-8", errors="replace") as fh:
            for raw_line in fh:
                line = raw_line.rstrip("\n")
                ts_match = TIMESTAMP_RE.match(line)
                if not ts_match:
                    continue
                timestamp = dt.datetime.fromisoformat(ts_match.group("ts"))
                rest = line[ts_match.end():]
                if not rest.startswith(TRIP_PREFIX):
                    continue
                body = rest[len(TRIP_PREFIX):]
                reason_body, sep, details = body.partition(") -> ")
                if sep:
                    reason = reason_body.strip()
                    details = details.strip()
                elif body.endswith(")"):
                    reason = body[:-1].strip()
                    details = ""
                else:
                    continue
                kind = "portaudio" if "portaudio-wedge" in reason else "watchdog"
                incidents.append(
                    Incident(
                        timestamp=timestamp,
                        source=path,
                        kind=kind,
                        reason=reason,
                        details=details,
                    )
                )
    incidents.sort(key=lambda incident: (incident.timestamp, str(incident.source)))
    return incidents


def count_in_window(incidents: list[Incident], now: dt.datetime, days: int) -> int:
    threshold = now - dt.timedelta(days=days)
    return sum(1 for incident in incidents if threshold <= incident.timestamp <= now)


def format_age(delta: dt.timedelta) -> str:
    total_minutes = int(delta.total_seconds() // 60)
    days, rem_minutes = divmod(total_minutes, 60 * 24)
    hours, minutes = divmod(rem_minutes, 60)
    return f"{days}d {hours}h {minutes}m"


def build_report(paths: Iterable[Path], now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now()
    incidents = parse_incidents(paths)
    latest = incidents[-1] if incidents else None
    count_1d = count_in_window(incidents, now, 1)
    count_7d = count_in_window(incidents, now, 7)
    count_30d = count_in_window(incidents, now, 30)

    source_rows = []
    for path in paths:
        source_incidents = [inc for inc in incidents if inc.source == path]
        source_rows.append(
            {
                "source": str(path),
                "exists": path.exists(),
                "count_7d": count_in_window(source_incidents, now, 7),
                "count_30d": count_in_window(source_incidents, now, 30),
                "last_incident": source_incidents[-1].timestamp.isoformat(sep=" ")
                if source_incidents
                else None,
            }
        )

    rate_7d = count_7d / 7 if count_7d else 0.0
    rate_30d = count_30d / 30 if count_30d else 0.0
    trend = "flat"
    trend_ratio = None
    if rate_30d > 0:
        trend_ratio = rate_7d / rate_30d
        if trend_ratio > 1.1:
            trend = "up"
        elif trend_ratio < 0.9:
            trend = "down"
    elif rate_7d > 0:
        trend = "up"

    days_since_last = (
        (now - latest.timestamp).total_seconds() / 86400 if latest else None
    )

    return {
        "generated_at": now.isoformat(sep=" "),
        "latest_incident": {
            "timestamp": latest.timestamp.isoformat(sep=" ") if latest else None,
            "source": str(latest.source) if latest else None,
            "kind": latest.kind if latest else None,
            "reason": latest.reason if latest else None,
            "details": latest.details if latest else None,
        },
        "days_since_last_incident": days_since_last,
        "days_since_last_incident_human": format_age(now - latest.timestamp)
        if latest
        else None,
        "incident_count_1d": count_1d,
        "incident_count_7d": count_7d,
        "incident_count_30d": count_30d,
        "rate_7d_per_day": rate_7d,
        "rate_30d_per_day": rate_30d,
        "trend": trend,
        "trend_ratio": trend_ratio,
        "sources": source_rows,
        "total_incidents": len(incidents),
    }


def render_markdown(report: dict) -> str:
    lines = [
        "# Dictation incident trend",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Generated | {report['generated_at']} |",
        f"| Total incidents | {report['total_incidents']} |",
        f"| Days since last incident | {report['days_since_last_incident']:.2f} days ({report['days_since_last_incident_human']}) |",
        f"| Incidents in last 24h | {report['incident_count_1d']} |",
        f"| Incidents in last 7d | {report['incident_count_7d']} |",
        f"| Incidents in last 30d | {report['incident_count_30d']} |",
        f"| 7d rate | {report['rate_7d_per_day']:.2f}/day |",
        f"| 30d rate | {report['rate_30d_per_day']:.2f}/day |",
    ]
    trend = report["trend"]
    if report["trend_ratio"] is None:
        trend_value = trend
    else:
        trend_value = f"{trend} ({report['trend_ratio']:.1f}x vs 30d average)"
    lines.append(f"| Trend | {trend_value} |")
    lines.extend(["", "| Log source | 7d incidents | 30d incidents | Last incident |", "| --- | ---: | ---: | --- |"])
    for row in report["sources"]:
        last = row["last_incident"] or "n/a"
        lines.append(
            f"| {row['source']} | {row['count_7d']} | {row['count_30d']} | {last} |"
        )
    latest = report["latest_incident"]
    lines.extend(
        [
            "",
            "| Latest incident field | Value |",
            "| --- | --- |",
            f"| Source | {latest['source'] or 'n/a'} |",
            f"| Kind | {latest['kind'] or 'n/a'} |",
            f"| Reason | {latest['reason'] or 'n/a'} |",
        ]
    )
    if latest["details"]:
        lines.append(f"| Details | {latest['details']} |")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--log",
        dest="logs",
        action="append",
        type=Path,
        help="extra log file to include; may be given multiple times",
    )
    parser.add_argument(
        "--format",
        choices=("markdown", "json"),
        default="markdown",
        help="output format",
    )
    args = parser.parse_args()

    logs = args.logs if args.logs else DEFAULT_LOGS
    report = build_report(logs)

    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
