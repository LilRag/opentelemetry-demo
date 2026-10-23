import io
import unittest
from contextlib import redirect_stderr, redirect_stdout

from faultctl import cli
from faultctl.operations import EXIT_ABORTED, EXIT_PARTIAL

from fakes import AdapterError, AdapterUnavailable, FakeDocker, FakeFlagdUi, make_controller


def run(argv, ui=None, docker=None, answer="n"):
    ui = ui or FakeFlagdUi()
    docker = docker or FakeDocker()
    created = []

    def factory(args):
        created.append(args)
        return make_controller(ui=ui, docker=docker)

    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(io.StringIO()):
        code = cli.main(argv, controller_factory=factory, out=out, input_fn=lambda _p: answer)
    return code, out.getvalue(), ui, docker, created


class HelpTests(unittest.TestCase):
    def test_help(self):
        code, out, *_ = run(["--help"])
        self.assertEqual(code, 0)
        for word in ("list", "inject", "status", "reset", "reset-all", "examples", "exit codes"):
            self.assertIn(word, out)

    def test_no_command_is_usage_error(self):
        code, *_ = run([])
        self.assertEqual(code, 2)


class ListAndShowTests(unittest.TestCase):
    def test_list(self):
        code, out, *_ = run(["list"])
        self.assertEqual(code, 0)
        self.assertIn("payment-failure", out)
        self.assertIn("cache-unavailable", out)
        self.assertIn("yes", out)

    def test_list_no_probe_does_not_build_controller(self):
        code, out, _, _, created = run(["list", "--no-probe"])
        self.assertEqual(code, 0)
        self.assertEqual(created, [])
        self.assertIn("not probed", out)

    def test_show(self):
        code, out, *_ = run(["show", "product-catalog-failure"])
        self.assertEqual(code, 0)
        self.assertIn("targeting rule", out)

    def test_show_unknown(self):
        code, *_ = run(["show", "nope"])
        self.assertEqual(code, 2)


class InjectCliTests(unittest.TestCase):
    def test_inject_success_exit_0(self):
        code, out, ui, *_ = run(["inject", "payment-failure", "--variant", "100%"])
        self.assertEqual(code, 0, out)
        self.assertIn("[OK]", out)
        self.assertEqual(ui.flags["paymentFailure"]["defaultVariant"], "100%")

    def test_invalid_incident_exit_2(self):
        code, out, ui, *_ = run(["inject", "rm-rf"])
        self.assertEqual(code, 2)
        self.assertEqual(ui.writes, 0)

    def test_failed_injection_exit_1(self):
        ui = FakeFlagdUi()
        ui.write_exc = AdapterError("HTTP 500")
        code, out, *_ = run(["inject", "payment-failure"], ui=ui)
        self.assertEqual(code, 1)
        self.assertIn("[FAILED]", out)

    def test_missing_docker_exit_4(self):
        docker = FakeDocker()
        docker.ping_exc = AdapterUnavailable("docker CLI not found on PATH")
        code, out, *_ = run(["inject", "currency-down"], docker=docker)
        self.assertEqual(code, 4)

    def test_dry_run_output(self):
        code, out, ui, docker, _ = run(["inject", "currency-down", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("DRY RUN", out)
        self.assertIn("PLAN: docker stop", out)
        self.assertEqual(docker.calls, [])

    def test_remote_base_url_refused_exit_3(self):
        code, out, ui, _, created = run(["--base-url", "http://shop.example.com:8080", "inject", "payment-failure"])
        self.assertEqual(code, 3)
        self.assertEqual(created, [])
        self.assertEqual(ui.writes, 0)

    def test_bad_project_name_refused(self):
        code, *_ = run(["--project", "x; rm -rf /", "status"])
        self.assertEqual(code, 3)


class InjectManyCliTests(unittest.TestCase):
    def test_multiple_incidents_one_flag_write(self):
        code, out, ui, docker, _ = run(["inject", "cart-failure", "ad-high-cpu", "currency-down"])
        self.assertEqual(code, 0, out)
        self.assertEqual(ui.writes, 1)
        self.assertEqual(ui.flags["cartFailure"]["defaultVariant"], "50%")
        self.assertEqual(ui.flags["adHighCpu"]["defaultVariant"], "on")
        self.assertEqual(docker.calls, [("stop", "currency")])
        self.assertIn("3/3 incidents injected", out)

    def test_multiple_with_unknown_is_partial(self):
        code, out, ui, *_ = run(["inject", "cart-failure", "bogus"])
        self.assertEqual(code, EXIT_PARTIAL)
        self.assertIn("[INVALID] inject bogus", out)
        self.assertEqual(ui.flags["cartFailure"]["defaultVariant"], "50%")

    def test_variant_with_multiple_rejected(self):
        code, out, ui, *_ = run(["inject", "cart-failure", "payment-failure", "--variant", "100%"])
        self.assertEqual(code, 2)
        self.assertEqual(ui.writes, 0)

    def test_no_ids_and_no_all_rejected(self):
        code, *_ = run(["inject"])
        self.assertEqual(code, 2)
        code, *_ = run(["inject", "--all", "cart-failure"])
        self.assertEqual(code, 2)

    def test_all_requires_confirmation(self):
        code, out, ui, docker, _ = run(["inject", "--all"], answer="n")
        self.assertEqual(code, EXIT_ABORTED)
        self.assertEqual(ui.writes, 0)
        self.assertEqual(docker.calls, [])

    def test_all_skips_unavailable_and_injects_rest(self):
        code, out, ui, docker, _ = run(["inject", "--all", "--yes"])
        self.assertEqual(code, 0, out)
        self.assertIn("[SKIPPED] inject kafka-queue-problems", out)
        self.assertEqual(ui.writes, 1)
        self.assertEqual({n for _, n in docker.calls}, {"valkey-cart", "astronomy-db", "currency"})
        self.assertEqual(ui.flags["kafkaQueueProblems"]["defaultVariant"], "off")
        self.assertEqual(ui.flags["productCatalogFailure"]["targeting"]["if"][1], "on")

    def test_all_dry_run(self):
        code, out, ui, docker, _ = run(["inject", "--all", "--dry-run"], answer="n")
        self.assertEqual(code, 0)
        self.assertIn("DRY RUN", out)
        self.assertEqual(ui.writes, 0)
        self.assertEqual(docker.calls, [])

    def test_all_then_reset_all_restores_baseline(self):
        ui, docker = FakeFlagdUi(), FakeDocker()
        baseline = ui.read_flags()
        run(["inject", "--all", "-y"], ui=ui, docker=docker)
        code, out, *_ = run(["reset-all", "-y"], ui=ui, docker=docker)
        self.assertEqual(code, 0, out)
        self.assertEqual(ui.read_flags(), baseline)
        self.assertTrue(all(c.running for c in docker.containers.values()))


class ResetCliTests(unittest.TestCase):
    def test_reset_all_requires_confirmation(self):
        docker = FakeDocker()
        docker.containers["currency"].status = "exited"
        code, out, ui, docker, created = run(["reset-all"], docker=docker, answer="n")
        self.assertEqual(code, EXIT_ABORTED)
        self.assertEqual(created, [])
        self.assertEqual(docker.calls, [])

    def test_reset_all_confirmed_by_prompt(self):
        code, out, *_ = run(["reset-all"], answer="yes")
        self.assertEqual(code, 0, out)
        self.assertIn("incidents at baseline", out)

    def test_reset_all_yes_flag(self):
        code, *_ = run(["reset-all", "--yes"])
        self.assertEqual(code, 0)

    def test_reset_all_dry_run_needs_no_confirmation(self):
        code, out, *_ = run(["reset-all", "--dry-run"], answer="n")
        self.assertEqual(code, 0)
        self.assertIn("DRY RUN", out)

    def test_reset_all_partial_exit_8(self):
        docker = FakeDocker()
        docker.containers["currency"].status = "exited"
        docker.start_exc = AdapterError("cannot start")
        code, out, *_ = run(["reset-all", "-y"], docker=docker)
        self.assertEqual(code, EXIT_PARTIAL)
        self.assertIn("need attention", out)

    def test_reset_unknown(self):
        code, *_ = run(["reset", "bogus"])
        self.assertEqual(code, 2)


class StatusCliTests(unittest.TestCase):
    def test_status_hides_active_faults_by_default(self):
        ui = FakeFlagdUi()
        ui.flags["paymentFailure"]["defaultVariant"] = "50%"
        code, out, *_ = run(["status"], ui=ui)
        self.assertEqual(code, 0)
        self.assertNotIn("payment-failure", out)
        self.assertIn("hidden", out)

    def test_status_show_active(self):
        ui = FakeFlagdUi()
        ui.flags["paymentFailure"]["defaultVariant"] = "50%"
        docker = FakeDocker()
        docker.containers["currency"].status = "exited"
        code, out, *_ = run(["status", "--show-active"], ui=ui, docker=docker)
        self.assertIn("OPERATOR VIEW", out)
        self.assertIn("payment-failure", out)
        self.assertIn("currency-down", out)

    def test_status_docker_unavailable(self):
        docker = FakeDocker()
        docker.ping_exc = AdapterUnavailable("daemon down")
        code, out, *_ = run(["status"], docker=docker)
        self.assertIn("Docker: unavailable", out)


if __name__ == "__main__":
    unittest.main()
