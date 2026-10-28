# Copyright 2024-2025 The vLLM Production Stack Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import asyncio
import json
import math
import threading
import time
from dataclasses import dataclass, fields
from typing import Literal, Optional
from urllib.parse import urlsplit

from fastapi import FastAPI

from vllm_router.log import init_logger
from vllm_router.parsers.yaml_utils import (
    read_and_process_yaml_config_file,
)
from vllm_router.routers.routing_logic import (
    LeastKVPerRequestRouter,
    LeastNormalizedLoadRouter,
    LeastQueueRouter,
    RoundRobinRouter,
    reconfigure_routing_logic,
)
from vllm_router.service_discovery import (
    ServiceDiscoveryType,
    StaticServiceDiscovery,
    get_service_discovery,
    reconfigure_service_discovery,
    swap_service_discovery,
)
from vllm_router.services.callbacks_service.callbacks import configure_custom_callbacks
from vllm_router.utils import (
    SingletonMeta,
    ModelType,
    parse_comma_separated_args,
    parse_static_aliases,
    parse_static_urls,
)

logger = init_logger(__name__)


@dataclass
class DynamicRouterConfig:
    """
    Re-configurable configurations for the VLLM router.
    """

    # Required configurations
    service_discovery: str
    routing_logic: str

    # Optional configurations
    # Service discovery configurations
    static_backends: Optional[str] = None
    static_models: Optional[str] = None
    static_aliases: Optional[str] = None
    static_model_labels: Optional[str] = None
    static_model_types: Optional[str] = None
    static_backend_health_checks: Optional[bool] = False
    static_backend_health_check_interval: Optional[int] = 60
    static_backend_health_check_timeout_seconds: Optional[int] = 10
    prefill_model_labels: Optional[str] = None
    decode_model_labels: Optional[str] = None
    k8s_port: Optional[int] = None
    k8s_namespace: Optional[str] = None
    k8s_label_selector: Optional[str] = None

    # Routing logic configurations
    session_key: Optional[str] = None
    leastqueue_metrics_max_age: float = 5.0
    leastqueue_reservation_ttl: float = 1.0
    backend_running_targets: Optional[dict[str, int]] = None
    engine_stats_interval: float = 30.0
    max_instance_failover_reroute_attempts: int = 0
    priority_header: Optional[str] = "x-request-priority"
    priority_field: Optional[str] = "priority"
    priority_default: Optional[int] = 0
    priority_threshold: Optional[int] = None

    # Logging Options
    callbacks: Optional[str] = None

    # Batch API configurations
    # TODO (ApostaC): Support dynamic reconfiguration of batch API
    # enable_batch_api: bool
    # file_storage_class: str
    # file_storage_path: str
    # batch_processor: str

    # Stats configurations
    # TODO (ApostaC): Support dynamic reconfiguration of stats monitor
    # engine_stats_interval: int
    # request_stats_window: int
    # log_stats: bool
    # log_stats_interval: int

    @staticmethod
    def from_args(args) -> "DynamicRouterConfig":
        return DynamicRouterConfig(
            service_discovery=args.service_discovery,
            static_backends=args.static_backends,
            static_models=args.static_models,
            static_model_types=args.static_model_types,
            static_aliases=args.static_aliases,
            static_backend_health_checks=args.static_backend_health_checks,
            static_backend_health_check_interval=args.static_backend_health_check_interval,
            static_backend_health_check_timeout_seconds=args.static_backend_health_check_timeout_seconds,
            k8s_port=args.k8s_port if args.service_discovery == "k8s" else None,
            k8s_namespace=(
                args.k8s_namespace if args.service_discovery == "k8s" else None
            ),
            k8s_label_selector=(
                args.k8s_label_selector if args.service_discovery == "k8s" else None
            ),
            # Routing logic configurations
            routing_logic=args.routing_logic,
            session_key=args.session_key,
            leastqueue_metrics_max_age=getattr(args, "leastqueue_metrics_max_age", 5.0),
            leastqueue_reservation_ttl=getattr(args, "leastqueue_reservation_ttl", 1.0),
            backend_running_targets=getattr(args, "backend_running_targets", None),
            engine_stats_interval=args.engine_stats_interval,
            max_instance_failover_reroute_attempts=args.max_instance_failover_reroute_attempts,
            priority_header=args.priority_header,
            priority_field=args.priority_field,
            priority_default=args.priority_default,
            priority_threshold=args.priority_threshold,
            # Logging Options
            callbacks=args.callbacks,
        )

    @staticmethod
    def from_yaml(yaml_path: str) -> "DynamicRouterConfig":
        config = read_and_process_yaml_config_file(yaml_path)
        return DynamicRouterConfig(**config)

    @staticmethod
    def from_json(json_path: str) -> "DynamicRouterConfig":
        with open(json_path, "r") as f:
            config = json.load(f)
        return DynamicRouterConfig(**config)

    def to_json_str(self) -> str:
        return json.dumps(self, default=lambda o: o.__dict__, sort_keys=True, indent=4)

    def validate(self):
        if self.backend_running_targets is not None:
            LeastNormalizedLoadRouter.validate_targets(self.backend_running_targets)
        if self.routing_logic == "leastnormalizedload":
            if self.service_discovery != "static":
                raise ValueError(
                    "leastnormalizedload requires static service discovery"
                )
            LeastNormalizedLoadRouter.validate_targets(self.backend_running_targets)
            if not isinstance(self.static_backends, str) or set(
                self.static_backends.split(",")
            ) != set(self.backend_running_targets):
                raise ValueError(
                    "backend_running_targets must cover exactly the configured backend URLs"
                )
        for name in (
            "engine_stats_interval",
            "leastqueue_metrics_max_age",
            "static_backend_health_check_interval",
            "static_backend_health_check_timeout_seconds",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{name} must be positive and finite")
        value = self.leastqueue_reservation_ttl
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError(
                "leastqueue_reservation_ttl must be nonnegative and finite"
            )
        attempts = self.max_instance_failover_reroute_attempts
        if type(attempts) is not int or attempts < 0:
            raise ValueError(
                "max_instance_failover_reroute_attempts must be a nonnegative integer"
            )
        if type(self.static_backend_health_checks) is not bool:
            raise ValueError("static_backend_health_checks must be a boolean")
        if self.service_discovery == "static":
            if not isinstance(self.static_backends, str) or not isinstance(
                self.static_models, str
            ):
                raise ValueError("Static backends and models must be configured")
            urls = self.static_backends.split(",")
            models = parse_comma_separated_args(self.static_models)
            if not urls or not models or any(not model.strip() for model in models):
                raise ValueError("At least one backend and model are required")
            for url in urls:
                parsed = urlsplit(url)
                if (
                    parsed.scheme not in ("http", "https")
                    or not parsed.hostname
                    or parsed.query
                    or parsed.fragment
                    or parsed.username
                    or parsed.password
                    or parsed.path not in ("", "/")
                ):
                    raise ValueError(f"Invalid backend base URL: {url}")
                parsed.port
            labels = parse_comma_separated_args(self.static_model_labels)
            types = parse_comma_separated_args(self.static_model_types)
            StaticServiceDiscovery._validate_index_aligned_lists(
                urls, models, labels, types
            )
            if len(set(zip(urls, models))) != len(urls):
                raise ValueError("Duplicate backend/model pair")
            if self.static_backend_health_checks and not types:
                raise ValueError("Health checks require static_model_types")
            if types and any(
                model_type not in ModelType.get_all_fields() for model_type in types
            ):
                raise ValueError("Unsupported static model type")
            if self.static_aliases:
                aliases = parse_static_aliases(self.static_aliases)
                if any(model not in models for model in aliases.values()):
                    raise ValueError("Alias target must be a configured model")


class DynamicConfigWatcher(metaclass=SingletonMeta):
    """
    Watches a config file for changes and updates the DynamicRouterConfig accordingly.
    """

    def __init__(
        self,
        config_path: str,
        config_file_type: Literal["YAML", "JSON"],
        watch_interval: int,
        init_config: DynamicRouterConfig,
        app: FastAPI,
    ):
        """
        Initializes the ConfigMapWatcher with the given ConfigMap name and namespace.

        Args:
            config_path: the path to the config file containing the dynamic configuration
            config_file_type: the config file type containing the dynamic configuration (YAML or JSON)
            watch_interval: the interval in seconds at which to watch the for changes
            app: the fastapi app to reconfigure
        """
        self.config_path = config_path
        self.config_file_type = config_file_type
        self.watch_interval = watch_interval
        self.current_config = init_config
        init_config.validate()
        self.app = app

        # Watcher thread
        self.running = True
        self.watcher_thread = threading.Thread(target=self._watch_worker)
        self.watcher_thread.start()
        assert hasattr(self.app, "state")

    def get_current_config(self) -> DynamicRouterConfig:
        return self.current_config

    def reconfigure_service_discovery(self, config: DynamicRouterConfig):
        """
        Reconfigures the router with the given config.
        """
        if config.service_discovery == "static":
            reconfigure_service_discovery(
                ServiceDiscoveryType.STATIC,
                app=self.app,
                urls=parse_static_urls(config.static_backends),
                models=parse_comma_separated_args(config.static_models),
                aliases=(
                    parse_static_aliases(config.static_aliases)
                    if config.static_aliases
                    else None
                ),
                model_labels=parse_comma_separated_args(config.static_model_labels),
                model_types=parse_comma_separated_args(config.static_model_types),
                static_backend_health_checks=config.static_backend_health_checks,
                static_backend_health_check_interval=config.static_backend_health_check_interval,
                static_backend_health_check_timeout_seconds=config.static_backend_health_check_timeout_seconds,
                prefill_model_labels=parse_comma_separated_args(
                    config.prefill_model_labels
                ),
                decode_model_labels=parse_comma_separated_args(
                    config.decode_model_labels
                ),
            )
        elif config.service_discovery == "k8s":
            reconfigure_service_discovery(
                ServiceDiscoveryType.K8S,
                app=self.app,
                namespace=config.k8s_namespace,
                port=config.k8s_port,
                label_selector=config.k8s_label_selector,
                prefill_model_labels=parse_comma_separated_args(
                    config.prefill_model_labels
                ),
                decode_model_labels=parse_comma_separated_args(
                    config.decode_model_labels
                ),
            )
        else:
            raise ValueError(
                f"Invalid service discovery type: {config.service_discovery}"
            )

        logger.info("DynamicConfigWatcher: Service discovery reconfiguration complete")

    def reconfigure_routing_logic(self, config: DynamicRouterConfig):
        """
        Reconfigures the router with the given config.
        """
        routing_logic = reconfigure_routing_logic(
            config.routing_logic,
            session_key=config.session_key,
            leastqueue_metrics_max_age=config.leastqueue_metrics_max_age,
            leastqueue_reservation_ttl=config.leastqueue_reservation_ttl,
            backend_running_targets=config.backend_running_targets,
            max_instance_failover_reroute_attempts=config.max_instance_failover_reroute_attempts,
            priority_header=config.priority_header,
            priority_field=config.priority_field,
            priority_default=config.priority_default,
            priority_threshold=config.priority_threshold,
            prefill_model_labels=parse_comma_separated_args(
                config.prefill_model_labels
            ),
            decode_model_labels=parse_comma_separated_args(config.decode_model_labels),
        )
        self.app.state.router = routing_logic
        logger.info("DynamicConfigWatcher: Routing logic reconfiguration complete")

    def reconfigure_batch_api(self, config: DynamicRouterConfig):
        """
        Reconfigures the router with the given config.
        """
        # TODO (ApostaC): Implement reconfigure_batch_api
        pass

    def reconfigure_stats(self, config: DynamicRouterConfig):
        """
        Reconfigures the router with the given config.
        """
        self.app.state.engine_stats_scraper.update_interval(
            config.engine_stats_interval
        )

    def reconfigure_callbacks(self, config: DynamicRouterConfig):
        """
        Reconfigures the router with the given config.
        """
        if config.callbacks:
            configure_custom_callbacks(config.callbacks, self.app)
        else:
            self.app.state.callbacks = None

    def reconfigure_all(self, config: DynamicRouterConfig):
        """
        Reconfigures the router with the given config.
        """
        config.validate()
        if (
            self.current_config.service_discovery == "static"
            and self.current_config.routing_logic
            in ("leastqueue", "leastkvperrequest", "leastnormalizedload", "roundrobin")
        ):
            return self._reconfigure_static(config)
        self.reconfigure_service_discovery(config)
        self.reconfigure_routing_logic(config)
        self.reconfigure_batch_api(config)
        self.reconfigure_stats(config)
        self.reconfigure_callbacks(config)

    def _reconfigure_static(self, config):
        if config.service_discovery != "static" or config.routing_logic not in (
            "leastqueue",
            "leastkvperrequest",
            "leastnormalizedload",
            "roundrobin",
        ):
            raise ValueError(
                "Live static updates support leastqueue, leastkvperrequest, leastnormalizedload and roundrobin only"
            )
        supported = {
            "static_backends",
            "static_models",
            "static_aliases",
            "static_model_labels",
            "static_model_types",
            "static_backend_health_checks",
            "static_backend_health_check_interval",
            "static_backend_health_check_timeout_seconds",
            "routing_logic",
            "leastqueue_metrics_max_age",
            "leastqueue_reservation_ttl",
            "backend_running_targets",
            "engine_stats_interval",
            "max_instance_failover_reroute_attempts",
        }
        changed = {
            field.name
            for field in fields(config)
            if getattr(config, field.name) != getattr(self.current_config, field.name)
        }
        if changed - supported:
            raise ValueError(f"Fields require restart: {sorted(changed - supported)}")
        old_discovery = get_service_discovery()
        new_discovery = None
        if any(name.startswith("static_") for name in changed):
            new_discovery = StaticServiceDiscovery(
                app=self.app,
                urls=config.static_backends.split(","),
                models=parse_comma_separated_args(config.static_models),
                aliases=(
                    parse_static_aliases(config.static_aliases)
                    if config.static_aliases
                    else None
                ),
                model_labels=parse_comma_separated_args(config.static_model_labels),
                model_types=parse_comma_separated_args(config.static_model_types),
                static_backend_health_checks=False,
                static_backend_health_check_interval=config.static_backend_health_check_interval,
                static_backend_health_check_timeout_seconds=config.static_backend_health_check_timeout_seconds,
            )
            old_ids = dict(
                zip(
                    zip(old_discovery.urls, old_discovery.models),
                    old_discovery.engines_id,
                )
            )
            new_discovery.engines_id = [
                old_ids.get((url, model), engine_id)
                for url, model, engine_id in zip(
                    new_discovery.urls, new_discovery.models, new_discovery.engines_id
                )
            ]
            if config.static_backend_health_checks:
                new_discovery.unhealthy_endpoint_hashes = list(
                    old_discovery.unhealthy_endpoint_hashes
                )
                new_discovery.start_health_check_task()
        router = self.app.state.router
        if config.routing_logic != self.current_config.routing_logic:
            if config.routing_logic == "leastnormalizedload":
                router = LeastNormalizedLoadRouter(config.backend_running_targets)
            else:
                router = {
                    "leastqueue": LeastQueueRouter,
                    "leastkvperrequest": LeastKVPerRequestRouter,
                    "roundrobin": RoundRobinRouter,
                }[config.routing_logic]()
        self.app.state.engine_stats_scraper.update_interval(
            config.engine_stats_interval
        )
        if isinstance(router, LeastQueueRouter):
            with router._lock:
                router.metrics_max_age = config.leastqueue_metrics_max_age
                router.reservation_ttl = (
                    0
                    if isinstance(
                        router, (LeastKVPerRequestRouter, LeastNormalizedLoadRouter)
                    )
                    else config.leastqueue_reservation_ttl
                )
                if isinstance(router, LeastNormalizedLoadRouter):
                    router.backend_running_targets = dict(
                        config.backend_running_targets
                    )
        router.max_instance_failover_reroute_attempts = (
            config.max_instance_failover_reroute_attempts
        )
        self.app.state.router = router
        if new_discovery is not None:
            swap_service_discovery(new_discovery)
            threading.Thread(target=old_discovery.close, daemon=True).start()
        self.current_config = config

    async def _apply_on_event_loop(self, config):
        self.reconfigure_all(config)
        self.current_config = config

    def _sleep_or_break(self, check_interval: float = 1):
        """
        Sleep for self.watch_interval seconds if self.running is True.
        Otherwise, break the loop.
        """
        for _ in range(int(self.watch_interval / check_interval)):
            if not self.running:
                break
            time.sleep(check_interval)

    def _watch_worker(self):
        """
        Watches the config file for changes and updates the DynamicRouterConfig accordingly.
        On every watch_interval, it will try loading the config file and compare the changes.
        If the config file has changed, it will reconfigure the system with the new config.
        """
        while self.running:
            try:
                if self.config_file_type == "YAML":
                    config = DynamicRouterConfig.from_yaml(self.config_path)
                elif self.config_file_type == "JSON":
                    config = DynamicRouterConfig.from_json(self.config_path)
                else:
                    raise ValueError("Unsupported config file type.")
                if config != self.current_config:
                    logger.info(
                        "DynamicConfigWatcher: Config changed, reconfiguring..."
                    )
                    loop = getattr(self.app.state, "event_loop", None)
                    if loop is None or not loop.is_running():
                        self._sleep_or_break()
                        continue
                    asyncio.run_coroutine_threadsafe(
                        self._apply_on_event_loop(config), loop
                    ).result()
                    logger.info("DynamicConfigWatcher: Config reconfiguration complete")
                    self.current_config = config
            except Exception as e:
                logger.warning(f"DynamicConfigWatcher: Error loading config file: {e}")

            self._sleep_or_break()

    def close(self):
        """
        Closes the watcher thread.
        """
        self.running = False
        self.watcher_thread.join()
        logger.info("DynamicConfigWatcher: Closed")


def initialize_dynamic_config_watcher(
    config_path: str,
    config_file_type: Literal["YAML", "JSON"],
    watch_interval: int,
    init_config: DynamicRouterConfig,
    app: FastAPI,
):
    """
    Initializes the DynamicConfigWatcher with the given config path, file type and watch interval.
    """
    return DynamicConfigWatcher(
        config_path, config_file_type, watch_interval, init_config, app
    )


def get_dynamic_config_watcher() -> DynamicConfigWatcher:
    """
    Returns the DynamicConfigWatcher singleton.
    """
    return DynamicConfigWatcher(_create=False)
