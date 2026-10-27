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
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Dict

import requests
from prometheus_client.parser import text_string_to_metric_families

from vllm_router.log import init_logger
from vllm_router.service_discovery import get_service_discovery
from vllm_router.utils import SingletonMeta

logger = init_logger(__name__)


@dataclass
class EngineStats:
    # Number of running requests
    num_running_requests: int = 0
    # Number of queuing requests
    num_queuing_requests: int = 0
    # GPU prefix cache hit rate (as used in some panels)
    gpu_prefix_cache_hit_rate: float = 0.0
    gpu_prefix_cache_hits_total: int = 0
    gpu_prefix_cache_queries_total: int = 0
    # GPU KV usage percentage (new field for dashboard "GPU KV Usage Percentage")
    gpu_cache_usage_perc: float = 0.0
    observed_at: float = 0.0
    request_metrics_valid: bool = False

    @staticmethod
    def from_vllm_scrape(vllm_scrape: str):
        """
        Parse the vllm scrape string and return a EngineStats object

        Args:
            vllm_scrape (str): The vllm scrape string

        Returns:
            EngineStats: The EngineStats object

        Note:
            Assume vllm only runs a single model
        """
        running_by_labels = {}
        waiting_by_labels = {}
        invalid_counts = False
        gpu_prefix_cache_hit_rate = 0.0
        gpu_prefix_cache_hits_total = 0
        gpu_prefix_cache_queries_total = 0
        gpu_cache_usage_perc = 0.0

        for family in text_string_to_metric_families(vllm_scrape):
            for sample in family.samples:
                if sample.name == "vllm:num_requests_running":
                    counts = running_by_labels
                elif sample.name == "vllm:num_requests_waiting":
                    counts = waiting_by_labels
                elif sample.name == "vllm:gpu_prefix_cache_hit_rate":
                    gpu_prefix_cache_hit_rate = sample.value
                elif sample.name == "vllm:gpu_prefix_cache_hits_total":
                    gpu_prefix_cache_hits_total = sample.value
                elif sample.name == "vllm:gpu_prefix_cache_queries_total":
                    gpu_prefix_cache_queries_total = sample.value
                elif sample.name in (
                    "vllm:gpu_cache_usage_perc",
                    "vllm:kv_cache_usage_perc",
                ):
                    # vLLM renamed gpu_cache_usage_perc to kv_cache_usage_perc;
                    # newer releases only export the new name.
                    gpu_cache_usage_perc = sample.value

                if sample.name in (
                    "vllm:num_requests_running",
                    "vllm:num_requests_waiting",
                ):
                    labels = tuple(sorted(sample.labels.items()))
                    if (
                        not math.isfinite(sample.value)
                        or sample.value < 0
                        or not float(sample.value).is_integer()
                        or labels in counts
                    ):
                        invalid_counts = True
                    else:
                        counts[labels] = int(sample.value)

        return EngineStats(
            observed_at=time.monotonic(),
            request_metrics_valid=(
                bool(running_by_labels)
                and running_by_labels.keys() == waiting_by_labels.keys()
                and not invalid_counts
            ),
            num_running_requests=sum(running_by_labels.values()),
            num_queuing_requests=sum(waiting_by_labels.values()),
            gpu_prefix_cache_hit_rate=gpu_prefix_cache_hit_rate,
            gpu_prefix_cache_hits_total=gpu_prefix_cache_hits_total,
            gpu_prefix_cache_queries_total=gpu_prefix_cache_queries_total,
            gpu_cache_usage_perc=gpu_cache_usage_perc,
        )


class EngineStatsScraper(metaclass=SingletonMeta):
    def __init__(self, scrape_interval: float):
        """
        Initialize the scraper to periodically fetch metrics from all serving engines.

        Args:
            scrape_interval (float): The interval in seconds
                to scrape the metrics.

        Raises:
            ValueError: if the service discover module is have
            not been initialized.

        """
        # Allow multiple calls but require the first call provide scrape_interval.
        if hasattr(self, "_initialized"):
            return
        if (
            scrape_interval is None
            or not math.isfinite(scrape_interval)
            or scrape_interval <= 0
        ):
            raise ValueError(
                "EngineStatsScraper must be initialized with scrape_interval"
            )
        self.engine_stats: Dict[str, EngineStats] = {}
        self.engine_stats_lock = threading.Lock()
        self.scrape_interval = scrape_interval
        self._wake_event = threading.Event()

        # scrape thread
        self.running = True
        self.scrape_thread = threading.Thread(target=self._scrape_worker, daemon=True)
        self.scrape_thread.start()
        self._initialized = True

    def _scrape_one_endpoint(self, url: str):
        """
        Scrape metrics from a single serving engine.

        Args:
            url (str): The URL of the serving engine (does not contain endpoint)
        """
        observed_at = time.monotonic()
        try:
            response = requests.get(url + "/metrics", timeout=self.scrape_interval)
            response.raise_for_status()
            engine_stats = EngineStats.from_vllm_scrape(response.text)
            engine_stats.observed_at = observed_at
        except Exception as e:
            logger.error(f"Failed to scrape metrics from {url}: {e}")
            return None
        return engine_stats

    def _scrape_metrics(self):
        """
        Scrape metrics from all serving engines.

        Scrape metrics from all serving engines by calling
        _scrape_one_endpoint on each of them. The metrics are
        stored in self.engine_stats.

        """
        collected_engine_stats = {}
        urls = sorted(
            {info.url for info in get_service_discovery().get_endpoint_info()}
        )
        logger.debug(f"Scraping metrics from {len(urls)} serving engine(s)")
        if urls:
            with ThreadPoolExecutor(max_workers=min(16, len(urls))) as executor:
                for url, engine_stats in zip(
                    urls, executor.map(self._scrape_one_endpoint, urls)
                ):
                    if engine_stats is not None:
                        collected_engine_stats[url] = engine_stats

        with self.engine_stats_lock:
            old_urls = list(self.engine_stats.keys())
            for old_url in old_urls:
                if old_url not in collected_engine_stats:
                    del self.engine_stats[old_url]
            for url, stats in collected_engine_stats.items():
                self.engine_stats[url] = stats

    def _sleep_or_break(self, check_interval: float = 1):
        """
        Sleep for self.scrape_interval seconds if self.running is True.
        Otherwise, break the loop.
        """
        self._wake_event.wait(self.scrape_interval)
        self._wake_event.clear()

    def update_interval(self, interval: float):
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("Scrape interval must be positive and finite")
        self.scrape_interval = interval
        self._wake_event.set()

    def _scrape_worker(self):
        """
        Periodically scrape metrics from all serving engines in the background.

        This function will loop forever and sleep for self.scrape_interval
        seconds between each scrape. It will call _scrape_metrics to scrape
        metrics from all serving engines and store them in self.engine_stats.

        """
        while self.running:
            self._scrape_metrics()
            self._sleep_or_break()

    def get_engine_stats(self) -> Dict[str, EngineStats]:
        """
        Retrieve a copy of the current engine statistics.

        Returns:
            A dictionary mapping engine URLs to their respective EngineStats objects.
        """
        with self.engine_stats_lock:
            return self.engine_stats.copy()

    def get_health(self) -> bool:
        """
        Check if the EngineStatsScraper is healthy

        Returns:
            bool: True if the EngineStatsScraper is healthy,
                False otherwise
        """
        return self.scrape_thread.is_alive()

    def close(self):
        """
        Stop the background thread and cleanup resources.
        """
        self.running = False
        self._wake_event.set()
        self.scrape_thread.join()


def initialize_engine_stats_scraper(scrape_interval: float) -> EngineStatsScraper:
    return EngineStatsScraper(scrape_interval)


def get_engine_stats_scraper() -> EngineStatsScraper:
    # This call returns the already-initialized instance (or raises an error if not yet initialized)
    return EngineStatsScraper()
