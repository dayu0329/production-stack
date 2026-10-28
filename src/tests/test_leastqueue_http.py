import asyncio
import json
import threading
from dataclasses import replace
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from aiohttp import web
from fastapi import FastAPI

from vllm_router.aiohttp_client import AiohttpClientWrapper
from vllm_router.dynamic_config import DynamicConfigWatcher, DynamicRouterConfig
import vllm_router.service_discovery as discovery_module
from vllm_router.routers.main_router import main_router
from vllm_router.routers.routing_logic import (
    cleanup_routing_logic,
    initialize_routing_logic,
)
from vllm_router.service_discovery import EndpointInfo
from vllm_router.service_discovery import StaticServiceDiscovery
from vllm_router.services.request_service import request as request_service
from vllm_router.stats import engine_stats
from vllm_router.stats.engine_stats import EngineStatsScraper
from vllm_router.stats.request_stats import RequestStatsMonitor, SingletonMeta


@asynccontextmanager
async def backend(name, waiting, running, model="test"):
    state = {"waiting": waiting, "running": running, "status": 200, "seen": []}

    async def get_metrics(request):
        return web.Response(
            text=(
                f'vllm:num_requests_running{{model_name="{model}"}} {state["running"]}\n'
                f'vllm:num_requests_waiting{{model_name="{model}"}} {state["waiting"]}\n'
                f'vllm:kv_cache_usage_perc{{model_name="{model}"}} 0.5\n'
            )
        )

    async def chat(request):
        payload = await request.json()
        state["seen"].append(payload)
        if state["status"] != 200:
            return web.json_response({"error": "busy"}, status=state["status"])
        if payload.get("stream"):
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            chunk = {"choices": [{"delta": {"content": name}}]}
            await response.write(f"data: {json.dumps(chunk)}\n\n".encode())
            if state.get("stream_started") is not None:
                state["stream_started"].set()
                await state["stream_release"].wait()
            await response.write(b"data: [DONE]\n\n")
            await response.write_eof()
            return response
        return web.json_response(
            {
                "backend": name,
                "model": model,
                "choices": [{"message": {"role": "assistant", "content": name}}],
            }
        )

    server = web.Application()
    server.router.add_get("/metrics", get_metrics)
    server.router.add_post("/v1/chat/completions", chat)
    runner = web.AppRunner(server)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    state["url"] = f"http://127.0.0.1:{runner.addresses[0][1]}"
    state["stop"] = site.stop
    try:
        yield state
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "policy", ["leastqueue", "leastkvperrequest", "leastnormalizedload"]
)
async def test_metrics_to_http_selection_streaming_model_filter_and_failover(
    monkeypatch,
    policy,
):
    cleanup_routing_logic()
    SingletonMeta._instances.pop(RequestStatsMonitor, None)
    async with (
        backend("first", 0, 100) as first,
        backend("second", 1, 0) as second,
        backend("other", 0, 0, "other") as other,
    ):
        endpoints = [
            EndpointInfo(
                url=state["url"],
                model_names=[model],
                Id=name,
                added_timestamp=0,
                model_label="default",
                sleep=False,
            )
            for state, model, name in [
                (first, "test", "first"),
                (second, "test", "second"),
                (other, "other", "other"),
            ]
        ]
        discovery = SimpleNamespace(
            get_endpoint_info=lambda: endpoints,
            aliases=None,
            has_ever_seen_model=lambda model: model in {"test", "other"},
        )
        monkeypatch.setattr(engine_stats, "get_service_discovery", lambda: discovery)
        monkeypatch.setattr(request_service, "get_service_discovery", lambda: discovery)
        monkeypatch.setattr(
            request_service, "is_request_rewriter_initialized", lambda: False
        )
        scraper = object.__new__(EngineStatsScraper)
        scraper.scrape_interval = 1
        scraper.engine_stats_lock = threading.Lock()
        scraper.engine_stats = {}
        await asyncio.to_thread(scraper._scrape_metrics)

        app = FastAPI()
        app.include_router(main_router)
        app.state.router = initialize_routing_logic(
            policy,
            backend_running_targets={
                first["url"]: 100,
                second["url"]: 30,
                other["url"]: 10,
            },
            leastqueue_reservation_ttl=0,
            max_instance_failover_reroute_attempts=1,
        )
        app.state.engine_stats_scraper = scraper
        app.state.request_stats_monitor = RequestStatsMonitor(sliding_window_size=60)
        app.state.aiohttp_client_wrapper = AiohttpClientWrapper()
        app.state.aiohttp_client_wrapper.start()
        app.state.otel_enabled = False
        app.state.semantic_cache_available = False
        app.state.callbacks = None
        app.state.external_provider_registry = None
        payload = {"model": "test", "messages": [{"role": "user", "content": "hello"}]}
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://router"
            ) as client:
                response = await client.post("/v1/chat/completions", json=payload)
                assert response.status_code == 200
                assert response.json()["backend"] == "first"
                assert other["seen"] == []

                second["waiting"] = 0
                await asyncio.to_thread(scraper._scrape_metrics)
                response = await client.post(
                    "/v1/chat/completions", json={**payload, "stream": True}
                )
                assert response.status_code == 200
                assert response.headers["content-type"].startswith("text/event-stream")
                assert "second" in response.text
                assert "data: [DONE]" in response.text

                second["status"] = 503
                response = await client.post("/v1/chat/completions", json=payload)
                assert response.status_code == 503
                assert response.json()["error"] == "busy"

                await app.state.aiohttp_client_wrapper.stop()
                app.state.aiohttp_client_wrapper.start()
                await second["stop"]()
                response = await client.post("/v1/chat/completions", json=payload)
                assert response.status_code == 200
                assert response.json()["backend"] == "first"
                await asyncio.to_thread(scraper._scrape_metrics)
                assert second["url"] not in scraper.get_engine_stats()

                response = await client.post(
                    "/v1/chat/completions", json={**payload, "model": "unknown"}
                )
                assert response.status_code == 404
                stats = app.state.request_stats_monitor.get_request_stats(0)
                assert all(
                    value.in_prefill_requests == value.in_decoding_requests == 0
                    for value in stats.values()
                )
        finally:
            await app.state.aiohttp_client_wrapper.stop()
            cleanup_routing_logic()
            SingletonMeta._instances.pop(RequestStatsMonitor, None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "policy", ["leastqueue", "leastkvperrequest", "leastnormalizedload"]
)
async def test_remove_backend_preserves_active_sse_and_new_requests_use_new_list(
    monkeypatch,
    policy,
):
    cleanup_routing_logic()
    SingletonMeta._instances.pop(RequestStatsMonitor, None)
    async with backend("first", 0, 0) as first, backend("second", 0, 10) as second:
        first["stream_started"] = asyncio.Event()
        first["stream_release"] = asyncio.Event()
        app = FastAPI()
        app.include_router(main_router)
        config = DynamicRouterConfig(
            service_discovery="static",
            routing_logic=policy,
            backend_running_targets={first["url"]: 100, second["url"]: 30},
            static_backends=f'{first["url"]},{second["url"]}',
            static_models="test,test",
            engine_stats_interval=1,
        )
        discovery = StaticServiceDiscovery(
            app, [first["url"], second["url"]], ["test", "test"]
        )
        monkeypatch.setattr(discovery_module, "_global_service_discovery", discovery)
        monkeypatch.setattr(
            request_service, "is_request_rewriter_initialized", lambda: False
        )
        scraper = object.__new__(EngineStatsScraper)
        scraper.scrape_interval = 1
        scraper._wake_event = threading.Event()
        scraper.engine_stats_lock = threading.Lock()
        scraper.engine_stats = {}
        await asyncio.to_thread(scraper._scrape_metrics)
        app.state.router = initialize_routing_logic(
            policy, backend_running_targets=config.backend_running_targets
        )
        app.state.engine_stats_scraper = scraper
        app.state.request_stats_monitor = RequestStatsMonitor(sliding_window_size=60)
        app.state.aiohttp_client_wrapper = AiohttpClientWrapper()
        app.state.aiohttp_client_wrapper.start()
        app.state.otel_enabled = False
        app.state.semantic_cache_available = False
        app.state.callbacks = None
        app.state.external_provider_registry = None
        watcher = object.__new__(DynamicConfigWatcher)
        watcher.current_config = config
        watcher.app = app
        payload = {"model": "test", "messages": [{"role": "user", "content": "hello"}]}
        pending = None
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://router"
            ) as client:
                pending = asyncio.create_task(
                    client.post(
                        "/v1/chat/completions", json={**payload, "stream": True}
                    )
                )
                await asyncio.wait_for(first["stream_started"].wait(), timeout=5)
                watcher.reconfigure_all(
                    replace(
                        config,
                        static_backends=second["url"],
                        static_models="test",
                        backend_running_targets={second["url"]: 30},
                    )
                )
                response = await client.post("/v1/chat/completions", json=payload)
                assert response.status_code == 200
                assert response.json()["backend"] == "second"
                assert not pending.done()
                first["stream_release"].set()
                streamed = await asyncio.wait_for(pending, timeout=5)
                assert streamed.status_code == 200
                assert "first" in streamed.text
                assert "data: [DONE]" in streamed.text
        finally:
            first["stream_release"].set()
            if pending is not None and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            await app.state.aiohttp_client_wrapper.stop()
            discovery_module.get_service_discovery().close()
            cleanup_routing_logic()
            SingletonMeta._instances.pop(RequestStatsMonitor, None)
