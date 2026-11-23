import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fleet_health as fh  # noqa: E402


class FleetHealthTests(unittest.TestCase):
    def setUp(self):
        self.raw = fh.load_mock_stats()
        self.now = max(fh.parse_time(v["gps"]["time"]) for v in self.raw)
        self.report = {v.name: v for v in fh.build_report(self.raw, self.now)}

    def test_stop_lamp_is_critical(self):
        truck = self.report["Truck 103 - Greensboro Hub"]
        self.assertEqual(truck.severity, "CRITICAL")
        self.assertTrue(any("STOP lamp" in i for i in truck.issues))

    def test_obdii_confirmed_and_pending_codes_parsed(self):
        van = self.report["Van 201 - Durham Service"]
        codes = {f.code for f in van.faults}
        self.assertEqual(codes, {"P0300", "P0171"})
        self.assertEqual(van.severity, "WARNING")

    def test_low_fuel_flagged(self):
        truck = self.report["Truck 102 - I-40 Westbound"]
        self.assertIn("Low fuel (15%)", truck.issues)

    def test_idle_time_flagged(self):
        truck = self.report["Truck 101 - Raleigh Depot"]
        self.assertTrue(any(i.startswith("Idling 22 min") for i in truck.issues))

    def test_healthy_vehicle_ok(self):
        self.assertEqual(self.report["Van 202 - Cary Field Tech"].severity, "OK")

    def test_critical_sorted_first(self):
        ordered = fh.build_report(self.raw, self.now)
        self.assertEqual(ordered[0].severity, "CRITICAL")


if __name__ == "__main__":
    unittest.main()
