# vLLM Production Stack: reference stack for production vLLM deployment

This fork adds `leastqueue`, `leastkvperrequest`, and `leastnormalizedload` routing and live updates for static router backends. It is based on [vLLM Production Stack](https://github.com/vllm-project/production-stack); the features below require a router image built from this repository.

| [**Blog**](https://lmcache.github.io) | [**Docs**](https://docs.vllm.ai/projects/production-stack) | [**Production-Stack Slack Channel**](https://communityinviter.com/apps/vllm-dev/join-vllm-developers-slack) | [**LMCache Slack**](https://join.slack.com/t/lmcacheworkspace/shared_invite/zt-2viziwhue-5Amprc9k5hcIdXT7XevTaQ) | [**Interest Form**](https://forms.gle/mQfQDUXbKfp2St1z7) |

<p align="center">
  <b>English</b> · <a href="README.zh.md">简体中文</a>
</p>

## Latest News

- 📄 [Official documentation](https://docs.vllm.ai/projects/production-stack) released for production-stack!
- ✨ [Cloud Deployment Tutorials](https://github.com/vllm-project/production-stack/blob/main/tutorials) for Lambda Labs, AWS EKS, Google GCP are out!
- 🛤️ 2026 roadmap is released! [Join the discussion now](https://github.com/vllm-project/production-stack/issues/855)!
- 🔥 vLLM Production Stack is released! Check out our [release blogs](https://blog.lmcache.ai/2025-01-21-stack-release) posted on January 22, 2025.

## Community Events

We host **bi-weekly** community meetings at the following timeslot:

- Every other Tuesdays at 5:30 PM PT – [Add to Calendar](https://drive.google.com/uc?export=download&id=1D4SqQiqzdSx_xsEwS0QTd592zd3Xourh)

All are welcome to join!

## Introduction

**vLLM Production Stack** project provides a reference implementation on how to build an inference stack on top of vLLM, which allows you to:

- 🚀 Scale from a single vLLM instance to a distributed vLLM deployment without changing any application code
- 💻 Monitor the metrics through a web dashboard
- 😄 Enjoy the performance benefits brought by request routing and KV cache offloading

## Step-By-Step Tutorials

0. How To [*Install Kubernetes (kubectl, helm, minikube, etc)*](https://github.com/vllm-project/production-stack/blob/main/tutorials/00-install-kubernetes-env.md)?
1. How to [*Deploy Production Stack on Major Cloud Platforms (AWS, GCP, Lambda Labs, Azure)*](https://github.com/vllm-project/production-stack/blob/main/tutorials/cloud_deployments)?
2. How To [*Set up a Minimal vLLM Production Stack*](https://github.com/vllm-project/production-stack/blob/main/tutorials/01-minimal-helm-installation.md)?
3. How To [*Customize vLLM Configs (optional)*](https://github.com/vllm-project/production-stack/blob/main/tutorials/02-basic-vllm-config.md)?
4. How to [*Load Your LLM Weights*](https://github.com/vllm-project/production-stack/blob/main/tutorials/03-load-model-from-pv.md)?
5. How to [*Launch Different LLMs in vLLM Production Stack*](https://github.com/vllm-project/production-stack/blob/main/tutorials/04-launch-multiple-model.md)?
6. How to [*Enable KV Cache Offloading with LMCache*](https://github.com/vllm-project/production-stack/blob/main/tutorials/05-offload-kv-cache.md)?

## Architecture

The stack is set up using [Helm](https://helm.sh/docs/), and contains the following key parts:

- **Serving engine**: The vLLM engines that run different LLMs.
- **Request router**: Directs requests to appropriate backends based on routing keys or session IDs to maximize KV cache reuse.
- **Observability stack**: monitors the metrics of the backends through [Prometheus](https://github.com/prometheus/prometheus) + [Grafana](https://grafana.com/)

<p align="center">
  <img src="https://github.com/user-attachments/assets/8f05e7b9-0513-40a9-9ba9-2d3acca77c0c" alt="Architecture of the stack" width="80%"/>
</p>

## Roadmap

We are actively working on this project and will release the following features soon. Please stay tuned!

- **Autoscaling** based on vLLM-specific metrics
- Support for **disaggregated prefill**
- **Router improvements** (e.g., more performant router using non-python languages, KV-cache-aware routing algorithm, better fault tolerance, etc)

## Deploying the stack via Helm

### Prerequisites

- A running Kubernetes (K8s) environment with GPUs
  - Run `cd utils && bash install-minikube-cluster.sh`
  - Or follow our [tutorial](tutorials/00-install-kubernetes-env.md)

### Deployment

vLLM Production Stack can be deployed via helm charts. Clone the repo to local and execute the following commands for a minimal deployment:

```bash
git clone https://github.com/vllm-project/production-stack.git
cd production-stack/
helm repo add vllm https://vllm-project.github.io/production-stack
helm install vllm vllm/vllm-stack -f tutorials/assets/values-01-minimal-example.yaml
```

The deployed stack provides the same [**OpenAI API interface**](https://docs.vllm.ai/en/latest/serving/openai_compatible_server.html?ref=blog.mozilla.ai#openai-compatible-server) as vLLM, and can be accessed through kubernetes service.

To validate the installation and send a query to the stack, refer to [this tutorial](tutorials/01-minimal-helm-installation.md).

For more information about customizing the helm chart, please refer to [values.yaml](https://github.com/vllm-project/production-stack/blob/main/helm/values.yaml) and our other [tutorials](https://github.com/vllm-project/production-stack/tree/main/tutorials).

### Uninstall

```bash
helm uninstall vllm
```

## Grafana Dashboard

### Features

The Grafana dashboard provides the following insights:

1. **Available vLLM Instances**: Displays the number of healthy instances.
2. **Request Latency Distribution**: Visualizes end-to-end request latency.
3. **Time-to-First-Token (TTFT) Distribution**: Monitors response times for token generation.
4. **Number of Running Requests**: Tracks the number of active requests per instance.
5. **Number of Pending Requests**: Tracks requests waiting to be processed.
6. **GPU KV Usage Percent**: Monitors GPU KV cache usage.
7. **GPU KV Cache Hit Rate**: Displays the hit rate for the GPU KV cache.

<p align="center">
  <img src="https://github.com/user-attachments/assets/05766673-c449-4094-bdc8-dea6ac28cb79" alt="Grafana dashboard to monitor the deployment" width="80%"/>
</p>

### Configuration

See the details in [`helm/README.md`](./helm/README.md#Observability)

## Router

The router ensures efficient request distribution among backends. It supports:

- Routing to endpoints that run different models
- Exporting observability metrics for each serving engine instance, including QPS, time-to-first-token (TTFT), number of pending/running/finished requests, and uptime
- Automatic service discovery and fault tolerance via the Kubernetes API
- Model aliases
- Multiple routing algorithms:
  - Round-robin routing
  - Least-queue routing (`leastqueue`)
  - Least KV usage per running request (`leastkvperrequest`)
  - Least normalized running load (`leastnormalizedload`)
  - Session-ID based routing
  - Prefix-aware routing (WIP)

Please refer to the [router documentation](./src/vllm_router/README.md) for more details.

### Least-queue routing

For the requested model, `leastqueue` selects a healthy backend using the lowest `(vllm:num_requests_waiting, vllm:num_requests_running + recent_assignments)` pair. Equal scores rotate by least recent selection. Each assignment counts locally for one second by default, so concurrent requests do not all select the same backend from an unchanged metrics snapshot. The adjustment applies to running requests only; a backend with fewer actual waiting requests still takes priority.

The router scrapes backend `/metrics` endpoints directly, with up to 16 concurrent scrapes. Both request metrics must be present, valid, and have matching labels. Metrics from multiple data-parallel engines at one URL are summed. With the default five-second freshness limit, a backend without fresh metrics is excluded while any backend has fresh metrics. If none do, routing rotates among healthy backends; if none are healthy, the request receives 503. Static backend health checks can provide a separate health signal, but they send real test requests to the backends.

Install this repository with Python 3.12 or later, then start the router with your own backend URLs and model names:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
vllm-router --port 8000 \
  --service-discovery static \
  --static-backends "http://vllm-1:8000,http://vllm-2:8000" \
  --static-models "my-model,my-model" \
  --static-model-types "chat,chat" \
  --static-backend-health-checks \
  --routing-logic leastqueue \
  --engine-stats-interval 1 \
  --leastqueue-metrics-max-age 5 \
  --leastqueue-reservation-ttl 1 \
  --max-instance-failover-reroute-attempts 1
```

The default scrape interval is 30 seconds, so set `--engine-stats-interval` below the metrics freshness limit for this strategy. Set `--leastqueue-reservation-ttl 0` to disable the local assignment adjustment. These reservations are per router process; multiple router replicas do not share them. Connection failures can reroute before a response starts, while HTTP error responses pass through and started streams are not replayed.

For Helm deployments, build and push this repository's router image and set `routerSpec.routingLogic: leastqueue`, `routerSpec.engineScrapeInterval: 1`, and the two least-queue flags in `routerSpec.extraArgs`.

### Least KV usage per running request

Set `--routing-logic leastkvperrequest` to sort by `(Waiting, GPU KV cache usage / Running)`, with a ratio of **0 when Running is 0**. Waiting always takes priority; equal scores rotate by least recent selection. This mode uses measured Running only and does not apply short-lived assignment compensation. It can favor a busier backend when that backend has a smaller ratio, and an idle backend scores zero even with high KV usage. No KV high-water threshold overrides this ordering.

Use `--engine-stats-interval 1` and `--leastqueue-metrics-max-age 5` as in the least-queue example. The reservation TTL option applies only to `leastqueue`. Both `vllm:kv_cache_usage_perc` and the older `vllm:gpu_cache_usage_perc` are supported, with the newer metric preferred when both exist. Values must be finite fractions between 0 and 1 with labels matching the request metrics. For multiple engines at one URL, KV usage is the arithmetic mean of engine fractions and Running is their sum; equal cache capacities are assumed for this mean.

Running greater than zero requires valid KV metrics. Running equal to zero needs only valid request metrics. Backends without fresh usable metrics are excluded while another backend has usable metrics; otherwise, healthy backends rotate. Static configurations can hot-switch between `leastkvperrequest`, `leastqueue`, and `roundrobin`. In YAML, set `routing_logic: leastkvperrequest`; in Helm, set `routerSpec.routingLogic: leastkvperrequest`.

Run `python -m pytest src/tests/test_leastkvperrequest_router.py src/tests/test_hot_reload.py -q` to check the ratio policy and hot updates.

### Least normalized running load

For static backends with different target concurrency, set `routing_logic: leastnormalizedload`. The router sorts by `(Waiting, actual Running / target Running)`, then rotates equal scores. Waiting always takes priority. With equal Waiting, Running 50 against a target of 100 wins over Running 20 against a target of 30. A target is a relative capacity weight, not a hard limit: ratios can exceed 1, and zero Running scores 0. This mode uses measured Running without short-lived assignment compensation, so repeated requests can select the same backend until metrics refresh.

Use [`examples/wy-router/normalized-load.yaml`](./examples/wy-router/normalized-load.yaml) as a complete starting configuration:

```yaml
backend_running_targets:
  "http://vllm-1.example.com:8000": 100
  "http://vllm-2.example.com:8000": 30
```

Replace the example URLs in both `static_models` and `backend_running_targets`, then run `vllm-router --dynamic-config-yaml examples/wy-router/normalized-load.yaml`. Every distinct backend URL must have exactly one positive integer target; missing targets, unknown URLs, booleans and fractional values are rejected. The URL must match exactly, including any trailing slash. Models sharing a URL share its target. This mode requires static service discovery so every backend can be validated before activation.

CLI users can pass `--backend-running-targets '{"http://vllm-1.example.com:8000":100,"http://vllm-2.example.com:8000":30}'` with the corresponding static backends and models. JSON configuration uses the same object. Targets and backend lists can be hot-updated together, and static configurations can switch between this mode, `leastqueue`, `leastkvperrequest`, and `roundrobin`. Invalid updates retain the last successful configuration. Removed backends receive no new requests; existing streams continue.

Metrics freshness uses `--leastqueue-metrics-max-age` (default 5 seconds); use `--engine-stats-interval 1`. Missing or stale request metrics follow the existing healthy-backend fallback. Tests: `python -m pytest src/tests/test_leastnormalizedload_router.py src/tests/test_hot_reload.py src/tests/test_leastqueue_http.py -q`.

### Live static backend updates

Use `--dynamic-config-yaml` or `--dynamic-config-json` to watch a complete router configuration. Static `leastqueue` and `roundrobin` configurations support live changes to backend URLs, model mappings and aliases, health-check settings, the least-queue parameters, `engine_stats_interval`, and `max_instance_failover_reroute_attempts`. Backend and parameter updates under the same strategy preserve existing short-lived assignments. Switching between these two strategies is supported for static service discovery. Other changes may require a restart.

An example YAML file is at [`examples/wy-router/router.yaml`](./examples/wy-router/router.yaml). Replace its backend URLs and model name for your deployment. On a Linux Docker host, mount the configuration directory so editors can replace the file atomically:

```bash
mkdir -p /opt/wy-router/config
cp examples/wy-router/router.yaml /opt/wy-router/config/router.yaml
docker build -f docker/Dockerfile -t wy-router:leastqueue .
docker run -d --name wy-router --restart unless-stopped \
  -p 4020:8080 \
  -v /opt/wy-router/config:/app/config:ro \
  wy-router:leastqueue \
  --host 0.0.0.0 --port 8080 \
  --dynamic-config-yaml /app/config/router.yaml
```

Edit `static_backends` to add or remove URLs; the watcher checks for changes about every ten seconds. New backends need a successful metrics scrape before they join metrics-based selection when another backend has fresh metrics. At least one backend must remain. Invalid or unsupported updates log a warning and leave the last successful configuration active. Check `docker logs --since 1m wy-router` and the `dynamic_config` field of `curl http://localhost:4020/health` to confirm an update. Removed backends receive no new requests, while in-flight requests and streams continue on their existing connections; wait for them to finish before stopping those vLLM instances.

Run the focused tests with `python -m pytest src/tests/test_leastqueue_router.py src/tests/test_leastqueue_http.py src/tests/test_hot_reload.py -q`. They use simulated HTTP backends and do not replace throughput or latency testing with real vLLM instances.

## Contributing

We welcome and value any contributions and collaborations. Please check out [CONTRIBUTING.md](CONTRIBUTING.md) for how to get involved.

## License

This project is licensed under Apache License 2.0. See the `LICENSE` file for details.

## Sponsors

We are grateful to our sponsors who support our development and benchmarking efforts:

<p align="center">
  <a href="https://gmicloud.ai">
    <img src="https://cdn.prod.website-files.com/6683d8c52e4e62685a8d90cf/67a0a0064683945b0cf77f25_GMI%20Cloud%20Logo_Black.svg" alt="GMI Cloud Logo" width="200"/>
  </a>
</p>

---

For any issues or questions, feel free to open an issue or contact us ([@ruizhang0101](https://github.com/ruizhang0101), [@ApostaC](https://github.com/ApostaC), [@YuhanLiu11](https://github.com/YuhanLiu11), [@Shaoting-Feng](https://github.com/Shaoting-Feng)).
