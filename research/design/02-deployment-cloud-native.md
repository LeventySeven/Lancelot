# Lancelot — Deployment, Ops & Cloud-Native Reality (2026)

> Decision-grade design research. Sibling to `SYNTHESIS.md`. Every claim is labelled
> **KNOWN** (sourced URL or cross-ref to a local teardown), **INFERRED** (reasoned from
> sourced facts), or **RECOMMENDATION** (Lancelot design call). The ops surface a
> framework ships is an *adoption gate*, not a nice-to-have: in 2026 a Python web
> framework that does not have first-class probes, signal handling, OTel and a
> credible Dockerfile loses to FastAPI before the first benchmark is run.
>
> Scope: containerization, Kubernetes, serverless/PaaS, observability, 12-factor
> config, and the ASGI-compatibility migration question. The *core* design
> (Rust+Python, GIL, streaming) lives in `SYNTHESIS.md`; this file is the
> envelope that core ships inside.

---

## Executive summary — the requirements, up front (12 lines)

1. **Ship dedicated probe endpoints**: `/livez` (process alive, no deps), `/readyz` (deps OK, accept traffic), `/startupz` (slow init done) — separate handlers, served from the **const-tier** (zero-GIL, sub-ms, never blocked by a saturated Python pool).
2. **`SIGTERM` = graceful drain by default**: stop accepting, finish in-flight (bounded by deadline), close upstream pools, exit 0. Configurable `shutdown_timeout`. This is the single biggest gap in Robyn (`process.kill()` drops in-flight — `robyn/03 §0`).
3. **cgroup-aware worker count** — never `os.cpu_count()`; read cgroup v2 `cpu.max` / v1 quota. Robyn's `os.cpu_count()` bug forks 64 procs on a 2-core limit and thrashes (`robyn/03 §0`).
4. **Prometheus `/metrics` + OTLP exporter built in**, opt-out not opt-in. RED metrics + GenAI semantic-convention spans (LLM model, token usage) for AI handlers.
5. **Structured JSON logs to stdout** with trace/request-ID correlation; 12-factor env-var config; no config files required to boot.
6. **Ship a reference multi-stage Dockerfile + distroless target** hitting **< 60 MB** final image (competitive vs ~120–250 MB FastAPI+uvicorn) and a non-root, healthcheck-bearing image.
7. **Ship reference K8s manifests**: Deployment with all three probes wired, `preStop` sleep, `terminationGracePeriodSeconds`, PDB, HPA (CPU + a custom RPS/queue-depth metric path), `readOnlyRootFilesystem`.
8. **Cold start is a marketing weapon**: a Rust core should import + bind in tens of ms vs uvicorn+FastAPI's hundreds of ms — measure and publish; it directly sells Cloud Run / Lambda / scale-to-zero.
9. **ASGI compat is an adoption bridge, not the core ABI**: ship a native Rust middleware ABI for speed **and** an ASGI-compat shim (`app.mount_asgi()` / Lancelot-as-ASGI-callable) so a FastAPI shop migrates route-by-route. **Recommendation: dual-tier, ASGI as the compatibility tier.**
10. **Lambda/serverless adapter**: provide a built-in invocation adapter (Lambda Web Adapter is the clean path — no code change) so Lancelot runs on Lambda/Cloud Run/Fly without a Mangum-style rewrite.
11. **Healthcheck + `/metrics` must never need the GIL or the handler pool** — they are the const-tier's first customer; a saturated app must still pass liveness or K8s kills a working pod.
12. **Provide the manifests, don't just document them**: `lancelot init --docker --k8s` scaffolds Dockerfile, `.dockerignore`, compose, and K8s YAML. The framework that ships the ops surface wins the startup that has no platform team.

---

## 1. Docker — the container image is the first impression

### 1.1 Why this matters more for Lancelot than for FastAPI

FastAPI ships a pure-Python wheel: `pip install fastapi` is trivial, any base
image works, and the official guidance is "start `FROM python:3.14`, `pip
install`, done" (KNOWN — fastapi.tiangolo.com/deployment/docker). Lancelot ships
a **compiled PyO3/maturin extension** (`SYNTHESIS §4.7`: abi3 wheels,
manylinux_2_28 + musllinux + macOS universal2 + win-x64). That changes the
Docker story in three ways:

- **A naive Dockerfile that `pip install`s Lancelot from source pulls a Rust
  toolchain into the image** → 1.5–2 GB. The teardown's packaging decision
  (abi3 wheels, one per platform, `SYNTHESIS §4.7`) is precisely what makes a
  *small* image possible: the wheel is prebuilt, the runtime image needs **no
  Rust, no cargo, no gcc**. The framework's packaging choice and its Docker
  story are the same decision.
- **glibc vs musl matters.** A manylinux_2_28 wheel needs glibc ≥ 2.28; it will
  not load on Alpine (musl). Lancelot must ship a **musllinux wheel**
  (`SYNTHESIS §4.7` already calls this out: "musllinux (Alpine/startup
  Dockerfiles)") *because startups reach for `python:alpine` by reflex*. If the
  musllinux wheel is missing, the Alpine Dockerfile silently falls back to an
  sdist build (Rust toolchain, slow, often fails) — the worst adoption
  experience possible. **The musllinux wheel is an ops requirement, not a nice-to-have.**
- **The Rust core is a cold-start asset** (see §3). It only pays off if the
  image actually ships the prebuilt binary and starts fast — a 2 GB image with
  a 4 s pull defeats the point.

### 1.2 Base image landscape (2026 numbers)

KNOWN — compressed/uncompressed sizes from Chainguard/Medium measurements
(medium.com/@bdalpe/wolfi-a-small-glibc-based-python-container-base,
edu.chainguard.dev, oneuptime.com/blog/2026-02-09-distroless-chainguard):

| Base | libc | ~Compressed | ~Uncompressed | Wheel compat | Shell/pkg mgr | Notes |
|---|---|---|---|---|---|---|
| `python:3.13` (full) | glibc | ~340 MB | ~1 GB | all | yes | never ship this |
| `python:3.13-slim` (bookworm) | glibc | ~28 MB base + Python | ~120–150 MB typ. final | manylinux ✓ | yes (apt, sh) | the safe default |
| `python:3.13-alpine` | **musl** | ~18 MB | ~53 MB | **musllinux only** | yes (apk, sh) | needs musllinux wheel or it source-builds |
| `cgr.dev/chainguard/python` (Wolfi) | glibc | ~22 MB | ~65 MB | manylinux ✓ | minimal | rolling CVE patches, glibc → wheels install |
| `gcr.io/distroless/python3-debian12:nonroot` | glibc | small | ~50 MB + venv | manylinux ✓ | **none** (no sh/apt/pip) | UID 65532, K8s `runAsNonRoot`-verifiable |
| `scratch` | none | ~0 | code only | static-musl only | none | only viable if Lancelot were a static binary (it embeds CPython → not realistic) |

INFERRED: a typical **FastAPI + uvicorn + pydantic** image on `python:3.13-slim`
with a clean multi-stage build lands **~120–250 MB** uncompressed
(medium.com/@ThinkingLoop "FastAPI Images Under 100MB" shows <100 MB is
*achievable* but the unoptimized norm is well above). A Lancelot image can be
**materially smaller** because the heavy dependency (the HTTP engine, JSON,
validation) is *inside the compiled extension* — there is no `uvicorn`,
`httptools`, `uvloop`, `starlette`, `anyio`, `h11` dependency tree to install.
The Python side of Lancelot is thin glue.

**RECOMMENDATION — image size target:** Lancelot's reference distroless image
should target **< 60 MB uncompressed** (distroless base ~50 MB + the abi3 `.so`
which is single-digit MB + thin Python glue). Headline claim for the docs:
*"Smaller than a FastAPI image, because the framework is one prebuilt `.so`, not
a dependency tree."* This is a real, defensible competitive number — publish a
`docker images` screenshot in the README.

### 1.3 The reference multi-stage Dockerfile (RECOMMENDATION — ship this)

Lancelot ships **three** reference Dockerfiles (`lancelot init --docker`
scaffolds them):

**(a) Distroless (default, production):**

```dockerfile
# ---- build stage: resolve deps + the abi3 wheel into a venv ----
FROM python:3.13-slim-bookworm AS build
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
# deps first → cache layer (changes rarely)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev
# Lancelot wheel installs here as a prebuilt manylinux_2_28 abi3 wheel — NO rust toolchain
COPY . .
RUN uv sync --frozen --no-dev

# ---- runtime stage: distroless, nonroot, no shell ----
FROM gcr.io/distroless/python3-debian12:nonroot
WORKDIR /app
COPY --from=build --chown=65532:65532 /app/.venv /app/.venv
COPY --from=build --chown=65532:65532 /app/app /app/app
ENV PATH="/app/.venv/bin:$PATH"
USER 65532:65532
EXPOSE 8000
# distroless has no curl/sh; healthcheck calls the interpreter against /livez
HEALTHCHECK --interval=10s --timeout=2s --start-period=5s --retries=3 \
  CMD ["python","-c","import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/livez').status==200 else 1)"]
ENTRYPOINT ["python","-m","lancelot","run","app.main:app","--host","0.0.0.0","--port","8000"]
```

Key choices and their justification:

- **Build/runtime on the same Debian 12 family** so glibc/libstdc++ match
  exactly between the venv that resolved the wheel and the runtime — prevents
  the classic "wheel built against newer glibc won't load" failure (KNOWN —
  nerdleveltech.com/distroless-python-containers-with-uv-tutorial: *"Both stages
  run on Debian 12 (bookworm), so glibc and libstdc++ versions match exactly"*).
- **`uv` not `pip`** — 2026 default; deterministic `uv.lock`, byte-compile,
  copy-mode link so the venv is relocatable across stages (KNOWN —
  nerdleveltech 2026 tutorial uses uv 0.11.x; FastAPI's own modern guidance
  dropped the gunicorn base image, future-architect.github.io/articles/20250602a).
- **Deps layer before app layer** — Docker cache: dependency install (slow) is
  cached, app-code change rebuilds only the last layer (KNOWN —
  fastapi.tiangolo.com/deployment/docker: *"Downloading and installing … could
  take minutes, but using the cache would take seconds"*).
- **`USER 65532:65532` numeric** — distroless `nonroot`; numeric form so
  Kubernetes `securityContext.runAsNonRoot` can verify without a UID lookup
  table (KNOWN — nerdleveltech 2026: *"numeric form so Kubernetes runAsNonRoot
  can verify it"*).
- **No shell in distroless** → the `HEALTHCHECK` cannot `curl`; it shells out to
  the bundled CPython instead (INFERRED standard distroless practice — distroless
  ships `python3` but no `/bin/sh`, `curl`, `apt`, `pip`; KNOWN distroless
  inventory from oneuptime.com/blog/2026-02-09-distroless-chainguard).
- **`ENTRYPOINT` exec form** — array form, not shell string, so SIGTERM reaches
  PID 1 (the Lancelot supervisor) and graceful shutdown actually runs (KNOWN —
  FastAPI docs explicitly warn: *"always use the exec form … to ensure …
  shutdown gracefully"*; cross-ref §2.3 — this is the same reason Robyn's
  missing graceful shutdown is fatal, `robyn/03 §0`).

**(b) Alpine/musl (the startup-reflex Dockerfile):** identical structure but
`FROM python:3.13-alpine` build + `FROM cgr.dev/chainguard/python:latest` or a
musl distroless runtime; **requires Lancelot's musllinux abi3 wheel** to exist
(`SYNTHESIS §4.7`). Ship this because startups type `python:alpine` on instinct;
if the wheel is missing they hit a Rust source build and blame Lancelot.

**(c) Chainguard/Wolfi (security-conscious / enterprise):** `FROM
cgr.dev/chainguard/python:latest-dev AS build` → `FROM
cgr.dev/chainguard/python:latest`; glibc so the manylinux wheel works,
rolling-release CVE patching, minimal surface (KNOWN —
chainguard.dev/unchained/minimal-container-images; Wolfi ~22 MB compressed).

### 1.4 .dockerignore — the single biggest size bug

KNOWN — medium.com/@ThinkingLoop and davidmuraya.com both name a missing
`.dockerignore` as *"the single biggest reason tiny images end up at hundreds of
MB"* (build context drags `.git`, `target/`, `.venv`, test data into the build).
`lancelot init --docker` must scaffold a `.dockerignore`:

```
.git/
target/            # Rust build artifacts — huge, never needed in the image
.venv/
__pycache__/
*.pyc
.pytest_cache/
.ruff_cache/
tests/
*.md
.env*
dist/
```

`target/` is Lancelot-specific and critical: a developer who built the Rust
core locally has a multi-GB `target/` that would otherwise be copied into the
build context. RECOMMENDATION: also `.git/` (Rust crates with vendored deps).

### 1.5 SBOM + vulnerability scanning (table stakes for enterprise adoption)

KNOWN — distroless's headline value is **fewer CVEs** because there is no
shell/apt/openssl surface for scanners to flag
(chainguard.dev/unchained/minimal-container-images,
oneuptime.com/blog/2026-02-09-distroless-chainguard: *"more important than size
is the smaller attack surface that results in fewer CVEs"*). For Lancelot:

- **Trivy / Grype scan in the README and CI template.** A Rust extension has its
  own supply chain (the crate tree from `SYNTHESIS §4.1`: pyo3, hyper, tower,
  tokio). Ship a `cargo auditable`-built `.so` so the crate SBOM is embedded in
  the binary and Grype/Trivy can read Rust deps, not just Python deps.
  RECOMMENDATION: `maturin build` with `cargo auditable` so the wheel carries a
  CycloneDX-readable dependency manifest.
- **`docker scout` / Trivy `--scanners vuln,license` in the scaffolded CI.**
- The distroless image will scan to **~0 OS CVEs**; the differentiator vs a
  FastAPI-on-slim image (which carries the full Debian apt surface). This is a
  concrete enterprise-sales bullet — put the scan output in the docs.

### 1.6 Build time & layer caching for a Rust core

INFERRED + KNOWN: the Rust compile is the slow part (`SYNTHESIS §4.1`:
`lto="fat"`, `codegen-units=1` — a *deliberately slow, maximally optimized*
release profile). This is **not in the user's Docker build** because the wheel
is prebuilt and published (`SYNTHESIS §4.7` abi3 wheels). The user's `docker
build` only does `uv sync` (wheel download + venv) → seconds, fully cacheable.
This is a structural advantage to advertise: *"Your `docker build` never
compiles Rust — you install a prebuilt wheel, same as installing FastAPI."*
The slow `lto=fat` compile happens once, in Lancelot's release CI (maturin
build matrix), not in every user's pipeline.


---

## 2. Kubernetes — the framework's job vs the platform's job

The split is the whole game. **Kubernetes owns:** scheduling, restart policy,
endpoint routing, the rolling-update strategy, autoscaling decisions, the
disruption budget. **The framework owns:** answering probes truthfully,
catching SIGTERM and draining, not lying about readiness, and not forking more
workers than the cgroup allows. Robyn fails three of those four (`robyn/03 §0`:
no graceful shutdown, no crashed-worker supervisor, `os.cpu_count()` ignores
cgroup quotas). Lancelot must get all four right *in the core*, because they
cannot be bolted on by the user.

### 2.1 Three probes, three endpoints — ship all three

KNOWN — Kubernetes defines three probe types with distinct semantics
(kubernetes.io/docs/concepts/configuration/liveness-readiness-startup-probes):

| Probe | Question it answers | Failure consequence | What it must check |
|---|---|---|---|
| **startup** | "Has slow init finished?" | container killed, restarted | only that boot completed (config loaded, Rust core bound, routes compiled) |
| **liveness** | "Is the process wedged?" | **container restarted** | **process-internal only** — event loop responsive, not deadlocked. **NEVER a dependency.** |
| **readiness** | "Should I get traffic?" | Pod IP removed from EndpointSlice (no restart) | self + **required** deps (DB, the LLM provider if hard-required) |

KNOWN — the canonical anti-pattern, verbatim from the K8s docs: a liveness
probe that checks a dependency *"causes restart loops when a dependency is
down… can lead to cascading failures… restarting of container under high
load."* Liveness checks the app **only**; readiness additionally checks
backends so traffic is withheld without a restart
(kubernetes.io/docs/concepts/configuration/liveness-readiness-startup-probes —
*"The liveness probe passes when the app itself is healthy, but the readiness
probe additionally checks that each required back-end service is available."*).

**RECOMMENDATION — Lancelot ships three built-in endpoints, opt-out, served
from the const-tier:**

- **`GET /livez`** → `200 {"status":"ok"}` if the process loop is turning.
  Implemented as a **const-tier route** (`SYNTHESIS §1 steal #5`, `robyn/02 §0`):
  pre-baked response, zero GIL, zero Python, zero alloc. **This is the single
  most important design point in this whole document:** if `/livez` went
  through the Python handler pool, a saturated app (all blocking-pool slots
  full) would fail liveness and K8s would *restart a working-but-busy pod*,
  amplifying an overload into an outage. The const-tier (Robyn's best idea) is
  the mechanism that makes liveness honest under load. It answers in Rust
  before any Python is touched.
- **`GET /readyz`** → runs a registered set of async readiness checks (DB ping,
  cache ping, "LLM provider key present"), `200` if all pass, `503` otherwise.
  This one *does* touch Python (it must call user-registered checks) but on a
  **separate, reserved concurrency lane** so a saturated request pool doesn't
  starve readiness. Default checks: none (returns 200); user registers via
  `@app.readiness_check`.
- **`GET /startupz`** → `200` only after lifespan/startup completed (Rust core
  bound, routes compiled, user `on_startup` ran). Lets K8s give slow-booting
  apps (model warmup, connection pool fill) a long `failureThreshold` without
  loosening the liveness probe (KNOWN — K8s docs: startup probe *"allows the
  application time to finish its initialization"* and blocks liveness/readiness
  until it passes).

Endpoint naming: `/livez` `/readyz` `/startupz` follows the Kubernetes core
convention (the `-z` suffix, as in `/healthz`/`/readyz` used by the API server
itself). Make the paths configurable but default to these.

Probe defaults to **document in the shipped manifest** (KNOWN — K8s defaults:
`periodSeconds` 10s, `timeoutSeconds` 1s, `failureThreshold` 3,
`successThreshold` 1; startup probe pattern = same endpoint as liveness with a
high `failureThreshold`):

```yaml
startupProbe:   { httpGet: {path: /startupz, port: 8000}, failureThreshold: 30, periodSeconds: 2 }   # 60s budget to boot
livenessProbe:  { httpGet: {path: /livez,   port: 8000}, periodSeconds: 10, failureThreshold: 3, timeoutSeconds: 2 }
readinessProbe: { httpGet: {path: /readyz,  port: 8000}, periodSeconds: 5,  failureThreshold: 2, timeoutSeconds: 3 }
```

INFERRED rationale: liveness `timeoutSeconds: 2` not the 1s default — a Rust
const-tier response is sub-ms, but K8s default 1s timeout has bitten many
services on a cold kubelet; 2s is the widely-recommended safety margin and
costs nothing for a const-tier route.

### 2.2 The gRPC probe option

KNOWN — K8s supports a native `grpc` probe action (returns on the standard gRPC
health-checking protocol `SERVING` status). If Lancelot's AI layer exposes gRPC
(the MCP layer is JSON-RPC over HTTP per `robyn/03 §0.7`, so HTTP probes are the
right default), HTTP probes remain correct. RECOMMENDATION: HTTP probes are the
default; document the gRPC probe only if/when a gRPC transport ships. Do not
over-build.

### 2.3 SIGTERM, preStop, and connection draining — the part Robyn gets fatally wrong

KNOWN — the exact pod-deletion sequence (kubernetes.io pod-lifecycle;
oneuptime.com/blog/2026-02-09-prestop-hooks-zero-connection-drop):

1. Pod marked for deletion in the API server.
2. **Two things happen concurrently and independently:**
   (a) the EndpointSlice controller removes the Pod IP from Service endpoints —
   this propagates *asynchronously* to kube-proxy / ingress / mesh (seconds of
   lag);
   (b) the kubelet begins termination: runs `preStop` (if defined), **then**
   sends `SIGTERM` to PID 1.
3. App drains in-flight requests.
4. At `terminationGracePeriodSeconds` (KNOWN default **30 s**) the kubelet
   sends **SIGKILL** regardless.

**The race (KNOWN):** because (a) is async, if the app reacts to SIGTERM
*instantly* by closing the listener, there is a multi-second window where the
load balancer still routes new connections to a now-closed socket → connection
refused / 5xx during every rolling deploy. The fix is **not** purely in the
framework — it is `preStop: sleep N` to hold termination until endpoint removal
propagates (KNOWN — *"add a sleep command in the pre-stop hook … 5-10 seconds
… for most cases"*; cloud LBs 15–30 s).

**This defines the framework/platform split precisely:**

- **Platform's job (the manifest Lancelot ships):** `preStop: exec sleep 5` (or
  15 behind a cloud LB), `terminationGracePeriodSeconds` set *greater than*
  preStop sleep + worst-case drain (KNOWN — *"terminationGracePeriodSeconds
  must be set to a value that is greater than the preStop hook wait time"*).
- **Framework's job (Lancelot core):** on `SIGTERM` —
  1. **immediately flip `/readyz` to 503** (so even if the preStop window is
     too short, the readiness controller pulls the pod from endpoints faster);
  2. **stop accepting new connections** (close the `SO_REUSEPORT` listener);
  3. **let in-flight requests finish**, bounded by a configurable
     `shutdown_timeout` (default ~25 s, deliberately < the manifest's 30 s
     grace so the framework exits cleanly *before* SIGKILL);
  4. **drain streaming/SSE responses** — for AI workloads this is critical: a
     long LLM stream must either complete or be cancelled cleanly, and the
     upstream LLM call must be aborted so we stop paying for tokens (cross-ref
     `SYNTHESIS §4.5`: "Client-disconnect → cancel → abort upstream LLM call");
  5. **close upstream pools** (the Blazeio-derived async client pool,
     `SYNTHESIS §4.6`/`blazeio/03 §0.2`) so connections aren't leaked;
  6. **exit 0.**

Robyn does *none* of this — `process.kill()` drops in-flight requests, there is
no graceful shutdown and no crashed-worker supervisor (KNOWN — `robyn/03 §0`,
echoed `SYNTHESIS §3` Robyn-AVOID and `SYNTHESIS §4.2`). FastAPI's own docs
hammer the exec-form-CMD point precisely so SIGTERM reaches the process and
lifespan shutdown runs (KNOWN — fastapi.tiangolo.com/deployment/docker). **For
Lancelot this is a Tier-S correctness requirement, not a feature:** graceful
SIGTERM draining must be in the Rust supervisor (`SYNTHESIS §4.2`: "Graceful
shutdown (drain in-flight) + crashed-worker respawn supervisor"), default-on,
no user code required.

RECOMMENDATION — also handle **`SIGQUIT` = fast drain** and **second `SIGTERM`
= immediate stop** (Gunicorn/uvicorn convention; gives an operator an escape
hatch). And a **`catch_unwind` at the FFI seam + respawn supervisor** so one
panicking worker doesn't take the pod down (`SYNTHESIS §4.1` already mandates
this; it is also an availability/ops requirement, not just a Rust-hygiene one).

### 2.4 The multiprocess model vs cgroup CPU limits — Robyn's `os.cpu_count()` bug

This is the highest-stakes ops interaction in the whole framework, and the
SYNTHESIS already flags it twice (`robyn/03 §0` AVOID: *"`os.cpu_count()`
ignores cgroup quotas"*; `SYNTHESIS §4.2`: *"cgroup-aware CPU count (not
`os.cpu_count()`)"*). Concretely:

KNOWN — `os.cpu_count()` returns the **host** core count, not the container's
cgroup CPU limit (bugs.python.org/issue36054, /issue80235;
runebook.dev/.../os.cpu_count; victoriametrics.com/blog/kubernetes-cpu-go-gomaxprocs).
A Python process inside a Pod with `resources.limits.cpu: "2"` on a 64-core
node sees `os.cpu_count() == 64`.

The failure cascade if Lancelot copies Robyn's model naively
(`SYNTHESIS §1 steal #3`: N processes, one GIL each, sharing a `SO_REUSEPORT`
socket):

- Lancelot forks **64 worker processes** for a 2-CPU limit.
- The CFS scheduler throttles the cgroup to 2 CPUs of wall time → 64 processes
  fighting for 2 CPUs of quota → massive context-switch thrash, CFS throttling
  stalls, p99 latency explosion, OOM risk (64× per-process Python heap +
  64× tokio runtime + 64× Rust arena).
- This is **exactly** the GOMAXPROCS-in-containers problem the Go ecosystem
  solved with `automaxprocs`; Python has no equivalent built in (KNOWN —
  victoriametrics.com/blog/kubernetes-cpu-go-gomaxprocs frames the identical
  cgroup-quota-vs-reported-cores trap).

**RECOMMENDATION — Lancelot's cgroup-aware worker count (do this in Rust, in
the supervisor, before fork):**

```
worker_count =
  explicit --workers / LANCELOT_WORKERS env   (12-factor: always wins)
  else floor(cgroup_quota)                     (cgroup v2: read /sys/fs/cgroup/cpu.max → "$MAX $PERIOD"; workers = MAX/PERIOD)
                                               (cgroup v1: cpu.cfs_quota_us / cpu.cfs_period_us)
  else os.cpu_count()                          (bare metal / no limit only)
  clamped to >= 1
```

KNOWN parsing detail that *will* bite if missed: cgroup v2 `cpu.max` is
`"$MAX $PERIOD"`; `$MAX == "max"` means **no limit** (fall through to host
count); a single value means period defaults — VictoriaMetrics shipped a bug
silently mis-parsing exactly this (KNOWN —
github.com/VictoriaMetrics/VictoriaMetrics/issues/8808). Lancelot must parse
all three forms. Also handle **fractional limits** (`limits.cpu: "1500m"` →
quota/period = 1.5): RECOMMENDATION `max(1, floor(quota))` workers, and let the
tokio worker-thread pool inside each process absorb the fractional remainder
(Rust I/O scales on threads; only Python scales on processes — `SYNTHESIS §4.2`).

**Read cgroups in Rust, not via Python `os`/`psutil`.** The Rust supervisor
already exists (`SYNTHESIS §4.1-4.2`); reading `/sys/fs/cgroup/cpu.max` is a
trivial file read and keeps the decision *before* the Python interpreter forks.
This also dodges the `psutil`-dependency suggestion (runebook) — no extra Python
dep, decision made in the layer that owns the fork.

Expose the resolved worker count in `/metrics` and log it at boot
(`workers=4 (cgroup-derived, cpu.max="200000 100000")`) so operators can see
the framework respected the limit — this single log line is a credibility
signal that Robyn never sends.

### 2.5 HPA, custom metrics, KEDA

KNOWN (kedify.io/resources/blog/keda-vs-hpa; datadoghq.com/blog/autoscaling-custom-metrics;
aws.amazon.com/blogs/mt/autoscaling-kubernetes-workloads-with-keda):

- **HPA on CPU** is the floor — works out of the box, but for a web/AI service
  CPU is a *lagging, noisy* signal (an LLM-proxying handler is mostly I/O-wait;
  CPU stays low while the request queue explodes). CPU-based HPA under-scales
  exactly the AI workload Lancelot targets.
- **HPA on custom metrics** (RPS, in-flight, queue depth) needs the Prometheus
  Adapter; **KEDA talks to Prometheus directly** and is the 2026 default for
  anything queue/RPS/event-driven, and **KEDA can scale to zero** (HPA cannot)
  — directly relevant to scale-to-zero PaaS economics (§3).
- KNOWN canonical pattern: *"target 200 RPS per pod with a Prometheus query
  that sums the rate of HTTP requests."* The Prometheus query is only possible
  if the framework **exports the right metric**.

**RECOMMENDATION — the framework's autoscaling responsibility is to emit the
metrics that make good scaling possible; the scaling decision is the
platform's.** Lancelot must export, by default, on `/metrics`:

- `lancelot_http_requests_total{method,route,status}` (RED — rate, errors).
- `lancelot_http_request_duration_seconds` histogram (RED — duration; p95/p99).
- `lancelot_inflight_requests` gauge — **the single best autoscaling signal for
  an AI proxy** (in-flight tracks concurrency/queue saturation, leads CPU by
  seconds for I/O-bound LLM handlers).
- `lancelot_blocking_pool_queue_depth` and `lancelot_blocking_pool_active` —
  surfaces the sync-handler pool saturation (the bounded, load-shedding pool
  from `SYNTHESIS §4.2`); a rising queue depth is the earliest scale signal and
  also the load-shed trigger.
- `lancelot_workers` gauge — the cgroup-derived worker count from §2.4.

Ship a **reference KEDA `ScaledObject`** (RPS + in-flight triggers) and an HPA
v2 manifest (CPU + the in-flight custom metric) in `lancelot init --k8s`. The
framework does not autoscale itself — it *makes itself autoscalable* by
emitting `lancelot_inflight_requests`. That gauge is the deliverable.

### 2.6 PodDisruptionBudget + rolling update for zero dropped requests

KNOWN (kubernetes.io/docs/tasks/run-application/configure-pdb;
oneuptime.com/blog/2026-01-06-kubernetes-pod-disruption-budgets;
statsig.com/blog/kubernetes-pdb-maxunavailable; dev.to/young_gao zero-downtime 2026):

- PDBs govern **voluntary** disruptions (node drain, autoscaler scale-down) —
  *not* the rolling update (that is `strategy.rollingUpdate`). Both must be set;
  they are different controls.
- For stateless web services: **`PDB maxUnavailable`** (scales with replica
  count) over `minAvailable` (KNOWN — official K8s recommendation; with <5
  replicas percentage `minAvailable` rounds up and can *lock out all
  disruptions*).
- Zero-downtime rolling update = three things together (KNOWN, verbatim
  framing): *"readiness probes that genuinely verify readiness, graceful
  shutdown that drains connections, and preStop hooks that account for Endpoint
  propagation delay."* Lancelot owns #1 and #2 (§2.1, §2.3); the shipped
  manifest owns #3.
- Rolling-update strategy: `maxSurge` > 0, small/zero `maxUnavailable` so new
  pods come up before old ones leave; `maxUnavailable: 0` is the safest for
  critical services (KNOWN).

**RECOMMENDATION — shipped reference Deployment** (the manifest `lancelot init
--k8s` scaffolds) wires *all* of it together:

```yaml
spec:
  replicas: 3
  strategy:
    type: RollingUpdate
    rollingUpdate: { maxSurge: 1, maxUnavailable: 0 }   # add-before-remove
  template:
    spec:
      terminationGracePeriodSeconds: 40                  # > preStop(5) + shutdown_timeout(25) + margin
      containers:
      - name: app
        image: myapp:1.0.0
        ports: [{containerPort: 8000}]
        startupProbe:   { httpGet: {path: /startupz, port: 8000}, failureThreshold: 30, periodSeconds: 2 }
        livenessProbe:  { httpGet: {path: /livez,    port: 8000}, periodSeconds: 10, failureThreshold: 3, timeoutSeconds: 2 }
        readinessProbe: { httpGet: {path: /readyz,   port: 8000}, periodSeconds: 5,  failureThreshold: 2, timeoutSeconds: 3 }
        lifecycle:
          preStop: { exec: { command: ["sleep","5"] } }  # endpoint-propagation airbag
        resources:
          requests: { cpu: "500m", memory: "256Mi" }
          limits:   { cpu: "2",    memory: "512Mi" }       # → §2.4: Lancelot forks floor(2)=2 workers, NOT host cores
        securityContext:
          runAsNonRoot: true
          runAsUser: 65532
          allowPrivilegeEscalation: false
          readOnlyRootFilesystem: true                     # Lancelot must not write to / at runtime
          capabilities: { drop: ["ALL"] }
---
apiVersion: policy/v1
kind: PodDisruptionBudget
spec: { maxUnavailable: 1, selector: { matchLabels: { app: myapp } } }
```

`readOnlyRootFilesystem: true` is a constraint on the framework: Lancelot must
**not** write temp files, sockets, or caches to the root FS at runtime (use
`/tmp` via an `emptyDir` if ever needed). The const-tier and in-memory caches
(`SYNTHESIS §1 steal #5`) are memory-resident — this is already aligned, but it
is now an explicit ops requirement to test against.


---

## 3. Serverless / PaaS — what 2026 startups actually pick

### 3.1 The 2026 PaaS landscape (KNOWN, sourced)

Startups in 2026 deploy on a small set of platforms. What matters for a
framework is **(a) does it need scale-to-zero, (b) what concurrency model does
the platform impose, (c) how much does cold start hurt, (d) does the platform
assume ASGI?**

| Platform | Scale-to-zero | Concurrency model | Cold start (KNOWN) | Container or adapter | Source |
|---|---|---|---|---|---|
| **Google Cloud Run** | yes (min-instances=0) or warm (min=1) | **N concurrent reqs / instance** (default 80, configurable) | **200–700 ms** | runs your container's HTTP server | medium.com/@bhagyarana80 serverless guide; render.com/articles/alternatives-to-fly-io |
| **Fly.io** | yes (`auto_stop_machines="stop"`) | per-Machine, full Docker | ~300 ms–2 s (app boot bound) | runs your container | render.com/articles/alternatives-to-fly-io; techsy.io |
| **Railway** | **no** scale-to-zero | always-on container | n/a (warm) | runs your container | devtoolpicks.com; thesoftwarescout.com |
| **Render** | **no** on paid (min=1); free tier spins down | always-on container | free tier **30–60 s**; paid warm | runs your container | render.com; expresstech.io |
| **AWS Lambda** | yes (inherent) | **1 request / instance** (no per-instance concurrency) | **500–2500 ms**; +100–200 ms for the Python web framework | event adapter OR Lambda Web Adapter | medium.com/@bhagyarana80; github.com/awslabs/aws-lambda-web-adapter |
| **Vercel (Python)** | yes | very limited; **10 s hard timeout**, 1024 MB | **~1200 ms** | asgi-adapter, not ASGI-native | medium.com/@bhagyarana80 |
| **Modal** | yes (warm pool) | function-per-container, warm pool | **GPU 2–4 s**, CPU sub-second (warm pool) | Python-native SDK (own runtime) | modal.com/docs/guide; introl.com serverless-gpu-comparison |

KNOWN takeaways that shape Lancelot:

- **The dominant model is "platform runs your container's HTTP server"** (Cloud
  Run, Fly, Railway, Render). For these, Lancelot needs **nothing special** — it
  is just a long-lived HTTP server in a container. Its job is to bind fast,
  pass health checks, and shut down on SIGTERM. All of §1–§2 directly applies.
  Cloud Run *"is the only option that supports [async] natively … because it
  runs a containerized HTTP server"* (KNOWN — medium.com/@bhagyarana80).
- **Cloud Run's concurrency model (default 80 concurrent requests per
  instance)** maps *perfectly* to Lancelot's design: a Rust core handling many
  concurrent I/O-bound LLM requests off-GIL is exactly the workload Cloud Run's
  per-instance concurrency rewards. A framework that serializes (Robyn's
  per-token GIL churn, `robyn/01 §10`) wastes the 80-slot concurrency; Lancelot's
  GIL-free await (`SYNTHESIS §1 steal #2`) exploits it. **This is a concrete
  selling point: "one Cloud Run instance, 80 concurrent LLM streams, GIL never
  held across the await."**
- **Lambda is the only one that needs an adapter** (1-request-per-instance,
  event-driven, not a long-lived socket). See §3.3.
- **Railway/Render-paid don't scale to zero**, so cold start is a non-issue
  there; **Cloud Run / Fly / Lambda / Modal do**, so cold start is a real
  competitive axis (§3.2).

### 3.2 Cold start — the Rust core is a quantifiable weapon

KNOWN numbers to anchor against:

- **FastAPI + uvicorn adds ~100–200 ms of framework cold-start** on top of the
  platform's container/runtime cold start, *regardless of adapter*
  (KNOWN — github.com/awslabs/aws-lambda-web-adapter/discussions/514:
  *"FastAPI with Uvicorn usually adds about 100~200ms cold-start time"*;
  corroborated medium.com/@bhagyarana80).
- That 100–200 ms is: import `uvicorn`, `starlette`, `pydantic`, `anyio`,
  `h11`/`httptools`, build the ASGI app, compile routes, start the event loop.
  It is **pure Python import + interpreter work**.
- Platform container cold start for these: Cloud Run 200–700 ms, Lambda
  500–2500 ms (KNOWN, §3.1). The framework's 100–200 ms is a **20–50%
  amplifier** on the fast (Cloud Run) path.

INFERRED — where Lancelot wins: the equivalent Lancelot work is *one
`import lancelot`* that loads a **single prebuilt `.so`** (the abi3 wheel,
`SYNTHESIS §4.7`). There is no `starlette`/`anyio`/`h11`/`uvicorn` Python import
tree; routing-trie construction, validator compilation, and OpenAPI build are
the registration-time compiler (`SYNTHESIS §1 steal #1`) running in/towards Rust
rather than as interpreted Python. The dominant cold-start cost of a
Python-import-heavy framework is **eliminated by construction**, not optimized.

**RECOMMENDATION — make this a measured, published number.** The teardown
mandate is replication; here the mandate is *proof*. Ship a benchmark in CI:
cold-start (process spawn → first 200 on `/livez`) for Lancelot vs
FastAPI+uvicorn, on Cloud Run and on Lambda-via-LWA. INFERRED target: Lancelot
framework-attributable cold start **< 50 ms** (sub-half of FastAPI's 100–200 ms;
a `.so` mmap + dlopen is single-digit-to-tens of ms, no Python import cascade).
This single graph sells Lancelot to every scale-to-zero / Cloud Run / Lambda
shop — it is the most legible performance claim the framework has, more legible
than RPS (which is contested and workload-dependent). **Cold start is the
headline serverless metric; the Rust core's structural advantage there is the
serverless go-to-market.**

Caveat to document honestly (INFERRED): the abi3 `.so` is *larger on disk* than
pure-Python FastAPI (compiled Rust + embedded deps). On Lambda, a larger
deployment artifact can add image-pull/init time. Net cold start is still
expected to win (import-execution dominates artifact size for these sizes), but
the benchmark must measure end-to-end, not just `import` time, to be honest.

### 3.3 The Lambda question — adapter, not rewrite

KNOWN — there are two ways to run a Python web app on Lambda:

1. **Mangum-style ASGI adapter**: `handler = Mangum(app)` — translates the
   Lambda event into an ASGI `scope`/`receive`/`send`. **Requires the framework
   to expose an ASGI app, and requires user code changes** (KNOWN —
   github.com/awslabs/aws-lambda-web-adapter/discussions/514:
   *"Mangum … requires code changes … you must add `handler = Mangum(app)`"*).
2. **AWS Lambda Web Adapter (LWA)**: a Lambda extension layer that **runs your
   real HTTP server inside the Lambda sandbox**, listens for the Lambda event,
   and proxies it as a normal HTTP request to `127.0.0.1:PORT`. **It works with
   *any* HTTP server — it does not require ASGI** and *"does not require major
   refactoring of the existing code base"* (KNOWN — same discussion + LWA repo;
   johal.in/aws-lambda-webadapter-2026). Cold-start parity with Mangum within
   100 ms; LWA faster at p99 (KNOWN — discussion #514;
   *"reduces integration code by 90%"*).

**This is decisive for Lancelot's design.** Mangum requires an ASGI surface;
LWA requires only that Lancelot is a normal HTTP server that binds a port and
answers a readiness check. Lancelot **is** a normal HTTP server (Rust core
binding a socket — `SYNTHESIS §4.2`). Therefore:

**RECOMMENDATION — Lambda support = LWA, zero framework code.** Lancelot does
*not* need a Mangum-equivalent or a bespoke Lambda handler. It needs:
(a) to bind a configurable port (already true), (b) a fast `/livez` for LWA's
`READINESS_CHECK_PATH` (the const-tier endpoint from §2.1 is exactly this),
(c) to honor `AWS_LWA_PORT`/`PORT` env (12-factor, §5). Ship a reference
Lambda Dockerfile that adds the LWA layer:
`COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:0.9.1 /lambda-adapter
/opt/extensions/lambda-adapter` and set `AWS_LWA_READINESS_CHECK_PATH=/livez`.
**The const-tier `/livez` endpoint from §2.1 does double duty as the LWA
readiness probe — one mechanism, three platforms (K8s liveness, Docker
HEALTHCHECK, Lambda LWA).** That is the design economy to advertise.

A Mangum-style native ASGI-event handler is a *possible later add* for users
who want API-Gateway-event-shaped Lambda without the LWA layer, but it is **not
required for Lambda adoption** and should not gate v1. The ASGI surface it would
need is the same one §6 recommends for migration anyway.

### 3.4 Modal / GPU-serverless

KNOWN — Modal is Python-native with its own runtime/SDK (no generic container
HTTP-server contract; modal.com/docs/guide; introl.com). A Lancelot app can run
*inside* a Modal function (Modal can serve an ASGI/WSGI app via
`@modal.asgi_app()` / `@modal.web_server()`), but Modal is primarily a GPU
*inference/training* substrate, not a web-framework host. RECOMMENDATION:
support via Modal's `@modal.web_server(port)` decorator path (Modal runs an
arbitrary HTTP server — same story as Cloud Run/Fly: Lancelot just binds a
port). The `@modal.asgi_app()` path is the ASGI-shim's job (§6). Low priority;
document it, don't special-case it.

### 3.5 The serverless verdict for Lancelot's design

INFERRED synthesis: **80% of 2026 startup serverless/PaaS deployment is
"platform runs your container's long-lived HTTP server"** (Cloud Run, Fly,
Railway, Render, Modal-web-server). For all of those Lancelot needs *nothing
serverless-specific* — fast bind, const-tier health endpoint, SIGTERM drain
(all already required by §1–§2). The only true outlier is **Lambda**, and the
2026 answer there is **LWA (no code change), not a Mangum rewrite**. Net: the
serverless requirement on the framework is **(1) sub-50 ms cold start (Rust
core delivers this structurally — measure & publish), (2) bind-a-port +
const-tier `/livez`, (3) honor `$PORT`.** No serverless adapter belongs in the
core. The ASGI shim (§6) covers the residual `@modal.asgi_app()` / Mangum
nostalgia cases as a *compatibility* feature, not a serverless feature.


---

## 4. Observability — table stakes for adoption (and a wedge for an AI framework)

A 2026 web framework with no built-in OpenTelemetry / Prometheus story is
*disqualified* before evaluation by any team that runs in production. FastAPI
itself has no built-in observability — it relies on a *zoo* of third-party
packages (`opentelemetry-instrumentation-fastapi`,
`prometheus-fastapi-instrumentator`, Granian's built-in metrics) that the user
must assemble (KNOWN — last9.io, signoz.io, github.com/trallnag/prometheus-fastapi-instrumentator,
github.com/blueswen/fastapi-observability). **That fragmentation is Lancelot's
opening:** ship the three pillars in the core, correct by default, and ship the
**GenAI semantic conventions** that no general-purpose framework ships — because
Lancelot is an *AI* framework, the LLM span is a first-class citizen, not an
add-on.

### 4.1 What FastAPI / Litestar / Granian actually do (KNOWN baseline)

- **FastAPI**: nothing built in. `FastAPIInstrumentor().instrument_app(app)`
  from `opentelemetry-instrumentation-fastapi` does auto-tracing by hooking the
  ASGI layer; `prometheus-fastapi-instrumentator` adds metrics; logging
  correlation is manual. Three packages, user-assembled (KNOWN — signoz.io,
  last9.io).
- **`prometheus-fastapi-instrumentator`** default metrics (KNOWN, verbatim —
  github.com/trallnag/prometheus-fastapi-instrumentator):
  `http_requests_total` (Counter, labels `handler,status,method`),
  `http_request_size_bytes` (Summary), `http_response_size_bytes` (Summary),
  `http_request_duration_seconds` (Histogram, label `handler`),
  `http_request_duration_highr_seconds` (Histogram). Status codes grouped
  `2xx/3xx/...`; unmatched routes → handler `none` (cardinality control).
- **Granian** (the Rust-cored ASGI/RSGI server — the closest existing thing to
  Lancelot's shape): ships **built-in metrics collection**, OTel semantic
  metrics exported via **OTLP** (KNOWN — search corpus; this is the model
  Lancelot should match and exceed: a Rust-cored server emitting OTel natively).
- **OTel Python OTLP exporter**: default transport is **gRPC**; `http/protobuf`
  is opt-in via `--exporter_otlp_protocol http/protobuf` or
  `OTEL_EXPORTER_OTLP_PROTOCOL` (KNOWN — opentelemetry.io/docs/languages/python/exporters).
  Config is fully env-var driven: `OTEL_SERVICE_NAME`, `OTEL_TRACES_EXPORTER`,
  `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`, `OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED`
  (KNOWN — opentelemetry.io/docs/zero-code/python/configuration).

### 4.2 The three pillars — what Lancelot ships by default

**RECOMMENDATION — built-in, opt-*out*, env-configured:**

**(a) Metrics — Prometheus `/metrics` + OTLP, on by default.** Emit the RED set
with names aligned to the de-facto FastAPI ecosystem (so existing Grafana
dashboards work unchanged) but with a `lancelot_`-free *option* (default to the
ecosystem-standard `http_*` names for drop-in dashboard compatibility; allow a
namespace prefix):

- `http_requests_total{method,handler,status}` — RED rate+errors. Status
  grouped `2xx/3xx/4xx/5xx` *and* exact (label both? no — exact status, let
  Prometheus recording-rule the grouping; cardinality is bounded by ~60 codes).
- `http_request_duration_seconds` histogram `{method,handler}` — RED duration;
  buckets tuned for web+AI: `[.005,.01,.025,.05,.1,.25,.5,1,2.5,5,10,30,60]`
  (LLM responses routinely exceed 10 s — FastAPI's default buckets top out too
  low for streaming AI; this is a Lancelot-specific correction).
- `http_request_size_bytes` / `http_response_size_bytes` summaries.
- `lancelot_inflight_requests` gauge — the autoscaling signal (§2.5).
- `lancelot_blocking_pool_{active,queue_depth,rejected_total}` — surfaces the
  bounded sync-handler pool (`SYNTHESIS §4.2`); `rejected_total` is the
  load-shed counter (503 `Retry-After`, `SYNTHESIS §3` FastAPI-AVOID fix).
- `lancelot_workers` gauge — cgroup-derived worker count (§2.4); proves the
  framework respected the CPU limit.
- **Route templating is mandatory for cardinality**: label `handler` must be
  the *route template* (`/users/{id}`) never the raw path (`/users/42`);
  unmatched → `handler="none"` (KNOWN — exact pattern from
  prometheus-fastapi-instrumentator; a framework that labels by raw path melts
  Prometheus). Lancelot has the route template at registration
  (`SYNTHESIS §1 steal #1`, `robyn/02 §0` radix trie) — emit it directly, no
  regex reverse-engineering needed.
- `/metrics` is a **const-tier-adjacent route** (cheap, no GIL for the scrape
  path beyond reading atomic counters) so a Prometheus scrape never competes
  with request handling.

**(b) Traces — OTLP exporter, W3C `traceparent` propagation, on by default.**

- Every request = a `SERVER` span; auto-extract/inject W3C `traceparent` +
  `tracestate` (and `baggage`). Span name = route template, not raw path.
- Standard HTTP semconv attributes: `http.request.method`,
  `http.route`, `http.response.status_code`, `server.address`, `url.path`,
  `network.protocol.version`.
- Exporter: OTLP. **Default `http/protobuf`** not gRPC — INFERRED
  recommendation against the OTel-Python default of gRPC: `http/protobuf` has
  fewer dependency/proxy headaches in PaaS/K8s ingress and is the more robust
  default for the startup audience (KNOWN that gRPC is OTel's default and
  http/protobuf is the documented alternative — opentelemetry.io exporters).
  Env-overridable per spec (`OTEL_EXPORTER_OTLP_PROTOCOL`).
- The span must be created **in the Rust core** at request entry and the
  context made available to the Python handler — so the trace covers the full
  Rust parse → Python handler → Rust serialize path, not just the Python slice
  (this is *better* than FastAPI's ASGI-hook instrumentation which can't see
  the pre-ASGI parse time). INFERRED competitive point: Lancelot can trace the
  zero-GIL Rust segment FastAPI's instrumentation is structurally blind to.

**(c) Logs — structured JSON to stdout, trace-correlated, on by default.**

- Default formatter = JSON, one object per line, to **stdout** (12-factor — §5;
  the platform ships logs, the app does not manage files).
- Every log line auto-injects `trace_id`, `span_id`, `request_id` from the
  active context (KNOWN — `OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED`
  does exactly this; Lancelot does it without an env var, by default).
- `request_id`: accept inbound `X-Request-ID` else generate (ULID); echo it in
  the response header and put it on every log line and the root span. This is
  the single most-requested production-debug feature and is trivial — ship it
  default-on (INFERRED, universal practice).

### 4.3 The GenAI semantic conventions — Lancelot's observability differentiator

KNOWN — OpenTelemetry has formal **GenAI semantic conventions** (still
*experimental* as of 2026-03 but vendor-supported: Datadog ≥1.37 ingests them
natively; opentelemetry.io/docs/specs/semconv/gen-ai/;
datadoghq.com/blog/llm-otel-semantic-convention). The exact span attributes
(verbatim from opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-spans):

- `gen_ai.operation.name` (e.g. `chat`, `embeddings`)
- `gen_ai.provider.name` (e.g. `openai`, `anthropic`)
- `gen_ai.request.model` / `gen_ai.response.model`
- `gen_ai.usage.input_tokens` / `gen_ai.usage.output_tokens`
- `gen_ai.request.temperature` / `gen_ai.request.max_tokens` /
  `gen_ai.request.frequency_penalty` / `gen_ai.request.presence_penalty`
- `gen_ai.response.finish_reasons`
- **Span name MUST be `{gen_ai.operation.name} {gen_ai.request.model}`**
  (e.g. `chat gpt-4o`); **span kind `CLIENT`** for the provider call.
- Token-usage metrics emitted when usage is in the (possibly streaming)
  response (KNOWN — opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-metrics).

**RECOMMENDATION — this is where an AI framework earns its name.** Lancelot's
built-in async LLM client (the Blazeio-pool-derived client, `SYNTHESIS §4.6`,
`blazeio/03 §0.2`) must, by default, wrap every outbound LLM call in a
GenAI-semantic-convention `CLIENT` span:

- span name `{operation} {model}`, kind `CLIENT`;
- set `gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.response.model`,
  `gen_ai.request.{temperature,max_tokens}`;
- on response (including the *final* SSE chunk for streaming — OpenAI returns
  `usage` in the terminal chunk when `stream_options.include_usage`), set
  `gen_ai.usage.input_tokens` / `gen_ai.usage.output_tokens` and
  `gen_ai.response.finish_reasons`;
- emit the token-count metrics so cost dashboards work out of the box.

No general-purpose framework (FastAPI, Litestar, Robyn) ships this — it is
*always* a userland concern there (LangSmith/Traceloop/manual). Lancelot
shipping it natively, correctly named, behind the *same* client every Lancelot
AI app already uses (`SYNTHESIS §4.6`), means **every Lancelot AI app gets
per-call token/cost/latency observability for free**. This is the single
strongest *observability* argument for the framework's existence and must be in
the design from day one. Respect `OTEL_SEMCONV_STABILITY_OPT_IN` for the
experimental→stable attribute transition (KNOWN — dual-emission env var, OTel
2026 transition mechanism).

Streaming nuance (INFERRED, from `SYNTHESIS §4.5` SSE architecture): the GenAI
span must **stay open across the entire stream** and close on the terminal
chunk — the keepalive timer wraps the channel receive, *not* the span, exactly
as the PEP-789 fix wraps the channel not the generator (`fastapi/03 §0.7-0.9`).
Time-to-first-token and total-stream-duration should both be span attributes
(custom `gen_ai.server.time_to_first_token` — the latency metric AI users
actually care about; INFERRED extension, semconv has a TTFT metric in the
metrics spec).

### 4.4 What's default-on vs opt-in (the policy)

| Signal | Default | Rationale |
|---|---|---|
| `/metrics` Prometheus | **on** | zero cost when unscraped; K8s/Grafana expect it |
| Structured JSON logs + request_id | **on** | 12-factor; trace correlation is useless without it |
| OTLP traces export | **on if `OTEL_EXPORTER_OTLP_ENDPOINT` set, else no-op** | the OTel-standard "configured by env or silent" contract; zero overhead with no collector |
| GenAI LLM spans | **on** (when the built-in client is used) | the differentiator; opt-out via config for privacy/PII |
| Capturing prompt/response *content* on spans | **off** | PII/cost; gated behind explicit `capture_content=True` (matches OTel `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` opt-in default-off) |
| Per-request body logging | **off** | PII + volume |

INFERRED principle: **observability that is free when unused is on; anything
with cost, PII, or cardinality risk is off and one flag away.** This is the
exact line FastAPI's ecosystem fails to draw (the user must wire *everything*),
and Lancelot's chance to be correct-by-default without being a data-leak risk.


---

## 5. Config & 12-factor — fail at startup, not at 3am

KNOWN — the 2026 Python-config consensus (medium.com/@hadiyolworld007
"Twelve-Factor Secrets and Config Without Footguns"; docs.pydantic.dev
settings; medium.com/datamindedbe twelve-factor; blog.greeden.me 2025-11):

- **Config in the environment, not in code** (12-factor III). Precedence:
  process env var > `.env` file (dev only) > secret-manager mount > default.
- **`pydantic-settings` v2 `BaseSettings`** is the de-facto pattern: typed
  settings class, env vars highest priority, validation at construction —
  *"missing required fields raise errors during settings creation … fix
  deployment config, not debug runtime behavior."* This **fail-fast-at-startup**
  property is the whole point: a bad config kills the pod at boot (startup
  probe never passes, rollout halts) instead of 500ing under traffic.
- Secrets in prod come from **external sources**: env vars or **file mounts**
  (K8s Secrets mounted as files, Vault/ASM/GSM Agent-injected files). The
  `*_FILE` convention (read secret from a file path in an env var) is the
  container-native pattern for not putting secrets in the process environment
  (where they leak via `/proc/<pid>/environ` and crash dumps).
- `.env` in `.gitignore`; ship `.env.example` with placeholders.

**RECOMMENDATION — Lancelot's config contract:**

- **Lancelot must boot from environment alone, zero config files required.**
  A startup that `docker run`s the image with env vars set must get a working
  server. No mandatory `lancelot.toml`. (Config *file* support is fine as an
  optional convenience; it must never be *required* — that breaks 12-factor and
  every PaaS.)
- Lancelot owns its *own* operational config via env, all prefixed `LANCELOT_`:
  `LANCELOT_HOST`, `LANCELOT_PORT` (also honor bare `PORT` — Cloud Run/Heroku/
  Render/Fly **inject `$PORT`** and the app *must* bind it; this is a hard PaaS
  requirement, KNOWN — every PaaS in §3), `LANCELOT_WORKERS` (overrides the
  cgroup-derived count, §2.4), `LANCELOT_SHUTDOWN_TIMEOUT`, `LANCELOT_LOG_LEVEL`,
  `LANCELOT_LOG_FORMAT` (`json`|`text`, default `json`), plus the standard
  `OTEL_*` set (§4) honored as-is (don't reinvent OTel's env contract).
- **Validate operational config at boot, in Rust, before fork** — an invalid
  `LANCELOT_PORT` or un-bindable address must exit non-zero *immediately* with
  a clear message, **not** Robyn's `input()`-on-port-conflict that hangs a
  container forever waiting for stdin (KNOWN — `robyn/03 §0` AVOID: *"`input()`
  on port conflict hangs containers"*; this is a literal container-deadlock
  bug Lancelot must not reproduce — a non-TTY container has no stdin, so the
  prompt blocks until the orchestrator SIGKILLs it).
- **User app settings**: Lancelot should *integrate with* `pydantic-settings`
  (the ecosystem standard, and pydantic-core is already in the stack —
  `SYNTHESIS §0`/`fastapi/03 §0.1`) rather than ship a competing settings
  system. Provide a documented `Settings` base and a DI provider so
  `def handler(settings: Settings = Depends(get_settings))` works with the
  two-phase DI compiler (`SYNTHESIS §1 steal #1`, `fastapi/02`). Settings
  resolved **once at registration** (cached singleton), not per request — same
  once/per-request split as everything else.
- **Healthcheck design ties back to config (§2.1)**: `/readyz` should reflect
  *graceful degradation* — if an optional dependency (e.g. a cache) is down,
  the app is *ready* (serves, degraded) not *unready*; only a *required*
  dependency failing flips `/readyz` to 503. The config declares which deps are
  required vs optional; readiness reads that. This is the difference between
  "cache blip → whole service drained out of the LB" (wrong) and "cache blip →
  serve from origin, slower" (right). FastAPI gives you nothing here; Lancelot
  should ship the required/optional dependency-health model as a first-class
  concept.
- **Hot reload: dev-only, in-process, never the prod default.** Robyn's
  watchdog+subprocess-respawn mechanism is the right idea but CLI-locked
  (KNOWN — `robyn/03 §0.4`, `SYNTHESIS` Robyn-AVOID: *"CLI-only `--dev`"*).
  RECOMMENDATION: reload is a `dev=True` / `LANCELOT_DEV=1` flag, in-process,
  **hard-disabled when not a TTY or when `LANCELOT_ENV=production`** — so a
  reloader can never accidentally ship to prod (a class of outage: file-watch
  fd exhaustion and double-process memory in prod). Multi-stage env via a
  single `LANCELOT_ENV` (`development`/`staging`/`production`) that flips safe
  defaults (reload, verbose errors, doc exposure) — but every individual knob
  stays independently overridable (12-factor: env is the source of truth, not a
  magic profile).

INFERRED principle: **the framework's config job is to make misconfiguration a
loud boot-time crash, never a silent runtime degradation.** Robyn's two config
bugs (`input()` hang, `os.cpu_count()`) are both *silent-at-boot, catastrophic-
under-load* — exactly the failure mode 12-factor's fail-fast settings exist to
prevent. Lancelot validating all operational config in the Rust supervisor
before the Python fork is the structural fix.

---

## 6. Migration / adoption path — the ASGI-compat decision

This is the highest-leverage *adoption* decision in the document. A faster
framework nobody can migrate to is dead. The question: **does Lancelot expose
an ASGI surface (run existing Starlette/FastAPI middleware, be a drop-in ASGI
app), use a native Rust ABI, or both?**

### 6.1 The evidence: ASGI is the ecosystem's lingua franca; RSGI is the precedent

KNOWN (asgi.readthedocs.io; starlette.dev/middleware;
deployhq.com/blog/python-application-servers-in-2025; github.com/emmett-framework/granian RSGI spec;
github.com/Kludex/starlette/discussions/2287):

- **ASGI is the universal contract.** Starlette middleware *is* a chain of ASGI
  apps; FastAPI, Starlette, Litestar, async-Django all *assume ASGI*. The
  entire middleware/auth/session/CORS/observability ecosystem
  (`opentelemetry-instrumentation-asgi`, `starlette-context`, sentry's ASGI
  middleware, every `BaseHTTPMiddleware` in the wild) speaks ASGI. *"ASGI
  remains the safer default for production systems requiring broad
  compatibility."*
- **Granian is the exact precedent for Lancelot's situation.** It is a
  Rust-cored Python server that found the same fork-in-the-road. Its answer:
  ship **both** — ASGI for compatibility, **RSGI** (its own Rust-native
  protocol) for the speed path. RSGI exists *"to solve the inefficiencies …
  for servers written in Rust, where I/O and threading are handled outside the
  Python interpreter"* — i.e. the precise ASGI tax Lancelot's Rust core also
  pays (ASGI forces every byte through Python `receive`/`send` dict messages —
  the serialization tax `SYNTHESIS §1` calls out for the streaming path).
- **The dual-protocol mechanism is trivial and proven**: RSGI apps expose
  `async def __rsgi__(self, scope, protocol)` *alongside* `async def __call__(
  self, scope, receive, send)`; the server *"prefers `__rsgi__` over `__call__`
  when present."* One app object, two protocol entry points, transparent
  negotiation, **zero duplicated app logic** (KNOWN — verbatim RSGI spec). This
  is the existence proof that "native ABI for speed + ASGI for compat" is not a
  research project — it ships today in Granian.
- ASGI's cost is real and exactly what Lancelot is built to avoid: ASGI
  *"expects the lower protocol to be handled by Python."* Lancelot's whole
  thesis (`SYNTHESIS §1`) is the lower protocol handled in **Rust, off-GIL**.
  Routing every request through the ASGI `receive`/`send` Python message pump
  *re-imposes the exact per-request Python tax* the Rust core eliminates
  (`SYNTHESIS §0` FastAPI fatal weakness: "≥10 framework allocs/req before user
  code"; ASGI message dicts are more of the same). **ASGI cannot be the hot
  path** — it can only be the compatibility path.

### 6.2 RECOMMENDATION — dual-tier, ASGI as the compatibility tier (decision)

**Lancelot ships a native Rust request ABI as the hot path, and an ASGI
compatibility surface as the migration/ecosystem bridge. ASGI is never the fast
path; it is the on-ramp.** Concretely, three interop seams:

1. **Native ABI (the hot path, default).** Lancelot handlers, the const-tier,
   the registration-time DI/validation compiler, native streaming — none of
   this touches ASGI. The Python↔Rust contract is Robyn's `FunctionInfo`
   opaque-callable shape (`SYNTHESIS §1 steal #10`, `robyn/03 §0.2`): all
   reflection at registration, Rust never introspects on the hot path. This is
   where the performance thesis lives. **Lancelot is RSGI-class, not ASGI, at
   the core.** (It can additionally expose an `__rsgi__`-style entrypoint so a
   Granian/RSGI-aware deployer can run it — cheap, follows the proven pattern.)

2. **Mount an ASGI app inside Lancelot (incremental migration IN).** Ship
   `app.mount_asgi("/legacy", fastapi_app)` — a sub-tree where requests are
   adapted to ASGI `scope/receive/send` and the user's existing FastAPI/
   Starlette/Django-async app runs unchanged. This is the **migration
   on-ramp**: a startup with a 200-route FastAPI app adopts Lancelot by
   mounting the whole thing under Lancelot's server, then moves routes onto
   native Lancelot handlers **one at a time**, measuring the speedup per route,
   with zero big-bang rewrite. The mounted subtree pays the ASGI tax (fine — it
   is the legacy tier by definition); native routes don't. This single feature
   converts "rewrite to adopt" into "point your server at Lancelot, migrate at
   leisure" — the difference between an unadoptable framework and an adoptable
   one.

3. **Be mountable as / expose an ASGI app (incremental migration OUT / tooling
   compat).** Lancelot exposes `app.asgi()` returning a spec-compliant ASGI 3.0
   callable. Why this matters even though it's the slow path:
   (a) it de-risks adoption — "you're not locked in, Lancelot *is* an ASGI app,
   run it under uvicorn/Granian/Hypercorn if you ever want to" kills the
   lock-in objection that sinks new frameworks;
   (b) the entire ASGI middleware/tooling ecosystem
   (`opentelemetry-instrumentation-asgi`, Sentry, auth middlewares) can wrap it
   during a transition;
   (c) it is exactly how the Mangum/`@modal.asgi_app()` residual serverless
   cases (§3.3-3.4) get covered for free — no separate adapter.
   The ASGI surface should run **native middleware in Rust** and only fall to
   the ASGI message pump at the app boundary — i.e. ASGI-compat does **not**
   mean adopting `BaseHTTPMiddleware`'s per-request task-group+2-stream+cached-
   request disaster (`SYNTHESIS §3` FastAPI-AVOID, `fastapi/01 §0.5`). Middleware
   is pure-Rust/native (`SYNTHESIS §3`: "pure-Rust/ASGI middleware only, never a
   `call_next` dispatch model"); ASGI is only the *app-callable* boundary.

### 6.3 The migration narrative Lancelot must be able to tell (this sells it)

INFERRED — the concrete adoption story, end to end, that the above enables:

1. `pip install lancelot` (one prebuilt abi3 wheel, §1 — no Rust toolchain).
2. `app = lancelot.App(); app.mount_asgi("/", my_fastapi_app)` — **the entire
   existing FastAPI app runs under Lancelot's Rust server on day one, zero code
   changes.** Already a win: better cold start (§3.2), graceful shutdown (§2.3),
   built-in probes/metrics/OTel (§2,§4), cgroup-correct workers (§2.4) — all
   *without rewriting a single route*. The ops surface alone justifies the
   switch before any handler is ported.
3. Port the hot routes to native Lancelot handlers incrementally; each ported
   route drops the ASGI tax and gets the registration-time compiler +
   const-tier + native streaming. Measure per-route; keep the long tail on the
   mounted ASGI app forever if it's not worth porting.
4. Delete `mount_asgi` when the last legacy route is ported (or never — it's a
   valid permanent state).

This is the **Strangler Fig** pattern, and it is the *only* migration model
that works for a new framework competing with an incumbent that has the
ecosystem. FastAPI itself won partly because it was "just Starlette + Pydantic,
incrementally adoptable." Lancelot's equivalent must be "just point your ASGI
app at it, then migrate routes." **The ASGI-compat surface is not a performance
feature — it is the entire go-to-market.** Skipping it makes Lancelot a
benchmark toy nobody can deploy; shipping it makes Lancelot adoptable by every
FastAPI shop without a rewrite budget.

### 6.4 What ASGI-compat explicitly does NOT mean

- It does **not** mean ASGI is the internal architecture (it is the boundary
  adapter only; the core is the native `FunctionInfo` ABI).
- It does **not** mean adopting Starlette's `BaseHTTPMiddleware` model
  (`SYNTHESIS §3` AVOID) — middleware is native/Rust; ASGI is app-callable
  boundary only.
- It does **not** mean ASGI-level performance on native routes — native routes
  bypass ASGI entirely; only mounted/legacy subtrees pay the tax, by design.
- It does **not** gate v1 on full ASGI spec coverage of every edge (lifespan,
  HTTP/2 server-push semantics) — ship HTTP + WebSocket scope support
  (covers FastAPI/Starlette/Litestar real-world usage); document the gaps
  honestly.


---

## 7. The concrete requirements list — the exact ops surface Lancelot must ship

Every item is a build target. Grouped by subsystem; each tagged with its
driving evidence. **R = hard requirement (v1), S = should (v1 if cheap), L =
later.**

### 7.1 Probe / health endpoints (the const-tier's first customer)

- **[R]** `GET /livez` — const-tier route (zero-GIL, zero-Python, zero-alloc;
  `SYNTHESIS §1 steal #5`). `200` iff process loop alive. **Must not** touch
  the Python handler pool or the GIL. *Driver: K8s liveness anti-pattern
  (kubernetes.io probes); a saturated pool must still pass liveness.*
- **[R]** `GET /readyz` — runs registered readiness checks on a **reserved
  concurrency lane**; `200`/`503`. Required-vs-optional dependency model
  (§5): optional dep down → still ready (degraded). Flips to `503` on SIGTERM
  *before* draining.
- **[R]** `GET /startupz` — `200` only after lifespan/startup complete. Lets
  K8s startup probe gate liveness/readiness for slow boots.
- **[R]** Paths configurable, defaults `/livez` `/readyz` `/startupz` (K8s `-z`
  convention). The `/livez` endpoint also serves as the Docker `HEALTHCHECK`
  target and the AWS LWA `READINESS_CHECK_PATH` — **one mechanism, three
  platforms** (§3.3).
- **[S]** Native gRPC health probe only if/when a gRPC transport ships (§2.2) —
  do not pre-build.

### 7.2 Signal handling & lifecycle (the Robyn-fatal gap)

- **[R]** `SIGTERM` → graceful drain, default-on, **in the Rust supervisor**,
  zero user code: flip `/readyz`→503 → stop accepting → drain in-flight (bound
  `LANCELOT_SHUTDOWN_TIMEOUT`, default ~25 s) → abort upstream LLM calls on
  client-disconnect (`SYNTHESIS §4.5`) → close async-client pools
  (`SYNTHESIS §4.6`) → exit 0. *Driver: `robyn/03 §0` (Robyn `process.kill()`
  drops in-flight — fatal); fastapi.tiangolo.com exec-form CMD guidance.*
- **[R]** `SIGKILL`-safety: `shutdown_timeout` default **strictly less than**
  the shipped manifest's `terminationGracePeriodSeconds` (25 < 40) so the app
  exits clean before K8s force-kills.
- **[S]** `SIGQUIT` = fast drain; second `SIGTERM` = immediate stop (operator
  escape hatch, Gunicorn/uvicorn convention).
- **[R]** `catch_unwind` at the FFI seam + crashed-worker respawn supervisor
  (`SYNTHESIS §4.1`) — one panicking worker must not drop the pod.
- **[R]** Exec-form entrypoint in all shipped Dockerfiles so SIGTERM reaches
  PID 1.

### 7.3 Process / resource model (the `os.cpu_count()` bug)

- **[R]** cgroup-aware worker count in the Rust supervisor *before* fork:
  `LANCELOT_WORKERS` env > cgroup v2 `cpu.max` (parse `"$MAX $PERIOD"`, handle
  `max`=unlimited & single-value forms) > cgroup v1 `cfs_quota_us/cfs_period_us`
  > `os.cpu_count()`; clamp `≥1`; fractional → `max(1, floor(quota))`. *Driver:
  `robyn/03 §0`, `SYNTHESIS §4.2`, bugs.python.org/issue36054,
  github.com/VictoriaMetrics/VictoriaMetrics/issues/8808.*
- **[R]** Log resolved worker count + source at boot
  (`workers=2 (cgroup cpu.max="200000 100000")`); expose `lancelot_workers`
  gauge.
- **[R]** Never block on `input()` / stdin on port conflict — fail fast,
  non-zero exit, clear message (`robyn/03 §0` AVOID — container deadlock bug).
- **[R]** Bounded, load-shedding sync-handler pool wired into the HTTP path
  (`SYNTHESIS §4.2`, fixes `robyn/02 sync-bug` + `fastapi/01 §0.3` 40-thread
  cliff); overflow → `503 Retry-After`, counted in `lancelot_blocking_pool_rejected_total`.
- **[R]** Honor `readOnlyRootFilesystem: true` — no runtime writes to `/`.

### 7.4 Observability (table stakes + the GenAI wedge)

- **[R]** `GET /metrics` Prometheus, default-on, cheap scrape path. RED metrics
  with **route-template** labels (never raw path), unmatched→`handler="none"`.
  Histogram buckets extended to 30/60 s (AI streaming). *Driver:
  github.com/trallnag/prometheus-fastapi-instrumentator default-metric shape.*
- **[R]** `lancelot_inflight_requests` gauge (the autoscaling signal, §2.5),
  `lancelot_blocking_pool_{active,queue_depth,rejected_total}`,
  `lancelot_workers`.
- **[R]** OTLP trace export, default `http/protobuf`, W3C `traceparent`
  propagation, honor all `OTEL_*` env vars; **no-op when no endpoint
  configured** (zero overhead). Span created in Rust at request entry (covers
  the zero-GIL parse segment FastAPI's ASGI instrumentation can't see).
- **[R]** Structured **JSON logs to stdout**, default-on, auto-inject
  `trace_id`/`span_id`/`request_id`. Accept/echo `X-Request-ID` (else ULID).
- **[R, the differentiator]** GenAI semantic-convention `CLIENT` spans on the
  built-in LLM client (`SYNTHESIS §4.6`): span name `{operation} {model}`,
  attrs `gen_ai.provider.name`, `gen_ai.request.model`,
  `gen_ai.usage.{input,output}_tokens` (from terminal SSE chunk),
  `gen_ai.response.finish_reasons`; emit token metrics; span spans the whole
  stream. Content capture **off** by default (PII). Honor
  `OTEL_SEMCONV_STABILITY_OPT_IN`. *Driver:
  opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-spans — no general
  framework ships this; Lancelot is an AI framework.*
- **[R]** Default-on/off policy table (§4.4): free-when-unused → on; cost/PII/
  cardinality → off, one flag away.

### 7.5 Containerization (the first impression)

- **[R]** Ship **musllinux abi3 wheel** (`SYNTHESIS §4.7`) — Alpine is a
  startup reflex; missing it = silent Rust source-build = worst onboarding.
- **[R]** `lancelot init --docker` scaffolds: distroless Dockerfile (default),
  Alpine/musl Dockerfile, Chainguard/Wolfi Dockerfile, `.dockerignore`
  (includes `target/`, `.git/`), `docker-compose.yml`.
- **[R]** Reference distroless image **< 60 MB uncompressed**, non-root
  (`USER 65532` numeric), `HEALTHCHECK` via bundled interpreter against
  `/livez`, exec-form entrypoint. *Competitive vs ~120–250 MB FastAPI+uvicorn
  (§1.2).*
- **[S]** `cargo auditable` build so the Rust SBOM is in the wheel; Trivy/Grype
  + `docker scout` in the scaffolded CI; publish "≈0 OS CVEs" scan in docs.
- **[R]** User `docker build` never compiles Rust (prebuilt wheel) — advertise
  it (§1.6).

### 7.6 Kubernetes manifests (provide, don't just document)

- **[R]** `lancelot init --k8s` scaffolds: Deployment (all 3 probes wired with
  sane defaults; `preStop: sleep 5`; `terminationGracePeriodSeconds: 40`;
  `strategy maxSurge:1 maxUnavailable:0`; `securityContext` non-root +
  `readOnlyRootFilesystem` + drop ALL caps; `resources` with the §2.4
  cgroup-interaction comment), PDB (`maxUnavailable: 1`), HPA v2 (CPU + the
  `lancelot_inflight_requests` custom metric), and a reference **KEDA
  `ScaledObject`** (RPS + in-flight). *Drivers: kubernetes.io probes &
  pod-lifecycle; oneuptime.com prestop-zero-connection-drop; kedify.io KEDA-vs-HPA.*
- **[R]** Probe defaults documented in the manifest with rationale comments
  (liveness `timeoutSeconds:2`, startup `failureThreshold:30 periodSeconds:2`).

### 7.7 Serverless / PaaS

- **[R]** Bind `$PORT` (PaaS-injected) as well as `LANCELOT_PORT` — hard
  requirement for Cloud Run/Render/Fly/Heroku (§3, §5).
- **[R]** Fast cold start (the Rust-core structural win): publish a measured
  CI benchmark — framework-attributable cold start target **< 50 ms** vs
  FastAPI+uvicorn's KNOWN 100–200 ms (§3.2, github.com/awslabs/aws-lambda-web-adapter/discussions/514).
  Measure end-to-end (artifact size honesty caveat).
- **[R]** Reference Lambda Dockerfile using **AWS Lambda Web Adapter** layer
  (no code change; `/livez` is the LWA readiness path) — *not* a Mangum-style
  bespoke handler (§3.3).
- **[L]** `@modal.web_server(port)` / `@modal.asgi_app()` documented (covered
  free by §7.8 ASGI surface); don't special-case.

### 7.8 ASGI-compat & migration (the go-to-market)

- **[R, decision]** **Dual-tier: native Rust `FunctionInfo` ABI = hot path;
  ASGI = compatibility/migration boundary only. ASGI is never the fast path.**
- **[R]** `app.mount_asgi(prefix, asgi_app)` — run an existing unmodified
  FastAPI/Starlette/Django-async app under Lancelot's server (Strangler-Fig
  migration IN). Mounted subtree pays the ASGI tax by design; native routes
  don't.
- **[R]** `app.asgi()` — Lancelot exposes a spec-compliant ASGI 3.0 callable
  (kills lock-in objection; covers ASGI tooling/middleware during transition;
  free Mangum/Modal coverage). HTTP + WebSocket scope (covers real-world
  FastAPI/Litestar usage); document lifespan/edge gaps honestly.
- **[S]** `__rsgi__`-style entrypoint so Granian/RSGI deployers can run
  Lancelot (cheap; proven pattern — github.com/emmett-framework/granian RSGI).
- **[R]** Middleware is native/Rust only — ASGI-compat is the *app-callable*
  boundary, **never** the `BaseHTTPMiddleware` `call_next` model
  (`SYNTHESIS §3` FastAPI-AVOID, `fastapi/01 §0.5`).

### 7.9 Config / 12-factor

- **[R]** Boot from environment alone — **zero config files required**. No
  mandatory `lancelot.toml`.
- **[R]** Validate all operational config in Rust **before fork**; invalid →
  immediate non-zero exit with a clear message (never hang on stdin).
- **[R]** `pydantic-settings` integration (DI provider, resolved once at
  registration) rather than a competing settings system.
- **[R]** Hot reload dev-only, in-process, **hard-disabled** when not a TTY or
  `LANCELOT_ENV=production` (fixes `robyn/03 §0.4` CLI-lock + prevents
  reload-in-prod outage class).
- **[S]** `LANCELOT_ENV` (development/staging/production) flips safe defaults
  (reload, verbose errors, doc exposure); every knob independently overridable.

### 7.10 The top-10, ranked by adoption impact

1. **`mount_asgi()` + `app.asgi()` dual-tier ASGI-compat** — without it,
   nobody can migrate; with it, every FastAPI shop can adopt route-by-route.
   *This is #1: it is the entire go-to-market.* (§6)
2. **SIGTERM graceful drain in the Rust supervisor** — Robyn's fatal gap; the
   difference between "production-grade" and "toy". (§2.3, §7.2)
3. **cgroup-aware worker count** — the `os.cpu_count()` bug is a guaranteed
   production meltdown under any CPU limit. (§2.4, §7.3)
4. **Three const-tier probe endpoints** — liveness must survive a saturated
   pool or K8s kills working pods. (§2.1, §7.1)
5. **Built-in Prometheus `/metrics` + OTLP + JSON logs, default-on** — table
   stakes; FastAPI's fragmented zoo is the opening. (§4, §7.4)
6. **GenAI semantic-convention LLM spans on the built-in client** — the
   observability differentiator no general framework ships. (§4.3, §7.4)
7. **Scaffolded Docker + K8s manifests (`lancelot init`)** — wins the startup
   with no platform team; "provide, don't document". (§1.3, §2.6, §7.5-7.6)
8. **Sub-50 ms measured cold start** — the most legible perf claim; sells
   every scale-to-zero/Lambda/Cloud Run shop. (§3.2, §7.7)
9. **musllinux abi3 wheel + < 60 MB distroless image** — Alpine reflex + image
   size are first-impression adoption gates. (§1, §7.5)
10. **Bind `$PORT` + zero-config-file boot + fail-fast config validation** —
    the 12-factor minimum every PaaS silently requires. (§5, §7.7, §7.9)

### 7.11 The ASGI-compat recommendation (one paragraph, the decision)

**Ship a native Rust `FunctionInfo`-style ABI as the sole hot path, and a
dual-direction ASGI compatibility surface (`mount_asgi()` to bring legacy apps
in, `app.asgi()` to expose Lancelot as an ASGI callable) as the migration and
ecosystem bridge — ASGI is explicitly never the fast path.** This is not a
compromise; it is the proven Granian/RSGI model
(github.com/emmett-framework/granian RSGI spec — Rust core + RSGI for speed +
ASGI for compat, one app object, transparent negotiation) applied to Lancelot's
exact situation. The native ABI is where the entire performance thesis lives
(zero-GIL parse, registration-time compiler, const-tier, native streaming —
`SYNTHESIS §1`); the ASGI surface is where adoption lives (Strangler-Fig
migration from FastAPI with zero rewrite, kill the lock-in objection, free
Mangum/Modal/tooling coverage). Skipping the ASGI surface makes Lancelot a
benchmark toy nobody can deploy into an existing FastAPI codebase; making ASGI
the core re-imposes the exact per-request Python tax the Rust core exists to
eliminate. Dual-tier, ASGI-as-on-ramp, is the only answer that is both fast and
adoptable.

---

## 8. Sources

**Docker / packaging:**
- fastapi.tiangolo.com/deployment/docker — official FastAPI container guidance (exec-form CMD, single-process-per-container, deprecated gunicorn base image)
- nerdleveltech.com/distroless-python-containers-with-uv-tutorial — 2026 distroless+uv multi-stage, glibc-match, numeric nonroot UID 65532
- medium.com/@bdalpe/wolfi-a-small-glibc-based-python-container-base — Wolfi ~22 MB compressed / ~65 MB; glibc wheel compat
- edu.chainguard.dev/open-source/wolfi/wolfi-with-dockerfiles ; chainguard.dev/unchained/minimal-container-images — distroless CVE-surface argument
- oneuptime.com/blog/post/2026-02-09-distroless-chainguard-minimal-images — distroless inventory (no sh/apt/pip), size/CVE
- medium.com/@ThinkingLoop FastAPI-images-under-100MB ; davidmuraya.com slimmer-fastapi-multistage — FastAPI image-size baselines, .dockerignore
- future-architect.github.io/articles/20250602a — 2025 FastAPI-on-Docker simplification (uvicorn --workers, no gunicorn)
- michaelbommarito.com/wiki/.../manylinux-rust-builds ; pyo3.rs building-and-distribution ; maturin.rs — abi3 / manylinux / musllinux wheel building

**Kubernetes:**
- kubernetes.io/docs/concepts/configuration/liveness-readiness-startup-probes — probe semantics, liveness-checks-dependency anti-pattern, defaults
- kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle — termination flow, grace period default 30 s
- oneuptime.com/blog/post/2026-02-09-prestop-hooks-zero-connection-drop — endpoint-propagation race, preStop sleep values, YAML
- kubernetes.io/docs/tasks/run-application/configure-pdb ; statsig.com/blog/kubernetes-pdb-maxunavailable — PDB maxUnavailable guidance
- dev.to/young_gao zero-downtime-2026 ; oneuptime.com/blog/2026-01-25 rolling-updates-zero-downtime — maxSurge/maxUnavailable, the 3-things framing
- kedify.io/resources/blog/keda-vs-hpa ; datadoghq.com/blog/autoscaling-custom-metrics ; aws.amazon.com/blogs/mt KEDA+Prometheus — HPA-vs-KEDA, RPS/queue scaling, scale-to-zero
- bugs.python.org/issue36054 & /issue80235 ; runebook.dev os.cpu_count ; victoriametrics.com/blog/kubernetes-cpu-go-gomaxprocs ; github.com/VictoriaMetrics/VictoriaMetrics/issues/8808 — os.cpu_count vs cgroup, cpu.max parsing bug

**Serverless / PaaS:**
- medium.com/@bhagyarana80 deploying-fastapi-on-serverless — Cloud Run/Lambda/Vercel cold-start & concurrency numbers, "Cloud Run runs a containerized HTTP server"
- github.com/awslabs/aws-lambda-web-adapter/discussions/514 — LWA vs Mangum: LWA runs a real HTTP server, no ASGI/code change, cold-start parity; FastAPI+uvicorn +100–200 ms
- render.com/articles/alternatives-to-fly-io ; techsy.io/en/blog/railway-vs-render-vs-fly-io ; devtoolpicks.com ; expresstech.io — scale-to-zero matrix, cold-start numbers
- modal.com/docs/guide ; introl.com/blog/serverless-gpu-platforms — Modal warm-pool / cold-start
- johal.in/aws-lambda-webadapter-python-asgi-wsgi-apigateway-local-testing-2026 — LWA 2026 ASGI/WSGI

**Observability:**
- opentelemetry.io/docs/specs/semconv/gen-ai/ , .../gen-ai-spans , .../gen-ai-metrics — GenAI semconv attribute names, span naming, token metrics, experimental status
- datadoghq.com/blog/llm-otel-semantic-convention — vendor ingestion of GenAI semconv (Datadog ≥1.37)
- github.com/trallnag/prometheus-fastapi-instrumentator — default metric names/labels, cardinality grouping
- last9.io/blog/integrating-opentelemetry-with-fastapi ; signoz.io/blog/opentelemetry-fastapi ; opentelemetry.io/docs/zero-code/python — FastAPI OTel instrumentation, OTLP default gRPC vs http/protobuf, env contract, logging auto-instrumentation
- (Granian built-in metrics / OTLP — search corpus, talkpython.fm/episodes/463)

**Config / 12-factor:**
- medium.com/@hadiyolworld007 fastapi-pydantic-settings-twelve-factor ; blog.greeden.me 2025-11 no-drama-config ; docs.pydantic.dev/concepts/pydantic_settings ; medium.com/datamindedbe twelve-factor-pydantic — env-precedence, fail-fast-at-startup, `*_FILE` secret pattern, .env.example

**ASGI / migration:**
- asgi.readthedocs.io ; starlette.dev/middleware — ASGI as ecosystem contract; Starlette middleware = ASGI app chain
- github.com/emmett-framework/granian/blob/master/docs/spec/RSGI.md — RSGI interface, the `__rsgi__`+`__call__` dual-protocol mechanism, Rust-serialization rationale
- deployhq.com/blog/python-application-servers-in-2025 — ASGI-dominance, when a Rust server exposes ASGI vs native, Granian one-binary multi-protocol
- github.com/Kludex/starlette/discussions/2287 — Starlette RSGI-support discussion

**Local cross-references (the RE corpus this design extends):**
- `Lancelot/research/SYNTHESIS.md` — §0 framework summaries, §1 steal-list (#1 reg-time compiler, #2 GIL choreography, #3 multiprocess, #5 const-tier, #10 FunctionInfo ABI), §3 anti-patterns, §4 architecture (4.1 core, 4.2 process model, 4.5 streaming, 4.6 AI layer, 4.7 packaging)
- `research/robyn/03-python-api-pyo3-bridge.md §0` — no graceful shutdown, no crashed-worker supervisor, `os.cpu_count()` ignores cgroups, `input()`-on-port-conflict container hang, CLI-only `--dev` (the ops anti-patterns this doc fixes)
- `research/robyn/01-rust-core-runtime-and-gil.md §0.2-0.5,§10` — SO_REUSEPORT multiprocess, GIL choreography, per-token streaming weakness
- `research/robyn/02-rust-routing-types-websockets.md §0` — const-router (the probe-endpoint substrate)
- `research/fastapi/01-routing-and-request-lifecycle.md §0.3,§0.5` — 40-thread cliff, BaseHTTPMiddleware disaster (why ASGI-compat ≠ call_next middleware)
- `research/fastapi/03-validation-serialization-openapi.md §0.7-0.9` — SSE/keepalive architecture (the streaming-shutdown + GenAI-span-lifetime rule)
- `research/blazeio/02-request-response-streaming-static.md §0` ; `blazeio/03 §0.2` — streaming substrate, async-client pool (shutdown must close it)

*Generated 2026-05-18. Sibling to SYNTHESIS.md. Lancelot/ is gitignored.
This is decision-grade design research, not the final design — open questions
(hyper-vs-actix, how far DI moves into Rust) remain in SYNTHESIS §4.8.*
