#!/usr/bin/env python3
"""
Samsara Fleet Health Report

Pulls a live snapshot of every vehicle from the Samsara API (GPS location,
engine state, fuel level and engine fault codes) and turns it into a
prioritized list a maintenance or dispatch manager can act on:

  * CRITICAL - J1939 stop/protect lamp on (pull the vehicle off the road)
  * WARNING  - active fault codes or check-engine light, extended idling,
               or low fuel

Runs against the real API when SAMSARA_API_TOKEN is set, or against
simulated data (mock_data/vehicle_stats.json) shaped like real API
responses, so it can be demoed without a customer org.

Standard library only - no installs needed.

Usage:
    python fleet_health.py                  # mock data, console + HTML report
    SAMSARA_API_TOKEN=xxx python fleet_health.py --live
    python fleet_health.py --idle-minutes 15 --low-fuel 25 --html report.html
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

API_BASE = "https://api.samsara.com"
STATS_ENDPOINT = "/fleet/vehicles/stats"
STAT_TYPES = "gps,engineStates,fuelPercents,faultCodes"
MOCK_FILE = Path(__file__).parent / "mock_data" / "vehicle_stats.json"

SEVERITY_ORDER = {"CRITICAL": 0, "WARNING": 1, "OK": 2}


# --------------------------------------------------------------------------
# Data access
# --------------------------------------------------------------------------

def fetch_vehicle_stats(token: str, max_retries: int = 3) -> list[dict]:
    """Call GET /fleet/vehicles/stats and follow pagination until done.

    Retries on HTTP 429 (rate limit) with exponential backoff.
    """
    vehicles: list[dict] = []
    cursor = ""
    while True:
        params = {"types": STAT_TYPES}
        if cursor:
            params["after"] = cursor
        url = f"{API_BASE}{STATS_ENDPOINT}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )

        for attempt in range(max_retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    page = json.load(resp)
                break
            except urllib.error.HTTPError as err:
                if err.code == 429 and attempt < max_retries:
                    wait = 2 ** attempt
                    print(f"Rate limited; retrying in {wait}s...", file=sys.stderr)
                    time.sleep(wait)
                    continue
                if err.code == 401:
                    sys.exit("401 Unauthorized: check SAMSARA_API_TOKEN and that it has "
                             "the 'Read Vehicle Statistics' scope.")
                raise

        vehicles.extend(page.get("data", []))
        pagination = page.get("pagination", {})
        if not pagination.get("hasNextPage"):
            return vehicles
        cursor = pagination.get("endCursor", "")


def load_mock_stats(path: Path = MOCK_FILE) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)["data"]


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------

@dataclass
class FaultCode:
    code: str
    description: str
    kind: str  # e.g. "J1939", "OBD-II confirmed", "OBD-II pending"


@dataclass
class VehicleHealth:
    vehicle_id: str
    name: str
    location: str
    speed_mph: float | None
    engine_state: str
    fuel_percent: float | None
    faults: list[FaultCode] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    severity: str = "OK"


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def extract_faults(fault_reading: dict | None) -> tuple[list[FaultCode], dict]:
    """Normalize J1939 and OBD-II fault payloads into one list.

    Returns (faults, lamps) where lamps holds warning-light status.
    """
    faults: list[FaultCode] = []
    lamps = {"stop": False, "protect": False, "warning": False, "check_engine": False}
    if not fault_reading:
        return faults, lamps

    j1939 = fault_reading.get("j1939")
    if j1939:
        lights = j1939.get("checkEngineLights", {})
        lamps["stop"] = bool(lights.get("stopIsOn"))
        lamps["protect"] = bool(lights.get("protectIsOn"))
        lamps["warning"] = bool(lights.get("warningIsOn") or lights.get("emissionsIsOn"))
        for dtc in j1939.get("diagnosticTroubleCodes", []):
            faults.append(FaultCode(
                code=f"SPN {dtc.get('spnId')} / FMI {dtc.get('fmiId')}",
                description=f"{dtc.get('spnDescription', '')} - {dtc.get('fmiDescription', '')}".strip(" -"),
                kind="J1939",
            ))

    obdii = fault_reading.get("obdii")
    if obdii:
        lamps["check_engine"] = bool(obdii.get("checkEngineLightIsOn"))
        for reading in obdii.get("diagnosticTroubleCodes", []):
            for bucket, label in (("confirmedDtcs", "confirmed"),
                                  ("pendingDtcs", "pending"),
                                  ("permanentDtcs", "permanent")):
                for dtc in reading.get(bucket, []):
                    faults.append(FaultCode(
                        code=dtc.get("dtcShortCode", str(dtc.get("dtcId"))),
                        description=dtc.get("dtcDescription", ""),
                        kind=f"OBD-II {label}",
                    ))
    return faults, lamps


def assess_vehicle(raw: dict, now: datetime, idle_minutes: int, low_fuel: float) -> VehicleHealth:
    gps = raw.get("gps") or {}
    engine = raw.get("engineStates") or {}
    fuel = raw.get("fuelPercents") or {}

    vh = VehicleHealth(
        vehicle_id=raw.get("id", ""),
        name=raw.get("name", "Unnamed vehicle"),
        location=(gps.get("reverseGeo") or {}).get("formattedLocation")
                 or (f"{gps['latitude']:.4f}, {gps['longitude']:.4f}" if "latitude" in gps else "Unknown"),
        speed_mph=gps.get("speedMilesPerHour"),
        engine_state=engine.get("value", "Unknown"),
        fuel_percent=fuel.get("value"),
    )

    vh.faults, lamps = extract_faults(raw.get("faultCodes"))
    critical = False

    if lamps["stop"]:
        vh.issues.append("STOP lamp on - remove from service")
        critical = True
    if lamps["protect"]:
        vh.issues.append("Protect lamp on - engine protection active")
        critical = True
    if lamps["warning"] or lamps["check_engine"]:
        vh.issues.append("Warning / check-engine light on")
    if vh.faults:
        vh.issues.append(f"{len(vh.faults)} active fault code(s)")

    if vh.engine_state == "Idle":
        since = parse_time(engine.get("time"))
        if since:
            idle_for = int((now - since).total_seconds() // 60)
            if idle_for >= idle_minutes:
                vh.issues.append(f"Idling {idle_for} min (wasting fuel)")

    if vh.fuel_percent is not None and vh.fuel_percent < low_fuel:
        vh.issues.append(f"Low fuel ({vh.fuel_percent:.0f}%)")

    if critical:
        vh.severity = "CRITICAL"
    elif vh.issues:
        vh.severity = "WARNING"
    return vh


def build_report(raw_vehicles: list[dict], now: datetime,
                 idle_minutes: int = 10, low_fuel: float = 20) -> list[VehicleHealth]:
    report = [assess_vehicle(v, now, idle_minutes, low_fuel) for v in raw_vehicles]
    report.sort(key=lambda v: (SEVERITY_ORDER[v.severity], v.name))
    return report


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def print_console(report: list[VehicleHealth], generated: datetime, source: str) -> None:
    counts = {s: sum(1 for v in report if v.severity == s) for s in SEVERITY_ORDER}
    print(f"\nSAMSARA FLEET HEALTH REPORT  ({source}, {generated:%Y-%m-%d %H:%M} UTC)")
    print(f"{len(report)} vehicles | {counts['CRITICAL']} critical | "
          f"{counts['WARNING']} warning | {counts['OK']} ok\n")
    for v in report:
        fuel = f"{v.fuel_percent:.0f}%" if v.fuel_percent is not None else "n/a"
        speed = f"{v.speed_mph:.0f} mph" if v.speed_mph is not None else "n/a"
        print(f"[{v.severity:<8}] {v.name}")
        print(f"            {v.location} | engine {v.engine_state} | {speed} | fuel {fuel}")
        for issue in v.issues:
            print(f"            - {issue}")
        for f in v.faults:
            print(f"              {f.kind}: {f.code}  {f.description}")
        print()


def write_html(report: list[VehicleHealth], generated: datetime, source: str, path: Path) -> None:
    colors = {"CRITICAL": "#c62828", "WARNING": "#ef6c00", "OK": "#2e7d32"}
    rows = []
    for v in report:
        issues = "<br>".join(html.escape(i) for i in v.issues) or "No issues"
        faults = "<br>".join(
            f"<code>{html.escape(f.code)}</code> {html.escape(f.description)} <small>({html.escape(f.kind)})</small>"
            for f in v.faults) or "-"
        fuel = f"{v.fuel_percent:.0f}%" if v.fuel_percent is not None else "n/a"
        speed = f"{v.speed_mph:.0f}" if v.speed_mph is not None else "n/a"
        rows.append(
            f"<tr><td><span class='pill' style='background:{colors[v.severity]}'>{v.severity}</span></td>"
            f"<td><b>{html.escape(v.name)}</b><br><small>{html.escape(v.location)}</small></td>"
            f"<td>{html.escape(v.engine_state)}</td><td>{speed}</td><td>{fuel}</td>"
            f"<td>{issues}</td><td>{faults}</td></tr>")
    counts = {s: sum(1 for v in report if v.severity == s) for s in SEVERITY_ORDER}
    doc = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Fleet Health Report</title>
<style>
 body {{ font-family: system-ui, sans-serif; margin: 2rem; color: #222; }}
 h1 {{ margin-bottom: .2rem; }} .meta {{ color: #666; margin-bottom: 1rem; }}
 .cards {{ display: flex; gap: 1rem; margin-bottom: 1.5rem; }}
 .card {{ padding: .8rem 1.2rem; border-radius: 8px; background: #f5f5f5; min-width: 110px; }}
 .card b {{ font-size: 1.6rem; display: block; }}
 table {{ border-collapse: collapse; width: 100%; }}
 th, td {{ text-align: left; padding: .6rem; border-bottom: 1px solid #e0e0e0; vertical-align: top; }}
 th {{ background: #fafafa; }}
 .pill {{ color: #fff; padding: 2px 8px; border-radius: 10px; font-size: .8rem; font-weight: 600; }}
</style></head><body>
<h1>Fleet Health Report</h1>
<div class="meta">Source: {html.escape(source)} &middot; Generated {generated:%Y-%m-%d %H:%M} UTC</div>
<div class="cards">
 <div class="card"><b>{len(report)}</b>Vehicles</div>
 <div class="card" style="color:{colors['CRITICAL']}"><b>{counts['CRITICAL']}</b>Critical</div>
 <div class="card" style="color:{colors['WARNING']}"><b>{counts['WARNING']}</b>Warning</div>
 <div class="card" style="color:{colors['OK']}"><b>{counts['OK']}</b>OK</div>
</div>
<table><tr><th>Status</th><th>Vehicle</th><th>Engine</th><th>Speed (mph)</th><th>Fuel</th><th>Issues</th><th>Fault codes</th></tr>
{''.join(rows)}
</table></body></html>"""
    path.write_text(doc, encoding="utf-8")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Samsara fleet health report")
    parser.add_argument("--live", action="store_true",
                        help="call the real Samsara API (needs SAMSARA_API_TOKEN)")
    parser.add_argument("--idle-minutes", type=int, default=10,
                        help="flag vehicles idling at least this long (default 10)")
    parser.add_argument("--low-fuel", type=float, default=20,
                        help="flag vehicles below this fuel %% (default 20)")
    parser.add_argument("--html", default="fleet_health_report.html",
                        help="HTML report path (default fleet_health_report.html)")
    args = parser.parse_args(argv)

    if args.live:
        token = os.environ.get("SAMSARA_API_TOKEN")
        if not token:
            sys.exit("Set SAMSARA_API_TOKEN to use --live.")
        raw = fetch_vehicle_stats(token)
        now = datetime.now(timezone.utc)
        source = "Samsara API (live)"
    else:
        raw = load_mock_stats()
        # Anchor "now" to the newest GPS ping so the demo output is stable.
        now = max(parse_time(v["gps"]["time"]) for v in raw if v.get("gps"))
        source = "simulated data"

    report = build_report(raw, now, args.idle_minutes, args.low_fuel)
    print_console(report, now, source)
    write_html(report, now, source, Path(args.html))
    print(f"HTML report written to {args.html}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
