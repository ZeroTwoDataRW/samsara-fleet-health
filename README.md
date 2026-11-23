# Samsara Fleet Health Report

A Python tool that uses the [Samsara API](https://developers.samsara.com/docs) to pull a live snapshot of every vehicle in a fleet: GPS location, engine state, fuel level and engine fault codes. It turns that data into a prioritized list a maintenance or dispatch manager can act on right away.

**The customer problem:** a fleet manager starts the day with dozens of trucks on the road and no quick way to see which ones need attention. This report answers "which vehicles do I deal with first, and why?" in one screen.

## What it flags

| Severity | Trigger | Why it matters |
| --- | --- | --- |
| CRITICAL | J1939 **stop** or **protect** lamp on | Engine damage risk; pull the vehicle off the road |
| WARNING | Active fault codes (J1939 SPN/FMI or OBD-II DTCs) or check-engine light | Schedule maintenance before it becomes a breakdown |
| WARNING | Engine idling longer than a threshold (default 10 min) | Wasted fuel and emissions |
| WARNING | Fuel below a threshold (default 20%) | Prevent a stranded vehicle |

It handles both heavy-duty trucks (**J1939**, SPN/FMI codes) and light-duty vans (**OBD-II**, P-codes), normalizing them into one list.

## Sample output

```
SAMSARA FLEET HEALTH REPORT  (simulated data, 2026-11-23 18:42 UTC)
5 vehicles | 1 critical | 3 warning | 1 ok

[CRITICAL] Truck 103 - Greensboro Hub
            Greensboro, NC | engine Idle | 0 mph | fuel 9%
            - STOP lamp on - remove from service
            - Warning / check-engine light on
            - 1 active fault code(s)
            - Idling 47 min (wasting fuel)
            - Low fuel (9%)
              J1939: SPN 100 / FMI 1  Engine Oil Pressure - Data valid but below normal operational range - most severe level

[WARNING ] Van 201 - Durham Service
            Durham, NC | engine On | 28 mph | fuel 48%
            - Warning / check-engine light on
            - 2 active fault code(s)
              OBD-II confirmed: P0300  Random/Multiple Cylinder Misfire Detected
              OBD-II pending: P0171  System Too Lean (Bank 1)
...
```

It also writes `fleet_health_report.html`, a shareable dashboard with summary cards and a sortable status table.

## Run it

Requires Python 3.9+. Standard library only, so there's nothing to install.

```bash
# Demo mode: simulated data shaped like real Samsara API responses
python fleet_health.py

# Live mode: real fleet data
export SAMSARA_API_TOKEN=your_token_here
python fleet_health.py --live

# Custom thresholds
python fleet_health.py --idle-minutes 15 --low-fuel 25 --html today.html

# Tests
python -m unittest discover -s tests -v
```

**API token:** in the Samsara dashboard go to Settings → API Tokens → Add an API Token, and give it the **Read Vehicle Statistics** scope.

## How it works

1. **One API call per page:** `GET /fleet/vehicles/stats?types=gps,engineStates,fuelPercents,faultCodes` returns the last known value of every stat for every vehicle. Combining types in a single call keeps the request count low against Samsara's rate limits.
2. **Pagination:** follows `pagination.endCursor` / `hasNextPage` until all vehicles are retrieved.
3. **Resilience:** retries HTTP 429 (rate limited) with exponential backoff and gives a clear message on 401 (bad token or missing scope).
4. **Normalization:** J1939 and OBD-II fault payloads have different shapes; both are converted to one `FaultCode` model.
5. **Rules:** each vehicle is scored and the list is sorted with critical vehicles first.

## Project layout

```
fleet_health.py              # API client, analysis rules, console + HTML output
mock_data/vehicle_stats.json # simulated 5-vehicle NC fleet (J1939 trucks + OBD-II vans)
tests/test_fleet_health.py   # unit tests for parsing and alert rules
```

## Next steps

- Use `/fleet/vehicles/stats/feed` to stream changes in near real time instead of taking snapshots.
- Push critical alerts to Slack or email, or create work orders in a maintenance system through its API.
- Use Samsara webhooks so alerts fire on events without polling.
