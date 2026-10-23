import unittest

from faultctl.incidents import FLAG, INCIDENTS
from faultctl.operations import EXIT_PARTIAL, Status, exit_code

from fakes import AdapterError, AdapterUnavailable, FakeDocker, FakeFlagdUi, make_controller


class ResetTests(unittest.TestCase):
    def test_reset_known_flag_incident(self):
        ui = FakeFlagdUi()
        c = make_controller(ui=ui)
        c.inject("payment-failure", "100%")
        r = c.reset("payment-failure")
        self.assertEqual(r.status, Status.OK, r.message)
        self.assertEqual(ui.flags["paymentFailure"]["defaultVariant"], "off")

    def test_reset_targeting_rule(self):
        ui = FakeFlagdUi()
        c = make_controller(ui=ui)
        c.inject("product-catalog-failure")
        r = c.reset("product-catalog-failure")
        self.assertEqual(r.status, Status.OK, r.message)
        self.assertEqual(ui.flags["productCatalogFailure"]["targeting"]["if"][1], "off")

    def test_reset_unknown_incident(self):
        r = make_controller().reset("not-a-thing")
        self.assertEqual(r.status, Status.INVALID)

    def test_repeated_reset_is_idempotent(self):
        ui = FakeFlagdUi()
        docker = FakeDocker()
        c = make_controller(ui=ui, docker=docker)
        c.inject("payment-failure")
        c.inject("cache-unavailable")
        for _ in range(3):
            self.assertEqual(c.reset("payment-failure").status, Status.OK)
            self.assertEqual(c.reset("cache-unavailable").status, Status.OK)
        self.assertEqual(ui.writes, 2)
        self.assertEqual(docker.calls, [("stop", "valkey-cart"), ("start", "valkey-cart")])

    def test_reset_container_start_failure(self):
        docker = FakeDocker()
        docker.containers["currency"].status = "exited"
        docker.start_exc = AdapterError("boom")
        r = make_controller(docker=docker).reset("currency-down")
        self.assertEqual(r.status, Status.FAILED)

    def test_reset_container_waits_for_health(self):
        docker = FakeDocker()
        docker.containers["astronomy-db"].status = "running"
        docker.containers["astronomy-db"].health = "starting"
        r = make_controller(docker=docker).reset("database-unavailable")
        self.assertEqual(r.status, Status.UNVERIFIED)
        self.assertEqual(docker.calls, [])

    def test_reset_dry_run(self):
        ui = FakeFlagdUi()
        ui.flags["paymentFailure"]["defaultVariant"] = "50%"
        r = make_controller(ui=ui).reset("payment-failure", dry_run=True)
        self.assertEqual(r.status, Status.PLANNED)
        self.assertEqual(ui.flags["paymentFailure"]["defaultVariant"], "50%")


class ResetAllTests(unittest.TestCase):
    def test_reset_all_restores_everything_in_one_flag_write(self):
        ui = FakeFlagdUi()
        docker = FakeDocker()
        c = make_controller(ui=ui, docker=docker)
        c.inject("payment-failure")
        c.inject("ad-high-cpu")
        c.inject("currency-down")
        writes_before = ui.writes
        results = c.reset_all()
        self.assertEqual(exit_code(results), 0, [r.message for r in results if r.status != Status.OK])
        self.assertEqual({r.incident for r in results}, set(INCIDENTS))
        self.assertEqual(ui.writes, writes_before + 1)
        for inc in INCIDENTS.values():
            if inc.mechanism == FLAG:
                self.assertEqual(ui.flags[inc.flag]["defaultVariant"], "off")
        self.assertEqual(docker.containers["currency"].status, "running")

    def test_only_allowlisted_containers_touched(self):
        docker = FakeDocker()
        for c in docker.containers.values():
            c.status = "exited"
        docker.containers["flagd"].status = "running"
        docker.containers["flagd-ui"].status = "running"
        docker.containers["frontend-proxy"].status = "running"
        make_controller(docker=docker).reset_all()
        self.assertEqual({n for _, n in docker.calls}, {"valkey-cart", "astronomy-db", "currency"})
        self.assertEqual(docker.containers["load-generator"].status, "exited")

    def test_partial_failure_reported(self):
        docker = FakeDocker()
        docker.containers["currency"].status = "exited"
        docker.start_exc = AdapterError("cannot start")
        results = make_controller(docker=docker).reset_all()
        failed = [r for r in results if r.status == Status.FAILED]
        self.assertEqual([r.incident for r in failed], ["currency-down"])
        self.assertEqual(exit_code(results), EXIT_PARTIAL)

    def test_flagd_ui_down_still_resets_containers(self):
        ui = FakeFlagdUi()
        ui.read_exc = AdapterUnavailable("flagd-ui down")
        docker = FakeDocker()
        docker.containers["currency"].status = "exited"
        results = make_controller(ui=ui, docker=docker).reset_all()
        by_id = {r.incident: r for r in results}
        self.assertEqual(by_id["payment-failure"].status, Status.UNAVAILABLE)
        self.assertEqual(by_id["currency-down"].status, Status.OK)
        self.assertEqual(exit_code(results), EXIT_PARTIAL)

    def test_reset_all_dry_run_changes_nothing(self):
        ui = FakeFlagdUi()
        docker = FakeDocker()
        docker.containers["currency"].status = "exited"
        results = make_controller(ui=ui, docker=docker).reset_all(dry_run=True)
        self.assertTrue(all(r.status == Status.PLANNED for r in results))
        self.assertEqual(ui.writes, 0)
        self.assertEqual(docker.calls, [])


if __name__ == "__main__":
    unittest.main()
