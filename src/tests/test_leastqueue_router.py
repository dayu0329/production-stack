import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from vllm_router.dynamic_config import DynamicRouterConfig
from vllm_router.parsers.parser import parse_args
from vllm_router.routers import routing_logic
from vllm_router.routers.routing_logic import (
    LeastQueueRouter,
    cleanup_routing_logic,
    get_routing_logic,
    initialize_routing_logic,
    reconfigure_routing_logic,
)
from vllm_router.stats import engine_stats
from vllm_router.stats.engine_stats import EngineStats, EngineStatsScraper


@pytest.fixture(autouse=True)
def cleanup():
    cleanup_routing_logic()
    yield
    cleanup_routing_logic()


def endpoint(url, healthy=True, sleep=False):
    return SimpleNamespace(url=url, healthy=healthy, sleep=sleep)


def metrics(waiting, running):
    return EngineStats.from_vllm_scrape(
        f'vllm:num_requests_running{{model_name="test",engine="0"}} {running}\n'
        f'vllm:num_requests_waiting{{model_name="test",engine="0"}} {waiting}\n'
    )


@pytest.mark.parametrize(
    "first,second,expected",
    [((0, 100), (1, 0), "first"), ((2, 8), (2, 3), "second")],
)
def test_waiting_then_running(first, second, expected):
    router = LeastQueueRouter(reservation_ttl=0)
    stats = {"first": metrics(*first), "second": metrics(*second)}
    assert (
        router.route_request([endpoint("first"), endpoint("second")], stats, {}, None)
        == expected
    )


def test_ties_rotate_even_with_reordered_endpoints():
    router = LeastQueueRouter(reservation_ttl=0)
    endpoints = [endpoint("first"), endpoint("second")]
    stats = {item.url: metrics(0, 0) for item in endpoints}
    selected = []
    for _ in range(20):
        endpoints.reverse()
        selected.append(router.route_request(endpoints, stats, {}, None))
    assert Counter(selected) == {"first": 10, "second": 10}


def test_invalid_stale_and_unhealthy_backends_are_excluded():
    router = LeastQueueRouter()
    endpoints = [endpoint(url) for url in ["missing", "invalid", "stale", "ready"]]
    endpoints.extend([endpoint("down", healthy=False), endpoint("asleep", sleep=True)])
    stats = {
        "invalid": EngineStats(),
        "stale": replace(metrics(0, 0), observed_at=time.monotonic() - 10),
        "ready": metrics(3, 10),
        "down": metrics(0, 0),
        "asleep": metrics(0, 0),
    }
    assert router.route_request(endpoints, stats, {}, None) == "ready"


def test_no_metrics_falls_back_and_no_healthy_backend_returns_503():
    router = LeastQueueRouter()
    endpoints = [endpoint("first"), endpoint("second"), endpoint("down", healthy=False)]
    assert Counter(
        router.route_request(endpoints, {}, {}, None) for _ in range(20)
    ) == {"first": 10, "second": 10}
    with pytest.raises(HTTPException) as raised:
        router.route_request([endpoint("down", healthy=False)], {}, {}, None)
    assert raised.value.status_code == 503


def test_burst_reservations_are_atomic_and_expire(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(routing_logic.time, "monotonic", lambda: now[0])
    router = LeastQueueRouter(reservation_ttl=1)
    endpoints = [endpoint("first"), endpoint("second")]
    stats = {"first": metrics(0, 0), "second": metrics(0, 20)}
    with ThreadPoolExecutor(max_workers=16) as executor:
        chosen = list(
            executor.map(
                lambda _: router.route_request(endpoints, stats, {}, None), range(100)
            )
        )
    assert Counter(chosen) == {"first": 60, "second": 40}
    now[0] = 101.1
    assert router.route_request(endpoints, stats, {}, None) == "first"


def test_idle_queues_close_running_gap_before_distributing_evenly(monkeypatch):
    monkeypatch.setattr(routing_logic.time, "monotonic", lambda: 100.0)
    router = LeastQueueRouter()
    endpoints = [endpoint("first"), endpoint("second")]
    stats = {"first": metrics(0, 0), "second": metrics(0, 20)}
    chosen = [router.route_request(endpoints, stats, {}, None) for _ in range(20)]
    assert chosen == ["first"] * 20
    chosen = [router.route_request(endpoints, stats, {}, None) for _ in range(20)]
    assert Counter(chosen) == {"first": 10, "second": 10}


def test_actual_waiting_still_takes_priority_over_reservations(monkeypatch):
    monkeypatch.setattr(routing_logic.time, "monotonic", lambda: 100.0)
    router = LeastQueueRouter()
    endpoints = [endpoint("first"), endpoint("second")]
    stats = {"first": metrics(0, 100), "second": metrics(1, 0)}
    assert all(
        router.route_request(endpoints, stats, {}, None) == "first" for _ in range(50)
    )


def test_reservations_do_not_leak_between_models_or_reroute_candidates():
    router = LeastQueueRouter()
    stats = {"model-a": metrics(0, 0), "model-b": metrics(0, 0)}
    assert router.route_request([endpoint("model-a")], stats, {}, None) == "model-a"
    assert router.route_request([endpoint("model-b")], stats, {}, None) == "model-b"
    assert router.route_request([endpoint("model-b")], stats, {}, None) == "model-b"


@pytest.mark.parametrize("value", ["NaN", "+Inf", "-1", "0.5"])
def test_bad_count_is_unknown(value):
    assert not metrics(0, value).request_metrics_valid


def test_zero_is_valid_but_missing_metric_is_unknown():
    assert metrics(0, 0).request_metrics_valid
    assert not EngineStats.from_vllm_scrape("").request_metrics_valid
    assert not EngineStats.from_vllm_scrape(
        "vllm:num_requests_running 0\n"
    ).request_metrics_valid


def test_dp_metrics_sum_distinct_engines_and_require_matching_labels():
    text = (
        'vllm:num_requests_running{engine="0"} 2\n'
        'vllm:num_requests_running{engine="1"} 4\n'
        'vllm:num_requests_waiting{engine="0"} 1\n'
        'vllm:num_requests_waiting{engine="1"} 3\n'
    )
    stats = EngineStats.from_vllm_scrape(text)
    assert stats.request_metrics_valid
    assert (stats.num_queuing_requests, stats.num_running_requests) == (4, 6)
    assert not EngineStats.from_vllm_scrape(
        text.replace('vllm:num_requests_waiting{engine="1"} 3\n', "")
    ).request_metrics_valid
    assert not EngineStats.from_vllm_scrape(
        text + 'vllm:num_requests_running{engine="0"} 2\n'
    ).request_metrics_valid


def test_cli_factory_and_dynamic_config(monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "vllm-router",
            "--service-discovery",
            "static",
            "--static-backends",
            "http://first:8000,http://second:8000",
            "--static-models",
            "test,test",
            "--routing-logic",
            "leastqueue",
            "--leastqueue-metrics-max-age",
            "7",
            "--leastqueue-reservation-ttl",
            "0.5",
        ],
    )
    args = parse_args()
    config = DynamicRouterConfig.from_args(args)
    assert config.leastqueue_metrics_max_age == 7
    router = initialize_routing_logic(
        args.routing_logic,
        leastqueue_metrics_max_age=config.leastqueue_metrics_max_age,
        leastqueue_reservation_ttl=config.leastqueue_reservation_ttl,
    )
    assert get_routing_logic() is router
    assert router.metrics_max_age == 7
    assert router.reservation_ttl == 0.5
    replacement = reconfigure_routing_logic("leastqueue", leastqueue_reservation_ttl=0)
    assert replacement is not router
    assert replacement.reservation_ttl == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"metrics_max_age": 0},
        {"metrics_max_age": float("nan")},
        {"reservation_ttl": -1},
        {"reservation_ttl": float("inf")},
    ],
)
def test_invalid_policy_settings(kwargs):
    with pytest.raises(ValueError):
        LeastQueueRouter(**kwargs)


def test_scraper_deduplicates_urls_and_removes_failed_samples(monkeypatch):
    import threading

    scraper = object.__new__(EngineStatsScraper)
    scraper.engine_stats_lock = threading.Lock()
    scraper.engine_stats = {"old": metrics(0, 0)}
    scraper._scrape_one_endpoint = Mock(
        side_effect=lambda url: metrics(0, 1) if url == "ok" else None
    )
    monkeypatch.setattr(
        engine_stats,
        "get_service_discovery",
        lambda: SimpleNamespace(
            get_endpoint_info=lambda: [
                endpoint("ok"),
                endpoint("ok"),
                endpoint("failed"),
            ]
        ),
    )
    scraper._scrape_metrics()
    assert set(scraper.get_engine_stats()) == {"ok"}
    assert scraper._scrape_one_endpoint.call_count == 2
