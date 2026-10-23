# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import os
from pathlib import Path
from dotenv import load_dotenv

# Automatically load .env from the current directory or parent directories if present
env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=env_path)

class Config:
    """
    Central Configuration for the Detection and Evidence Collection Pipeline.
    Loads values from environment variables with production-ready defaults.
    """

    # ---------------------------------------------------------
    # 1. Telemetry & Infrastructure Endpoints
    # ---------------------------------------------------------
    PROMETHEUS_URL: str = os.getenv("PROMETHEUS_URL", "http://localhost:9090")
    # Jaeger v2 query endpoint routed through the Envoy reverse proxy
    JAEGER_URL: str = os.getenv("JAEGER_URL", "http://localhost:8080/jaeger/ui/api/v3/traces")
    OPENSEARCH_URL: str = os.getenv("OPENSEARCH_URL", "")
    ACTION_API_URL = os.getenv("ACTION_API_URL", "http://127.0.0.1:8020")
    ACTION_AGENT_TOKEN = os.getenv("ACTION_AGENT_TOKEN", "")
    ACTION_DRY_RUN = os.getenv("ACTION_DRY_RUN", "true").lower() == "true"
    TOOL_CALL_LIMIT = 4
    DOCKER_CONTAINER_PREFIX: str = os.getenv("DOCKER_CONTAINER_PREFIX", "opentelemetry-demo")

    # ---------------------------------------------------------
    # 2. Universal Detection Thresholds (Tuning Gates)
    # ---------------------------------------------------------
    # Polling frequency: how often the Watchdog checks the system health
    WATCHDOG_INTERVAL_SECONDS: int = int(os.getenv("WATCHDOG_INTERVAL_SECONDS", "15"))

    # Symptom 1: Trigger if > 15% of requests on any service return 5xx
    ERROR_RATE_THRESHOLD_PERCENT: float = float(os.getenv("ERROR_RATE_THRESHOLD_PERCENT", "15.0"))

    # Symptom 2: Trigger if p95 request duration exceeds 3.0 seconds
    LATENCY_THRESHOLD_SECONDS: float = float(os.getenv("LATENCY_THRESHOLD_SECONDS", "3.0"))

    # Symptom 3: Trigger if memory usage exceeds 85% of container limit
    MEMORY_THRESHOLD_RATIO: float = float(os.getenv("MEMORY_THRESHOLD_RATIO", "0.85"))

    # Symptom 4: Trigger if Kafka consumer lag exceeds 50 unread messages
    KAFKA_LAG_THRESHOLD: int = int(os.getenv("KAFKA_LAG_THRESHOLD", "50"))

    # ---------------------------------------------------------
    # 3. Cognitive LLM Settings
    # ---------------------------------------------------------
    LLM_MODEL: str = os.getenv("LLM_MODEL", "")
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
    GOOGLE_API_KEY: str = os.getenv("GOOGLE_API_KEY", "") or GEMINI_API_KEY
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")

    # Ensure SDKs can find the Gemini key under either variable
    if GEMINI_API_KEY and not os.getenv("GOOGLE_API_KEY"):
        os.environ["GOOGLE_API_KEY"] = GEMINI_API_KEY
    if GOOGLE_API_KEY and not os.getenv("GEMINI_API_KEY"):
        os.environ["GEMINI_API_KEY"] = GOOGLE_API_KEY

    @classmethod
    def display(cls):
        """Prints current configuration for easy debugging on startup."""
        print("=" * 60)
        print("[*] Detection Pipeline Configuration Initialized")
        print("=" * 60)
        print(f"  - Prometheus Endpoint : {cls.PROMETHEUS_URL}")
        print(f"  - Jaeger Endpoint     : {cls.JAEGER_URL}")
        print(f"  - OpenSearch Endpoint : {cls.OPENSEARCH_URL}")
        print(f"  - Action API Endpoint : {cls.ACTION_API_URL}")
        print(f"  - Watchdog Interval   : Every {cls.WATCHDOG_INTERVAL_SECONDS}s")
        print(f"  - Error Threshold     : > {cls.ERROR_RATE_THRESHOLD_PERCENT}%")
        print(f"  - Latency Threshold   : > {cls.LATENCY_THRESHOLD_SECONDS}s")
        print(f"  - Target LLM Model    : {cls.LLM_MODEL}")
        print("=" * 60)

# Instantiate singleton instance
config = Config()
