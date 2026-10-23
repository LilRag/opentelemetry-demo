"""Agent-only HTTP handover using the Action API's shared contract schema."""
import importlib.util
from pathlib import Path
import requests
from config import config

spec = importlib.util.spec_from_file_location(
    "incident_contract", Path(__file__).resolve().parents[2] / "action-api" / "handover.py")
schema = importlib.util.module_from_spec(spec)
spec.loader.exec_module(schema)
HandoverContract = schema.HandoverContract


class RemediationClient:
    def __init__(self, session=None):
        self.session = session or requests.Session()

    def submit(self, contract):
        validated = HandoverContract.model_validate(contract)
        if not config.ACTION_AGENT_TOKEN:
            raise RuntimeError("Set ACTION_AGENT_TOKEN before submitting proposals")
        response = self.session.post(
            config.ACTION_API_URL.rstrip("/") + "/handovers",
            json={"request_id": validated.incident_id + "-fix-v1", "dry_run": config.ACTION_DRY_RUN,
                  "contract": validated.model_dump(mode="json")},
            headers={"Authorization": "Bearer " + config.ACTION_AGENT_TOKEN}, timeout=15)
        response.raise_for_status()
        return response.json()
