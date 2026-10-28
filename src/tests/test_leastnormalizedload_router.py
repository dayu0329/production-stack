import json
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from vllm_router.dynamic_config import DynamicRouterConfig
from vllm_router.parsers.parser import parse_args
from vllm_router.routers.routing_logic import (
    LeastNormalizedLoadRouter,
    cleanup_routing_logic,
    get_routing_logic,
    initialize_routing_logic,
    reconfigure_routing_logic,
)
from vllm_router.stats.engine_stats import EngineStats

TARGETS = {"http://first:8000": 100, "http://second:8000": 30}
ENDPOINTS = [SimpleNamespace(url=url) for url in TARGETS]


@pytest.fixture(autouse=True)
def cleanup():
    cleanup_routing_logic()
    yield
    cleanup_routing_logic()


def metrics(waiting, running):
    return EngineStats.from_vllm_scrape(
        f"vllm:num_requests_waiting {waiting}\n"
        f"vllm:num_requests_running {running}\n"
    )


@pytest.mark.parametrize(
    "first,second,expected",
    [
        ((0, 50), (0, 20), "http://first:8000"),
        ((0, 80), (0, 20), "http://second:8000"),
        ((0, 300), (1, 0), "http://first:8000"),
        ((1, 0), (0, 300), "http://second:8000"),
        ((0, 0), (0, 1), "http://first:8000"),
        ((0, 150), (0, 60), "http://first:8000"),
    ],
)
def test_waiting_then_normalized_running(first, second, expected):
    router = LeastNormalizedLoadRouter(TARGETS)
    stats = dict(zip(TARGETS, [metrics(*first), metrics(*second)]))
    assert all(
        router.route_request(ENDPOINTS, stats, {}, None) == expected
        for unused in range(20)
    )
    assert router._reservations == {}


@pytest.mark.parametrize("running", [(50, 15), (0, 0)])
def test_equal_ratios_rotate_under_concurrency(running):
    router = LeastNormalizedLoadRouter(TARGETS)
    stats = {url: metrics(0, count) for url, count in zip(TARGETS, running)}
    with ThreadPoolExecutor(max_workers=8) as executor:
        chosen = list(
            executor.map(
                lambda unused: router.route_request(ENDPOINTS, stats, {}, None),
                range(100),
            )
        )
    assert Counter(chosen) == {url: 50 for url in TARGETS}


def test_freshness_health_fallback_and_candidate_filtering():
    router = LeastNormalizedLoadRouter(TARGETS)
    first, second = TARGETS
    stats = {
        first: replace(metrics(0, 0), observed_at=time.monotonic() - 10),
        second: metrics(2, 60),
    }
    assert router.route_request(ENDPOINTS, stats, {}, None) == second
    stats[first] = EngineStats()
    assert router.route_request(ENDPOINTS, stats, {}, None) == second
    assert Counter(
        router.route_request(ENDPOINTS, {}, {}, None) for unused in range(20)
    ) == {first: 10, second: 10}
    assert router.route_request([ENDPOINTS[1]], stats, {}, None) == second
    for state in ({"healthy": False}, {"sleep": True}):
        with pytest.raises(HTTPException) as raised:
            router.route_request([SimpleNamespace(url=first, **state)], stats, {}, None)
        assert raised.value.status_code == 503


@pytest.mark.parametrize(
    "targets",
    [
        None,
        {},
        [],
        "bad",
        {"http://first:8000": 0},
        {"http://first:8000": -1},
        {"http://first:8000": True},
        {"http://first:8000": 1.5},
        {"http://first:8000": "100"},
        {"bad-url": 100},
        {"http://user:password@example.com": 100},
    ],
)
def test_invalid_targets_rejected(targets):
    with pytest.raises(ValueError):
        LeastNormalizedLoadRouter(targets)


def config():
    return DynamicRouterConfig(
        service_discovery="static",
        routing_logic="leastnormalizedload",
        static_backends=",".join(TARGETS),
        static_models="test,test",
        backend_running_targets=dict(TARGETS),
        engine_stats_interval=1,
    )


def test_mapping_must_cover_all_backend_urls_and_no_others():
    for targets in ({"http://first:8000": 100}, {**TARGETS, "http://typo:8000": 10}):
        with pytest.raises(ValueError):
            replace(config(), backend_running_targets=targets).validate()
    with pytest.raises(ValueError):
        replace(config(), service_discovery="k8s").validate()
    replace(
        config(),
        static_backends=",".join(TARGETS) + ",http://first:8000",
        static_models="test,test,another-model",
    ).validate()


def test_cli_factory_and_reconfigure(monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "vllm-router",
            "--service-discovery",
            "static",
            "--static-backends",
            ",".join(TARGETS),
            "--static-models",
            "test,test",
            "--routing-logic",
            "leastnormalizedload",
            "--backend-running-targets",
            json.dumps(TARGETS),
            "--leastqueue-metrics-max-age",
            "7",
        ],
    )
    parsed = DynamicRouterConfig.from_args(parse_args())
    router = initialize_routing_logic(
        parsed.routing_logic,
        backend_running_targets=parsed.backend_running_targets,
        leastqueue_metrics_max_age=parsed.leastqueue_metrics_max_age,
    )
    assert get_routing_logic() is router
    assert router.metrics_max_age == 7
    assert router.reservation_ttl == 0
    parsed.backend_running_targets["http://first:8000"] = 1
    assert router.backend_running_targets == TARGETS
    assert (
        reconfigure_routing_logic(
            "leastnormalizedload", backend_running_targets=TARGETS
        )
        is not router
    )


@pytest.mark.parametrize("file_type", ["yaml", "json"])
def test_startup_from_config_file(monkeypatch, tmp_path, file_type):
    if file_type == "yaml":
        path = "examples/wy-router/normalized-load.yaml"
        expected = DynamicRouterConfig.from_yaml(path)
    else:
        path = str(tmp_path / "router.json")
        expected = config()
        with open(path, "w", encoding="utf-8") as output:
            output.write(expected.to_json_str())
    expected.validate()
    monkeypatch.setattr(
        sys, "argv", ["vllm-router", f"--dynamic-config-{file_type}", path]
    )
    assert DynamicRouterConfig.from_args(parse_args()) == expected


def test_cli_rejects_missing_targets(monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "vllm-router",
            "--service-discovery",
            "static",
            "--static-backends",
            ",".join(TARGETS),
            "--static-models",
            "test,test",
            "--routing-logic",
            "leastnormalizedload",
        ],
    )
    with pytest.raises(ValueError):
        parse_args()
