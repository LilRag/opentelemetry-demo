"""Typed boundary between the detection pipeline and the action executor."""

import json
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Diagnosis(ContractModel):
    culprit_service: str = Field(min_length=1, max_length=100)
    root_cause: str = Field(min_length=1, max_length=2000)
    blast_radius: str = Field(min_length=1, max_length=2000)
    evidence: dict = Field(min_length=1)

    @model_validator(mode="after")
    def telemetry_only(self):
        forbidden = {"active_flag", "active_flags", "active_chaos_flags", "current_variant", "defaultVariant"}

        def inspect(value):
            if isinstance(value, dict):
                if forbidden.intersection(value):
                    raise ValueError("Current fault-flag settings cannot be diagnostic evidence")
                for child in value.values():
                    inspect(child)
            elif isinstance(value, list):
                for child in value:
                    inspect(child)

        inspect(self.evidence)
        if len(json.dumps(self.evidence)) > 16000:
            raise ValueError("Evidence bundle is too large; submit concise references")
        return self


class HandoverStep(ContractModel):
    step: int = Field(strict=True, ge=1, le=10)
    action_type: Literal["TOGGLE_FLAG", "RESTART_CONTAINER", "START_CONTAINER", "VERIFY_RECOVERY"]
    target: str = Field(min_length=1, max_length=100)
    parameters: dict
    description: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_parameters(self):
        params = self.parameters
        if self.action_type == "TOGGLE_FLAG":
            if not {"desired_variant"} <= params.keys() or set(params) - {"desired_variant", "api_endpoint", "flag_name"}:
                raise ValueError("TOGGLE_FLAG requires desired_variant")
            if params.get("flag_name", self.target) != self.target:
                raise ValueError("flag_name must match target")
            if params["desired_variant"] != "off":
                raise ValueError("Only disabling a fault is permitted")
            if "api_endpoint" in params and params["api_endpoint"] != "http://localhost:8080/feature/api/write":
                raise ValueError("Model-selected execution URLs are forbidden")
        elif self.action_type in {"START_CONTAINER", "RESTART_CONTAINER"}:
            if set(params) - {"container_name"} or params.get("container_name", self.target) != self.target:
                raise ValueError("Optional container_name must match target")
        else:
            if set(params) != {"metric_query", "expected_value", "evaluation_window_seconds"}:
                raise ValueError("VERIFY_RECOVERY requires query, expected value and evaluation window")
            if not isinstance(params["metric_query"], str) or not 1 <= len(params["metric_query"]) <= 2000:
                raise ValueError("Invalid verification query")
            if type(params["expected_value"]) not in (int, float):
                raise ValueError("expected_value must be numeric")
            if type(params["evaluation_window_seconds"]) is not int or not 1 <= params["evaluation_window_seconds"] <= 600:
                raise ValueError("Verification window must be 1 to 600 seconds")
        return self


class RemediationPlan(ContractModel):
    summary: str = Field(min_length=1, max_length=2000)
    actions: list[HandoverStep] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def ordered(self):
        if [s.step for s in self.actions] != list(range(1, len(self.actions) + 1)):
            raise ValueError("Steps must be ordered and numbered consecutively from 1")
        verification_started = False
        writes = 0
        for step in self.actions:
            if step.action_type == "VERIFY_RECOVERY":
                verification_started = True
            else:
                if verification_started:
                    raise ValueError("Recovery checks must follow all system changes")
                writes += 1
        if not 1 <= writes <= 5:
            raise ValueError("Plan requires 1 to 5 system changes")
        return self


class HandoverContract(ContractModel):
    incident_id: str = Field(min_length=1, max_length=100)
    status: Literal["AWAITING_APPROVAL"]
    timestamp: datetime
    diagnosis: Diagnosis
    remediation_plan: RemediationPlan


class HandoverSubmission(ContractModel):
    request_id: str = Field(min_length=1, max_length=100)
    dry_run: bool = Field(default=True, strict=True)
    contract: HandoverContract


def to_proposal(submission):
    contract = submission.contract
    steps = []
    for step in contract.remediation_plan.actions:
        if step.action_type == "VERIFY_RECOVERY":
            continue
        elif step.action_type == "TOGGLE_FLAG":
            steps.append({"kind": "feature_flag", "parameters": {"flag": step.target, "variant": "off"}})
        else:
            steps.append({"kind": "start" if step.action_type == "START_CONTAINER" else "restart",
                          "parameters": {"service": step.target}})
    return {
        "request_id": submission.request_id, "incident_id": contract.incident_id,
        "reason": contract.remediation_plan.summary,
        "evidence": [json.dumps(contract.diagnosis.evidence)],
        "kind": "plan", "parameters": {"steps": steps}, "dry_run": submission.dry_run,
        "handover": contract,
    }
