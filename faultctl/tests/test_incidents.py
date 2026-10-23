import json
import os
import re
import unittest
from pathlib import Path

from faultctl.incidents import CONTAINER_STOP, FLAG, INCIDENTS, OFF

from fakes import FakeDocker, FakeFlagdUi, make_controller

DEMO_DIR = Path(os.environ.get("FAULTCTL_DEMO_DIR", Path(__file__).resolve().parents[2]))


class CatalogTests(unittest.TestCase):
    def test_ids_are_stable_slugs(self):
        for inc_id, inc in INCIDENTS.items():
            self.assertEqual(inc_id, inc.id)
            self.assertRegex(inc_id, r"^[a-z0-9]+(-[a-z0-9]+)*$")

    def test_entries_are_complete(self):
        for inc in INCIDENTS.values():
            self.assertIn(inc.mechanism, (FLAG, CONTAINER_STOP))
            self.assertTrue(inc.summary and inc.target and inc.effects and inc.verification)
            if inc.mechanism == FLAG:
                self.assertTrue(inc.flag)
                self.assertIn(inc.default_variant, inc.variants)
                self.assertNotIn(OFF, inc.variants)
                self.assertIsNone(inc.container)
            else:
                self.assertTrue(inc.container)
                self.assertIsNone(inc.flag)


@unittest.skipUnless((DEMO_DIR / "src/flagd/demo.flagd.json").exists(), "demo checkout not found")
class CatalogMatchesDemoTests(unittest.TestCase):
    """The catalog must only reference mechanisms that exist in the real demo checkout."""

    def test_flags_and_variants_exist(self):
        flags = json.loads((DEMO_DIR / "src/flagd/demo.flagd.json").read_text())["flags"]
        for inc in INCIDENTS.values():
            if inc.mechanism != FLAG:
                continue
            self.assertIn(inc.flag, flags, inc.id)
            for v in (*inc.variants, OFF):
                self.assertIn(v, flags[inc.flag]["variants"], f"{inc.id} variant {v}")
            if inc.targeting_rule:
                self.assertEqual(len(flags[inc.flag]["targeting"]["if"]), 3)

    def test_containers_exist_in_compose(self):
        compose = "".join(
            (DEMO_DIR / f).read_text() for f in ("compose.yaml", "compose.full.yaml") if (DEMO_DIR / f).exists()
        )
        names = set(re.findall(r"container_name:\s*([\w-]+)", compose))
        for inc in INCIDENTS.values():
            for c in (inc.container, *inc.requires):
                if c:
                    self.assertIn(c, names, inc.id)


class ListTests(unittest.TestCase):
    def test_availability_all_yes_when_demo_running(self):
        avail = make_controller().availability()
        self.assertEqual(set(avail), set(INCIDENTS))
        self.assertEqual(avail["payment-failure"], "yes")
        self.assertEqual(avail["cache-unavailable"], "yes")

    def test_kafka_incidents_unavailable_without_full_stack(self):
        avail = make_controller().availability()
        self.assertTrue(avail["kafka-queue-problems"].startswith("no ("))
        self.assertIn("kafka", avail["kafka-queue-problems"])

    def test_missing_flag_is_reported_unavailable(self):
        ui = FakeFlagdUi()
        del ui.flags["paymentFailure"]
        avail = make_controller(ui=ui).availability()
        self.assertIn("not defined", avail["payment-failure"])

    def test_docker_down_makes_availability_unknown(self):
        from fakes import AdapterUnavailable

        docker = FakeDocker()
        docker.ping_exc = AdapterUnavailable("daemon down")
        avail = make_controller(docker=docker).availability()
        self.assertTrue(all(v.startswith("unknown") for v in avail.values()))


if __name__ == "__main__":
    unittest.main()
