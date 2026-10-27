import asyncio
import threading
import time
import sys
from dataclasses import replace

import pytest
from fastapi import FastAPI

import vllm_router.service_discovery as discovery_module
from vllm_router.dynamic_config import DynamicConfigWatcher, DynamicRouterConfig
from vllm_router.routers.routing_logic import (
    LeastKVPerRequestRouter,
    cleanup_routing_logic,
    initialize_routing_logic,
)
from vllm_router.service_discovery import StaticServiceDiscovery, get_service_discovery
from vllm_router.stats.engine_stats import EngineStatsScraper
from vllm_router.utils import SingletonMeta
from vllm_router.parsers.parser import parse_args


@pytest.fixture
def setup_reload(monkeypatch):
    cleanup_routing_logic()
    app = FastAPI()
    config = DynamicRouterConfig(
        service_discovery="static",
        routing_logic="leastqueue",
        static_backends="http://first:8000,http://second:8000",
        static_models="test,test",
        engine_stats_interval=1,
        max_instance_failover_reroute_attempts=1,
    )
    discovery = StaticServiceDiscovery(
        app, ["http://first:8000", "http://second:8000"], ["test", "test"]
    )
    monkeypatch.setattr(discovery_module, "_global_service_discovery", discovery)
    app.state.router = initialize_routing_logic("leastqueue")
    app.state.router.max_instance_failover_reroute_attempts = 1
    scraper = object.__new__(EngineStatsScraper)
    scraper.scrape_interval = 1
    scraper._wake_event = threading.Event()
    app.state.engine_stats_scraper = scraper
    watcher = object.__new__(DynamicConfigWatcher)
    watcher.current_config = config
    watcher.app = app
    yield app, config, watcher
    get_service_discovery().close()
    cleanup_routing_logic()


@pytest.mark.parametrize(
    "changes",
    [
        {"static_backends": "http://first:8000"},
        {"static_backends": "http://first:8000,bad-url"},
        {"leastqueue_reservation_ttl": -1},
        {"engine_stats_interval": 0},
        {"max_instance_failover_reroute_attempts": -1},
        {"static_aliases": "alias:unknown"},
        {"routing_logic": "bad"},
        {"callbacks": "missing.callback"},
    ],
)
def test_bad_update_keeps_all_active_state(setup_reload, changes):
    app, config, watcher = setup_reload
    router = app.state.router
    discovery = get_service_discovery()
    with pytest.raises(ValueError):
        watcher.reconfigure_all(replace(config, **changes))
    assert watcher.current_config is config
    assert get_service_discovery() is discovery
    assert app.state.router is router
    assert router.max_instance_failover_reroute_attempts == 1
    assert app.state.engine_stats_scraper.scrape_interval == 1


def test_add_remove_keeps_router_and_existing_endpoint_identity(setup_reload):
    app, config, watcher = setup_reload
    old = get_service_discovery()
    router = app.state.router
    router.route_request(old.get_endpoint_info(), {}, {}, None)
    reservations = dict(router._reservations)
    config = replace(config, static_backends="http://first:8000,http://third:8000")
    watcher.reconfigure_all(config)
    current = get_service_discovery()
    assert current is not old
    assert current.urls == ["http://first:8000", "http://third:8000"]
    assert current.engines_id[0] == old.engines_id[0]
    assert app.state.router is router
    assert router._reservations == reservations


def test_parameter_changes_do_not_recreate_discovery_or_reset_reservations(
    setup_reload,
):
    app, config, watcher = setup_reload
    discovery = get_service_discovery()
    router = app.state.router
    router.route_request(discovery.get_endpoint_info(), {}, {}, None)
    reservations = dict(router._reservations)
    watcher.reconfigure_all(
        replace(
            config,
            engine_stats_interval=2,
            leastqueue_metrics_max_age=8,
            leastqueue_reservation_ttl=0.5,
            max_instance_failover_reroute_attempts=3,
        )
    )
    assert app.state.router is router
    assert get_service_discovery() is discovery
    assert router._reservations == reservations
    assert router.metrics_max_age == 8
    assert router.reservation_ttl == 0.5
    assert router.max_instance_failover_reroute_attempts == 3
    assert app.state.engine_stats_scraper.scrape_interval == 2
    assert app.state.engine_stats_scraper._wake_event.is_set()


@pytest.mark.asyncio
async def test_file_watcher_rejects_bad_yaml_then_applies_valid_replacement(
    setup_reload, tmp_path
):
    app, config, _ = setup_reload
    app.state.event_loop = asyncio.get_running_loop()
    path = tmp_path / "router.yaml"
    path.write_text("static_models: [broken", encoding="utf-8")
    watcher = DynamicConfigWatcher(str(path), "YAML", 1, config, app)
    try:
        await asyncio.sleep(0.1)
        assert watcher.current_config == config
        replacement = tmp_path / "next.yaml"
        replacement.write_text(
            "service_discovery: static\nrouting_logic: leastqueue\n"
            "static_models:\n  test:\n    static_model_type: chat\n"
            "    static_backends:\n      - http://third:8000\n"
            "engine_stats_interval: 2\nmax_instance_failover_reroute_attempts: 2\n",
            encoding="utf-8",
        )
        replacement.replace(path)
        deadline = time.monotonic() + 4
        while watcher.current_config.static_backends != "http://third:8000":
            assert time.monotonic() < deadline
            await asyncio.sleep(0.05)
        assert get_service_discovery().urls == ["http://third:8000"]
        assert app.state.router.max_instance_failover_reroute_attempts == 2
        assert app.state.engine_stats_scraper.scrape_interval == 2
    finally:
        await asyncio.to_thread(watcher.close)
        SingletonMeta._instances.pop(DynamicConfigWatcher, None)


def test_user_example_parses():
    config = DynamicRouterConfig.from_yaml("examples/wy-router/router.yaml")
    config.validate()
    assert len(config.static_backends.split(",")) == 4
    assert config.static_models == ",".join(["my-model"] * 4)


def test_switch_to_kv_mode_update_backends_and_switch_back(setup_reload):
    app, config, watcher = setup_reload
    config = replace(config, routing_logic="leastkvperrequest")
    watcher.reconfigure_all(config)
    router = app.state.router
    assert isinstance(router, LeastKVPerRequestRouter)
    assert router.reservation_ttl == 0
    config = replace(
        config,
        static_backends="http://third:8000",
        static_models="test",
        leastqueue_metrics_max_age=8,
        leastqueue_reservation_ttl=3,
    )
    watcher.reconfigure_all(config)
    assert app.state.router is router
    assert router.metrics_max_age == 8
    assert router.reservation_ttl == 0
    assert get_service_discovery().urls == ["http://third:8000"]
    config = replace(config, routing_logic="roundrobin")
    watcher.reconfigure_all(config)
    config = replace(config, routing_logic="leastqueue")
    watcher.reconfigure_all(config)
    assert not isinstance(app.state.router, LeastKVPerRequestRouter)
    assert app.state.router.reservation_ttl == 3


def test_startup_loads_yaml_with_scrape_and_retry_settings(monkeypatch, tmp_path):
    config_path = tmp_path / "router.yaml"
    config_path.write_text(
        "service_discovery: static\nrouting_logic: leastqueue\n"
        "static_models:\n  test:\n    static_model_type: chat\n"
        "    static_backends:\n      - http://127.0.0.1:9000\n"
        "engine_stats_interval: 1\nmax_instance_failover_reroute_attempts: 3\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys, "argv", ["vllm-router", "--dynamic-config-yaml", str(config_path)]
    )
    args = parse_args()
    config = DynamicRouterConfig.from_args(args)
    assert config == DynamicRouterConfig.from_yaml(str(config_path))
    assert config.engine_stats_interval == 1
    assert config.max_instance_failover_reroute_attempts == 3
