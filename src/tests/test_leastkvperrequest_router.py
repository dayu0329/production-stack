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
    LeastKVPerRequestRouter,
    cleanup_routing_logic,
    get_routing_logic,
    initialize_routing_logic,
    reconfigure_routing_logic,
)
from vllm_router.stats.engine_stats import EngineStats


@pytest.fixture(autouse=True)
def cleanup():
    cleanup_routing_logic()
    yield
    cleanup_routing_logic()


def metrics(waiting, running, usage=None, name="kv_cache_usage_perc"):
    text = (
        f"vllm:num_requests_waiting {waiting}\n"
        f"vllm:num_requests_running {running}\n"
    )
    if usage is not None:
        text += f"vllm:{name} {usage}\n"
    return EngineStats.from_vllm_scrape(text)


@pytest.mark.parametrize(
    "first,second,expected",
    [
        ((0, 1, 0.9), (1, 100, 0.1), "first"),
        ((0, 20, 0.8), (0, 2, 0.2), "first"),
        ((0, 2, 0.2), (0, 20, 0.8), "second"),
        ((0, 0, 0.99), (0, 10, 0.1), "first"),
        ((0, 0, None), (0, 10, 0.1), "first"),
        ((1, 0, 0.0), (0, 10, 0.9), "second"),
    ],
)
def test_waiting_then_kv_per_running_request(first, second, expected):
    router = LeastKVPerRequestRouter()
    endpoints = [SimpleNamespace(url="first"), SimpleNamespace(url="second")]
    stats = {"first": metrics(*first), "second": metrics(*second)}
    assert router.route_request(endpoints, stats, {}, None) == expected


@pytest.mark.parametrize("running,usage", [(0, 0.9), (4, 0.8), (4, 0.0)])
def test_concurrent_ties_rotate_without_reservations(running, usage):
    router = LeastKVPerRequestRouter()
    endpoints = [SimpleNamespace(url="first"), SimpleNamespace(url="second")]
    stats = {endpoint.url: metrics(0, running, usage) for endpoint in endpoints}
    with ThreadPoolExecutor(max_workers=8) as executor:
        selected = list(
            executor.map(
                lambda unused: router.route_request(endpoints, stats, {}, None),
                range(100),
            )
        )
    assert Counter(selected) == {"first": 50, "second": 50}
    assert router._reservations == {}


def test_invalid_stale_and_unhealthy_backends_are_excluded():
    router = LeastKVPerRequestRouter()
    stats = {
        "missing-kv": metrics(0, 1),
        "invalid-kv": metrics(0, 1, "NaN"),
        "stale": replace(metrics(0, 0), observed_at=time.monotonic() - 10),
        "down": metrics(0, 0),
        "asleep": metrics(0, 0),
        "ready": metrics(2, 3, 0.8),
    }
    endpoints = [
        SimpleNamespace(url=url, healthy=url != "down", sleep=url == "asleep")
        for url in stats
    ]
    assert router.route_request(endpoints, stats, {}, None) == "ready"
    with pytest.raises(HTTPException) as raised:
        router.route_request([SimpleNamespace(url="down", healthy=False)], {}, {}, None)
    assert raised.value.status_code == 503


def test_all_missing_kv_metrics_rotate():
    router = LeastKVPerRequestRouter()
    endpoints = [SimpleNamespace(url="first"), SimpleNamespace(url="second")]
    stats = {endpoint.url: metrics(0, 1) for endpoint in endpoints}
    assert Counter(
        router.route_request(endpoints, stats, {}, None) for unused in range(20)
    ) == {"first": 10, "second": 10}


@pytest.mark.parametrize("name", ["gpu_cache_usage_perc", "kv_cache_usage_perc"])
@pytest.mark.parametrize("usage", [0, 0.25, 1])
def test_both_metric_names_accept_valid_ratios(name, usage):
    assert metrics(0, 1, usage, name).kv_cache_usage_ratio == usage


@pytest.mark.parametrize("usage", ["NaN", "+Inf", "-Inf", -0.1, 1.1])
def test_invalid_cache_usage_is_not_zero(usage):
    assert metrics(0, 1, usage).kv_cache_usage_ratio is None


def test_dp_cache_averages_matching_labels_and_prefers_modern_name():
    text = (
        'vllm:num_requests_running{engine="0"} 2\n'
        'vllm:num_requests_running{engine="1"} 4\n'
        'vllm:num_requests_waiting{engine="0"} 0\n'
        'vllm:num_requests_waiting{engine="1"} 0\n'
        'vllm:kv_cache_usage_perc{engine="0"} 0.2\n'
        'vllm:kv_cache_usage_perc{engine="1"} 0.6\n'
        'vllm:gpu_cache_usage_perc{engine="0"} 0.9\n'
        'vllm:gpu_cache_usage_perc{engine="1"} 0.9\n'
    )
    stats = EngineStats.from_vllm_scrape(text)
    assert stats.kv_cache_usage_ratio == pytest.approx(0.4)
    assert stats.num_running_requests == 6
    assert (
        EngineStats.from_vllm_scrape(
            text.replace('vllm:kv_cache_usage_perc{engine="1"} 0.6\n', "")
        ).kv_cache_usage_ratio
        is None
    )
    assert (
        EngineStats.from_vllm_scrape(
            text + 'vllm:kv_cache_usage_perc{engine="0"} 0.2\n'
        ).kv_cache_usage_ratio
        is None
    )


def test_cli_factory_and_dynamic_config(monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "vllm-router",
            "--service-discovery",
            "static",
            "--static-backends",
            "http://first:8000",
            "--static-models",
            "test",
            "--routing-logic",
            "leastkvperrequest",
            "--leastqueue-metrics-max-age",
            "7",
        ],
    )
    config = DynamicRouterConfig.from_args(parse_args())
    config.validate()
    router = initialize_routing_logic(
        config.routing_logic,
        leastqueue_metrics_max_age=config.leastqueue_metrics_max_age,
    )
    assert isinstance(router, LeastKVPerRequestRouter)
    assert get_routing_logic() is router
    assert router.metrics_max_age == 7
    assert router.reservation_ttl == 0
    replacement = reconfigure_routing_logic("leastkvperrequest")
    assert replacement is not router
    assert replacement.reservation_ttl == 0
