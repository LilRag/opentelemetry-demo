"""Queries for the HTTP, RPC and container metrics exposed by this demo."""
import json


def error_rate(service=None):
    scope = 'service_namespace="opentelemetry-demo"'
    if service:
        scope += ',service_name=' + json.dumps(service)
    counters = [
        ('http_server_request_duration_seconds_count', 'http_response_status_code=~"5.."'),
        ('rpc_server_call_duration_seconds_count', 'rpc_response_status_code!="OK",rpc_response_status_code!="0",rpc_response_status_code!=""'),
        ('rpc_server_duration_milliseconds_count', 'rpc_grpc_status_code!="0",rpc_grpc_status_code!=""'),
    ]
    ratios = []
    for metric, failure in counters:
        total = f'sum by(service_name)(rate({metric}{{{scope}}}[1m]))'
        errors = f'sum by(service_name)(rate({metric}{{{scope},{failure}}}[1m]))'
        ratios.append(f'(({errors} or ({total} * 0)) / ({total} > 0)) * 100')
    return 'max by(service_name)(' + ' or '.join(
        f'label_replace(({q}),"signal","{i}","service_name",".*")' for i, q in enumerate(ratios)) + ')'


def latency(service=None):
    scope = 'service_namespace="opentelemetry-demo"'
    if service:
        scope += ',service_name=' + json.dumps(service)
    metrics = [('http_server_request_duration_seconds_bucket', 1),
               ('rpc_server_call_duration_seconds_bucket', 1),
               ('rpc_server_duration_milliseconds_bucket', 1000)]
    return 'max by(service_name)(' + ' or '.join(
        f'label_replace((histogram_quantile(0.95,sum by(le,service_name)(rate({name}{{{scope}}}[1m]))) / {divisor}),"signal","{i}","service_name",".*")'
        for i, (name, divisor) in enumerate(metrics)) + ')'


MEMORY = 'container_memory_usage_total_bytes / on(container_name,host_name) (container_memory_usage_limit_bytes > 0)'
QUEUE = 'sum by(group)(kafka_consumer_group_lag_sum_ratio)'
