"""Offline detector-to-API integration. No shop operations or paid LLM calls."""
import sys
import os
import tempfile
import unittest
import importlib.util
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from main import create_app

PIPELINE = Path(__file__).resolve().parents[1] / "src" / "detection-pipeline"
sys.path.append(str(PIPELINE))
from config import config
from rca_agent import RCAgent
from integration import RemediationClient
from tools import ReadOnlyTools
from evidence_collector import EvidenceCollector
watchdog_spec = importlib.util.spec_from_file_location('incident_watchdog', PIPELINE / 'watchdog.py')
watchdog_module = importlib.util.module_from_spec(watchdog_spec)
watchdog_spec.loader.exec_module(watchdog_module)
Watchdog = watchdog_module.Watchdog


class DetectionIntegrationTests(unittest.TestCase):
    def bundle(self):
        return {"incident_id": "OFFLINE-INTEGRATION", "target_service": "checkout",
                "evidence": {"metrics": {"error_rate": None}, "infrastructure": {
                    "containers": [{"name": "checkout", "status": "running"},
                                   {"name": "astronomy-db", "status": "exited"}]}}}

    def test_detector_submits_pending_proposal_to_actual_api_routes(self):
        contract = RCAgent()._generate_forensic_contract(self.bundle())
        self.assertEqual(contract["diagnosis"]["culprit_service"], "astronomy-db")
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
            "ACTION_AGENT_TOKEN": "integration-agent", "ACTION_HUMAN_TOKEN": "integration-human"}), patch.object(config, "ACTION_AGENT_TOKEN", "integration-agent"), patch.object(config, "ACTION_DRY_RUN", True):
            executor = Mock(return_value={"message": "simulated"})
            with TestClient(create_app(Path(folder) / "test.sqlite3", executor)) as api:
                class Transport:
                    def post(self, url, **kwargs):
                        return api.post('/handovers', json=kwargs['json'], headers=kwargs['headers'])
                client = RemediationClient(Transport())
                proposal = client.submit(contract)
                self.assertEqual(proposal['state'], 'pending')
                duplicate = client.submit(contract)
                self.assertEqual(proposal['id'], duplicate['id'])
                path = '/actions/' + proposal['id']
                agent = {'Authorization': 'Bearer integration-agent'}
                human = {'Authorization': 'Bearer integration-human'}
                self.assertEqual(api.post(path + '/execute', headers=agent).status_code, 409)
                api.post(path + '/decision', headers=human, json={'approve': True, 'reviewer': 'Test human'})
                self.assertEqual(api.post(path + '/execute', headers=agent).json()['state'], 'dry_run')
                executor.assert_not_called()

    def test_no_evidence_escalates_instead_of_claiming_confidence(self):
        result = RCAgent()._generate_forensic_contract({'incident_id': 'EMPTY', 'evidence': {}})
        self.assertEqual(result['status'], 'ESCALATED')
        self.assertIsNone(result['remediation_plan'])

    def test_prohibited_flag_evidence_never_reaches_llm(self):
        agent = RCAgent()
        with patch.object(agent, '_tool_loop') as llm:
            result = agent.analyze({'evidence': {'active_chaos_flags': {'synthetic': 'on'}}}, tools=Mock())
            self.assertEqual(result['status'], 'ESCALATED')
            llm.assert_not_called()

    def test_tools_cannot_read_flags_or_execute_shell(self):
        tools = ReadOnlyTools(Mock())
        for name in ('read_flags', 'restart_service', 'shell', 'approve'):
            with self.assertRaises(ValueError):
                tools.call(name, {})

    def test_tools_dispatch_read_queries_and_preserve_history(self):
        collector = Mock()
        collector._collect_traces.return_value = (['trace-1'], [])
        tools = ReadOnlyTools(collector)
        self.assertEqual(tools.call('get_traces', {'service': 'checkout'})['trace_ids'], ['trace-1'])
        tools.call('get_logs', {'service': 'payment'})
        tools.call('container_health', {'service': 'astronomy-db'})
        with patch('tools.requests.get') as query:
            query.return_value.json.return_value = {'status': 'success', 'data': {'result': []}}
            tools.call('query_prometheus', {'query': 'up'})
        self.assertEqual(len(tools.history), 4)

    def test_llm_can_request_tool_before_returning_contract(self):
        agent = RCAgent()
        tools = ReadOnlyTools(Mock())
        tools.collector._collect_infra_state.return_value = self.bundle()['evidence']['infrastructure']
        contract = agent._generate_forensic_contract(self.bundle())
        with patch.object(agent, '_call_gemini_api', side_effect=[
            {'tool': 'container_health', 'arguments': {'service': 'astronomy-db'}}, {'contract': contract}]):
            result = agent._tool_loop(self.bundle(), tools)
        self.assertEqual(result['status'], 'AWAITING_APPROVAL')
        self.assertEqual(tools.history[0]['tool'], 'container_health')

    def test_collector_does_not_expose_flag_reader(self):
        self.assertFalse(hasattr(EvidenceCollector, '_collect_chaos_flags'))

    def test_each_metric_symptom_triggers_without_fault_injection(self):
        watchdog = Watchdog.__new__(Watchdog)
        for method, labels, value, symptom in [
            ('_check_error_rates', {'service_name': 'checkout'}, '90', 'HIGH_ERROR_RATE'),
            ('_check_latency', {'service_name': 'checkout'}, '5', 'HIGH_LATENCY'),
            ('_check_resource_usage', {'container_name': 'email'}, '0.95', 'RESOURCE_SPIKE'),
            ('_check_kafka_lag', {'group': 'fraud-detection'}, '100', 'PIPELINE_QUEUE_LAG')]:
            with self.subTest(symptom=symptom), patch.object(watchdog, '_query', return_value={
                'status': 'success', 'data': {'result': [{'metric': labels, 'value': [0, value]}]}}):
                self.assertEqual(getattr(watchdog, method)()['symptom'], symptom)

    def test_running_but_unhealthy_container_is_detected(self):
        watchdog = Watchdog.__new__(Watchdog)
        container = Mock()
        container.name, container.status = 'payment', 'running'
        container.attrs = {'State': {'Health': {'Status': 'unhealthy'}}}
        watchdog.docker_client = Mock()
        watchdog.docker_client.containers.list.return_value = [container]
        self.assertEqual(watchdog._check_container_health()['symptom'], 'CONTAINER_DOWN')

    def test_tool_loop_enforces_budget(self):
        agent = RCAgent()
        tools = Mock()
        tools.definitions.return_value = {}
        with patch.object(agent, '_call_gemini_api', return_value={'tool': 'get_logs', 'arguments': {'service': 'checkout'}}):
            with self.assertRaises(ValueError):
                agent._tool_loop(self.bundle(), tools)
        self.assertEqual(tools.call.call_count, config.TOOL_CALL_LIMIT)

    def test_log_search_matches_live_otel_fields_and_requires_correlation(self):
        collector = EvidenceCollector.__new__(EvidenceCollector)
        collector.opensearch_url = 'http://test-opensearch'
        with patch('evidence_collector.requests.post') as post:
            post.return_value.json.return_value = {'hits': {'hits': []}}
            self.assertEqual(collector._collect_logs('checkout', ['trace-1']), [])
            body = post.call_args.kwargs['json']['query']['bool']
            self.assertEqual(body['minimum_should_match'], 1)
            self.assertIn({'match': {'resource.service.name': 'checkout'}}, body['should'])
            self.assertIn({'match': {'traceId': 'trace-1'}}, body['should'])
            post.return_value.raise_for_status.assert_called_once()


if __name__ == '__main__':
    unittest.main()
