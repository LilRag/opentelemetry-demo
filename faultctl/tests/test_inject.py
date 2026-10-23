import unittest

from faultctl.operations import Status

from fakes import (
    AdapterError,
    AdapterTimeout,
    AdapterUnavailable,
    FakeDocker,
    FakeEvaluator,
    FakeFlagdUi,
    make_controller,
)


class FlagInjectTests(unittest.TestCase):
    def test_success_is_verified_through_flagd(self):
        ui = FakeFlagdUi()
        r = make_controller(ui=ui).inject("payment-failure", "100%")
        self.assertEqual(r.status, Status.OK, r.message)
        self.assertEqual(ui.flags["paymentFailure"]["defaultVariant"], "100%")
        self.assertIn("flagd confirms", r.message)

    def test_only_target_flag_changes(self):
        ui = FakeFlagdUi()
        before = ui.read_flags()
        make_controller(ui=ui).inject("cart-failure")
        after = ui.read_flags()
        before["cartFailure"]["defaultVariant"] = "50%"
        self.assertEqual(before, after)

    def test_targeting_rule_flag(self):
        ui = FakeFlagdUi()
        r = make_controller(ui=ui).inject("product-catalog-failure")
        self.assertEqual(r.status, Status.OK, r.message)
        self.assertEqual(ui.flags["productCatalogFailure"]["targeting"]["if"][1], "on")
        self.assertEqual(ui.flags["productCatalogFailure"]["defaultVariant"], "off")

    def test_unknown_incident(self):
        ui = FakeFlagdUi()
        r = make_controller(ui=ui).inject("drop-database")
        self.assertEqual(r.status, Status.INVALID)
        self.assertEqual(ui.writes, 0)

    def test_disallowed_variant(self):
        ui = FakeFlagdUi()
        r = make_controller(ui=ui).inject("payment-failure", "off")
        self.assertEqual(r.status, Status.INVALID)
        r = make_controller(ui=ui).inject("payment-failure", "33%")
        self.assertEqual(r.status, Status.INVALID)
        self.assertEqual(ui.writes, 0)

    def test_variant_on_container_incident_rejected(self):
        r = make_controller().inject("cache-unavailable", "on")
        self.assertEqual(r.status, Status.INVALID)

    def test_mechanism_missing_from_demo(self):
        ui = FakeFlagdUi()
        del ui.flags["adHighCpu"]
        r = make_controller(ui=ui).inject("ad-high-cpu")
        self.assertEqual(r.status, Status.UNAVAILABLE)
        self.assertEqual(ui.writes, 0)

    def test_required_containers_missing(self):
        ui = FakeFlagdUi()
        r = make_controller(ui=ui).inject("kafka-queue-problems")
        self.assertEqual(r.status, Status.UNAVAILABLE)
        self.assertEqual(ui.writes, 0)

    def test_write_failure(self):
        ui = FakeFlagdUi()
        ui.write_exc = AdapterError("HTTP 500")
        r = make_controller(ui=ui).inject("payment-failure")
        self.assertEqual(r.status, Status.FAILED)

    def test_write_timeout_is_unknown_not_retried(self):
        ui = FakeFlagdUi()
        ui.write_exc = AdapterTimeout("timed out")
        r = make_controller(ui=ui).inject("payment-failure")
        self.assertEqual(r.status, Status.UNKNOWN)
        self.assertEqual(ui.writes, 1)

    def test_write_not_persisted_is_unknown(self):
        ui = FakeFlagdUi()
        ui.drop_writes = True
        r = make_controller(ui=ui).inject("payment-failure")
        self.assertEqual(r.status, Status.UNKNOWN)

    def test_flagd_not_serving_new_value_is_unverified(self):
        ui = FakeFlagdUi()
        ev = FakeEvaluator(ui)
        ev.stale = True
        r = make_controller(ui=ui, evaluator=ev).inject("payment-failure")
        self.assertEqual(r.status, Status.UNVERIFIED)
        self.assertIn("still evaluates", r.message)

    def test_flagd_unreachable_is_unverified(self):
        ui = FakeFlagdUi()
        ev = FakeEvaluator(ui)
        ev.exc = AdapterUnavailable("connection refused")
        r = make_controller(ui=ui, evaluator=ev).inject("payment-failure")
        self.assertEqual(r.status, Status.UNVERIFIED)

    def test_flagd_ui_unreachable(self):
        ui = FakeFlagdUi()
        ui.read_exc = AdapterUnavailable("cannot reach")
        r = make_controller(ui=ui).inject("payment-failure")
        self.assertEqual(r.status, Status.UNAVAILABLE)

    def test_already_active_makes_no_write(self):
        ui = FakeFlagdUi()
        ui.flags["paymentFailure"]["defaultVariant"] = "50%"
        r = make_controller(ui=ui).inject("payment-failure", "50%")
        self.assertEqual(r.status, Status.OK)
        self.assertEqual(ui.writes, 0)
        self.assertIn("no change", r.message)

    def test_dry_run_changes_nothing(self):
        ui = FakeFlagdUi()
        docker = FakeDocker()
        r = make_controller(ui=ui, docker=docker).inject("payment-failure", dry_run=True)
        self.assertEqual(r.status, Status.PLANNED)
        self.assertTrue(all(s.startswith("PLAN:") for s in r.steps))
        self.assertEqual(ui.writes, 0)
        r = make_controller(ui=ui, docker=docker).inject("cache-unavailable", dry_run=True)
        self.assertEqual(r.status, Status.PLANNED)
        self.assertEqual(docker.calls, [])


class ContainerInjectTests(unittest.TestCase):
    def test_stop_and_verify(self):
        docker = FakeDocker()
        r = make_controller(docker=docker).inject("cache-unavailable")
        self.assertEqual(r.status, Status.OK, r.message)
        self.assertEqual(docker.calls, [("stop", "valkey-cart")])
        self.assertEqual(docker.containers["valkey-cart"].status, "exited")

    def test_already_stopped(self):
        docker = FakeDocker()
        docker.containers["valkey-cart"].status = "exited"
        r = make_controller(docker=docker).inject("cache-unavailable")
        self.assertEqual(r.status, Status.OK)
        self.assertEqual(docker.calls, [])

    def test_stop_failure(self):
        docker = FakeDocker()
        docker.stop_exc = AdapterError("permission denied")
        r = make_controller(docker=docker).inject("currency-down")
        self.assertEqual(r.status, Status.FAILED)

    def test_stop_timeout_is_unknown(self):
        docker = FakeDocker()
        docker.stop_exc = AdapterTimeout("timed out")
        r = make_controller(docker=docker).inject("currency-down")
        self.assertEqual(r.status, Status.UNKNOWN)

    def test_docker_missing(self):
        docker = FakeDocker()
        docker.ping_exc = AdapterUnavailable("docker CLI not found on PATH")
        r = make_controller(docker=docker).inject("currency-down")
        self.assertEqual(r.status, Status.UNAVAILABLE)
        self.assertEqual(docker.calls, [])


class TargetSafetyTests(unittest.TestCase):
    def test_container_from_other_project_refused(self):
        docker = FakeDocker()
        docker.containers["valkey-cart"].labels["com.docker.compose.project"] = "other-team"
        r = make_controller(docker=docker).inject("cache-unavailable")
        self.assertEqual(r.status, Status.REFUSED)
        self.assertEqual(docker.calls, [])

    def test_flag_control_plane_from_other_project_refused(self):
        ui = FakeFlagdUi()
        docker = FakeDocker()
        docker.containers["flagd-ui"].labels["com.docker.compose.project"] = "someone-else"
        r = make_controller(ui=ui, docker=docker).inject("payment-failure")
        self.assertEqual(r.status, Status.REFUSED)
        self.assertEqual(ui.writes, 0)

    def test_remote_docker_host_refused(self):
        docker = FakeDocker()
        r = make_controller(docker=docker, env={"DOCKER_HOST": "tcp://10.0.0.5:2375"}).inject("currency-down")
        self.assertEqual(r.status, Status.REFUSED)
        self.assertEqual(docker.calls, [])

    def test_demo_dir_mismatch_refused(self):
        docker = FakeDocker()
        docker.containers["currency"].labels["com.docker.compose.project.working_dir"] = "/elsewhere"
        r = make_controller(docker=docker, demo_dir="/my/demo").inject("currency-down")
        self.assertEqual(r.status, Status.REFUSED)


if __name__ == "__main__":
    unittest.main()
