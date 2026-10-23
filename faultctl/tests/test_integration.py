"""Opt-in integration tests against the LOCAL running demo.

Run only with FAULTCTL_INTEGRATION=1 and the demo started via docker compose.
Each test injects one incident and always resets it afterwards.
"""

import os
import unittest

from faultctl import cli
from faultctl.operations import Status

ENABLED = os.environ.get("FAULTCTL_INTEGRATION") == "1"


@unittest.skipUnless(ENABLED, "set FAULTCTL_INTEGRATION=1 to run against the local demo")
class LocalDemoIntegrationTests(unittest.TestCase):
    def setUp(self):
        args = cli.build_parser().parse_args(["status"])
        self.controller = cli.default_controller(args)

    def _roundtrip(self, incident_id, variant=None):
        try:
            r = self.controller.inject(incident_id, variant)
            self.assertEqual(r.status, Status.OK, r.message)
            again = self.controller.inject(incident_id, variant)
            self.assertEqual(again.status, Status.OK, again.message)
            self.assertIn("no change", again.message)
        finally:
            r = self.controller.reset(incident_id)
        self.assertEqual(r.status, Status.OK, r.message)
        self.assertEqual(self.controller.reset(incident_id).status, Status.OK)

    def test_payment_failure_roundtrip(self):
        self._roundtrip("payment-failure", "100%")

    def test_product_catalog_targeting_roundtrip(self):
        self._roundtrip("product-catalog-failure")

    def test_cache_unavailable_roundtrip(self):
        self._roundtrip("cache-unavailable")


if __name__ == "__main__":
    unittest.main()
