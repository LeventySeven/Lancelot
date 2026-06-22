# Serialization — byte-path domain synthesis

**Domain:** Serialization. pydantic-core `to_json`/`dump_json` (the marquee large-payload win), canonical / cache-stable JSON, the FastAPI `jsonable_encoder` fallback surface, untyped/error-body serialization, streaming JSONL serialization. The crossover (~1–4 KB) and the encoder-parity surface.

**Method:** read the full mine (`01_robyn`, `02_fastapi`, `03_blazeio`, `04_serving`) + the `05_lancelot_state` baseline, then **ground-truthed against Lancelot source** — `lancelot/validation.py` (`_wire_json`, `_json_default`), `lancelot/canonical.py`, `src/serialize.rs` (`to_json_bytes`, `_to_json`), `lancelot/asgi.py` 200-path (`_ta.dump_json` + `_wire_json` untyped path), and `conformance/vendor/fastapi/tests/test_jsonable_encoder.py`. Date 2026-06-22.

**Bottom line:** the serialization story is **almost entirely HAVE and ahead.** The two proven wins (pydantic-core `dump_json` on the typed 200 path; orjson-or-pydantic-core on the untyped path) are both shipped and gated correctly. The whole "add a faster JSON serializer" space is REFUSED (orjson/ujson/simd-json/Arrow/pickle — see the ledger). What's actually left is **two small correctness fixes** (a Decimal int/float divergence + the `jsonable_encoder` fallback-table completeness gap) and a few **doc-accuracy / honesty corrections**. No L-sized build, no marquee gap.

---

## What was verified about the ACTUAL Lancelot serializer chain (ground truth)

There are **two** serialize paths off the 200, plus the error path — all confirmed in source:

1. **Typed 200 (`route.response_model is not None`)** — `asgi.py:1751` `_ta.dump_json(_val, by_alias=, exclude_unset=, exclude_defaults=, exclude_none=, include=, exclude=)`. Pure pydantic-core, one Rust pass, bytes out, TypeAdapter cached at registration (`route._response_ta`, P1). `response_model_skip_validation` (M17 P4) skips the `model_validate` pass and feeds the raw result to `dump_json` (hard-fenced against `exclude_unset`). **This is the marquee large-payload win (~2.3× @ 69 KB).** Matches FastAPI's `ModelField.serialize_json` → `TypeAdapter.dump_json` exactly (di_params.md M8, core.md #1).

2. **Untyped 200 (no `response_model`)** — `asgi.py:1769` `_wire_json(result)`. `_wire_json` (`validation.py:266`) = **orjson.dumps (soft-dep, lazy+memoized) → `_native._to_json` (pydantic-core) → stdlib `json.dumps(default=_json_default)`**.

3. **Error / 422 / problem bodies** — also `_wire_json` (`errors_to_422`, `asgi.py:833/913/1858/1861/1994`). So the 422 bytes provably can't drift from the success bytes (same serializer).

4. **Rust `src/serialize.rs::to_json_bytes`** (used by native-server dispatch + R3 stream chunk JSON): **orjson.dumps → pydantic_core.to_json → stdlib json**, each via `PyOnceLock` (GIL-deadlock-safe, the M7 lesson). `_to_json` is the pyfunction the Python `_wire_json` calls.

5. **Canonical** — `canonical.dumps_canonical` (`canonical.py:19`) = stdlib `json.dumps(sort_keys=True, separators=(",",":"), ensure_ascii=False, allow_nan=False)`. **Deliberately NOT pydantic-core** (pydantic-core `to_json` has no stable sorted-key mode). Stdlib-only, no dep.

**Two ledger/CLAUDE-md claims are FACTUALLY WRONG and should be corrected (honesty, not a build):**
- ❌ Ledger line 59: *"Already have simd-json on untyped paths."* **There is no simd-json anywhere in `Cargo.toml` or `src/`** (grep clean). The untyped path is orjson→pydantic-core→stdlib. simd-json is listed only as an aspirational dep in the ledger's dep-roster; it is not wired. Anyone reasoning "we already have a SIMD serializer" is wrong.
- ❌ Ledger line 72 / DEFERRED: *"orjson swap experiment (T1.2) — bench-gate."* **orjson is already SHIPPED** as a soft-dep auto-detect in BOTH `_wire_json` (Python, `validation.py:281-297`) and `to_json_bytes` (Rust, `serialize.rs:38-86`), tagged M6-T1.2b. The deferred line is stale — it's HAVE, not pending.

---

## must_build (new or improvable, real byte-path / operator value)

### 1. Decimal-encoder int/float fidelity in `_json_default` (correctness fix) — **S**
**Mechanism.** FastAPI's `decimal_encoder` (`encoders.py:84`, di_params.md M9): `Decimal` with `exponent >= 0` → **int**, else → **float**. `Decimal(2) → 2`, `Decimal("1.23") → 1.23`. Round-trip fidelity for `Numeric(10,0)` / integer-valued decimals.
**Lancelot state — PARTIAL (divergent).** `validation.py:254-255`: `if isinstance(obj, _dcm.Decimal): return float(obj)` — **always float**, so `Decimal(2) → 2.0`. This only fires on the `_wire_json` *stdlib fallback* path (untyped/Any responses or error bodies that contain a bare `Decimal`); the typed `dump_json` path and orjson path handle Decimal natively/correctly.
**Why.** Silent data corruption for financial / integer-decimal APIs that return untyped dicts (`{"qty": Decimal(2)}` → `2.0` not `2`). One-line fix.
**Source cite.** di_params.md M9 #1 (`encoders.py:84`, `decimal_encoder`); FastAPI `test_decimal_encoder_int`.
**Gate risk.** **None.** Verified: `conformance/vendor/fastapi/tests/test_jsonable_encoder.py::test_decimal_encoder_int` imports `from fastapi.encoders import jsonable_encoder` (vendored, no conftest swap, no `lancelot/encoders.py`) — so it tests vendored FastAPI, not Lancelot, and **passes regardless**. The fix is a pure runtime-correctness improvement with zero conformance/clippy/perf exposure (off the hot path — fallback branch only).
**Approach.** `validation.py:254` →
```python
if isinstance(obj, _dcm.Decimal):
    return int(obj) if obj.as_tuple().exponent >= 0 else float(obj)
```
NaN/Infinity already excluded upstream (`allow_nan=False` raises). Add a unit test in `tests/` (`Decimal(2)→2`, `Decimal("1.23")→1.23`); do NOT touch the vendored conformance test.

### 2. `_json_default` fallback-table completeness (parity fix) — **S**
**Mechanism.** FastAPI `ENCODERS_BY_TYPE` (`encoders.py:84-112`) covers `deque`, `GeneratorType`, `NameEmail`, `SecretStr`, `SecretBytes`, `Pattern`(→`.pattern`), `IPv4/6Address/Interface/Network`(→str), `Url`/`AnyUrl`(→str), plus the `sqlalchemy_safe` `_sa`-prefixed-key filter.
**Lancelot state — PARTIAL.** `_json_default` (`validation.py:213-263`) covers BaseModel, dataclass, datetime/date/time/timedelta, UUID, Enum, Decimal, PurePath, set/frozenset, bytes/bytearray. **Missing:** `deque`, `GeneratorType`, the Pydantic-extra types (`SecretStr/Bytes`, `Pattern`, `IPv4/6*`, `NameEmail`, `Url`/`AnyUrl`), and `_sa` SQLAlchemy-internal-key filtering.
**Why.** Only the *untyped* fallback path is affected (typed routes go through pydantic-core which already knows these). The high-value subset is **`deque`** (common), **`Pattern`** (regex configs), and the **`_sa` filter** (the classic "serializing a SQLAlchemy ORM object leaks `_sa_instance_state` → crash/garbage" footgun for the DB-web archetype). The Pydantic-extra IP/Secret/Url types are lower-value (users hitting them usually have a typed model → pydantic-core handles it).
**Source cite.** di_params.md M9 #2/#3/#4 (`encoders.py:84-112, 288-297`).
**Gate risk.** **None** (same reasoning as #1 — conformance test is vendored; this is Lancelot-runtime parity only). Each added isinstance branch is fallback-path only, zero hot-path cost.
**Approach.** Add the branches to `_json_default`. Keep it dependency-free: `deque`/`GeneratorType` are stdlib; for the Pydantic types, gate behind a lazy `try: from pydantic import SecretStr, ...` (never an unconditional import — cardinal rule). Add `_sa`-key drop when the object is a dict whose keys start with `_sa` (mirror FastAPI's `sqlalchemy_safe=True` default). Mark the IP/Url/NameEmail subset as optional polish.

### 3. Honesty corrections in CLAUDE.md / the ledger (doc-only) — **S**
**Mechanism.** Two false claims (above): "simd-json on untyped paths" (not wired) and "orjson swap experiment deferred" (already shipped).
**Lancelot state — needs correction.** Both are stale/wrong vs source.
**Why.** A future agent reading "we have SIMD JSON" or "orjson is still a pending experiment" makes wrong build decisions (re-derives orjson, or over-claims SIMD perf). Cardinal rule #1 / no-bullshit.
**Source cite.** `src/serialize.rs` (orjson IS wired, simd-json is NOT); ledger lines 59 & 72.
**Gate risk.** None (docs only).
**Approach.** Edit `05_lancelot_state/research_plans_ledger.md` (and the CLAUDE.md Tier-D note if it repeats the simd claim): replace "Already have simd-json on untyped paths" with "untyped/error path = orjson(soft-dep)→pydantic-core→stdlib; no simd-json wired"; move "orjson swap (T1.2)" from DEFERRED to BUILT/HAVE (M6-T1.2b).

---

## improve (smaller upgrades, optional)

### I1. orjson small-payload thread-hop note is N/A here, but a `to_json` size note is worth it — **S**
Blazeio (modules core M11) uses a **size threshold** for off-loop compression (`< INBOUND_CHUNK_SIZE` → inline, avoid thread-hop). The serialization analogue: the orjson-first ordering in `to_json_bytes` costs one extra C-call attempt (orjson.dumps → TypeError → pydantic-core) on every payload that orjson *can't* serialize (any response containing a bare `BaseModel` without `OPT_SERIALIZE_PYDANTIC`). For a typed route this never happens (it goes through `dump_json` path #1, not `_wire_json`). For an untyped route returning a dict-of-models, every response pays a failed orjson attempt before pydantic-core. **Improvement:** when orjson raises TypeError once for a given route's result shape, the cost is bounded (one extra call), so this is genuinely micro — **document, don't build.** The orjson-first ordering is correct for the dict-heavy common case (the M5 lab measured 13.8k vs 6.3k ops/s @ 70 KB). Gate risk: none. Verdict: leave as-is; just note the failed-attempt cost in a comment if not already there (it is — `serialize.rs:68-86`).

### I2. Streaming JSONL per-item serialize is already pydantic-core — confirm, don't change — **S**
`asgi.py:1593/1606` auto-wrap path: `_item_adapter.dump_json(x)` per item for `AsyncGenerator[Model]` returns — pure pydantic-core, per-item `TypeAdapter`. This is the right serializer. The ONLY caveat (already in CLAUDE.md Tier-D): per-chunk Pydantic *validation* on streams is a perf cliff — but here it's `dump_json` on an already-typed item, which is the cheap path. **No change.** Note: vLLM M9's `exclude_unset=True` per SSE chunk (proj_inference.md) is a *user-side* serialization choice (the user controls their chunk model); Lancelot's stream pump passes user bytes through — correctly user-owned.

---

## have (already shipped — explicitly NOT to build, so nothing is redundant)

| Mechanism | Lancelot location | Source cite |
|---|---|---|
| **pydantic-core `dump_json` typed 200 fast path** (single Rust pass, bytes out, no dict intermediate) — the marquee large-payload win (~2.3× @ 69 KB) | `asgi.py:1751`; `route._response_ta` cached at registration | core.md #1, di_params.md M8 |
| **`DefaultPlaceholder`-gated fast path** (explicit `response_class=` falls through to encode path; default uses dump_json) | `routing.py` Default/DefaultPlaceholder re-export; `asgi.py:1724` response_model branch + response_class dispatch | core.md #1/#7 |
| **`response_model_skip_validation`** (skip `model_validate`, ~4× on list reads), hard-fenced against `exclude_unset`/`exclude_defaults`/`by_alias` | `asgi.py:1739`, `routing.py` fence | ledger P4 (BUILT, hard-fenced) |
| **All `dump_json` kwargs** (`by_alias`, `exclude_unset`, `exclude_defaults`, `exclude_none`, `include`, `exclude`) | `asgi.py:1751-1758` | core.md #1 |
| **orjson soft-dep auto-detect** (Python `_wire_json` + Rust `to_json_bytes`), TypeError→pydantic-core fall-through, NOT a runtime dep | `validation.py:281-297`, `serialize.rs:38-86` | M6-T1.2b; **closes ledger DEFERRED T1.2** |
| **`_wire_json` = ONE serializer for 200-untyped AND 422/error** (success/error bytes provably can't drift) | `validation.py:266`, `errors_to_422` | core.md #8 (bytes parity) |
| **`_json_default` jsonable-encoder-shape fallback** (BaseModel/dataclass/datetime/UUID/Enum/PurePath/set/bytes + asyncpg lying-UUID via `str()`) | `validation.py:213-263` | di_params.md M9 |
| **`PyOnceLock` GIL-safe memoization** of `pydantic_core.to_json` + `orjson.dumps` callables (avoids the std-OnceLock×GIL deadlock) | `serialize.rs:19-49` | rust_core.md (GIL lesson), CLAUDE.md memory |
| **`canonical.dumps_canonical`** (stdlib sort_keys, compact, no-NaN, utf-8 — KV-cache / prompt-cache stability) | `canonical.py:19` | bytepath.md §3 |
| **`is_body_allowed_for_status_code`** body-wipe for 204/205/304/1xx | `asgi.py` (M2 push2) | core.md #10 |
| **Streaming JSONL serialize** (per-item `dump_json` on `AsyncGenerator[Model]`; `application/jsonl`) | `asgi.py:1593/1606` | core.md #4 |
| **`ORJSONResponse`/`UJSONResponse` pass-throughs** (alias to JSONResponse; contract works w/o orjson/ujson installed) | `responses.py:159` | core.md #8 |
| **`intern!` on serialize hot-path names** (`to_json`, `pydantic_core`, `orjson.dumps`) | `serialize.rs:22-44` (M7 T4.13) | rust_core.md |
| **Single-pass `validate_json`** (input side — `TypeAdapter.validate_json(raw_bytes)`, one Rust pass, stdlib fallback only for byte-identical 422 loc) | `validation.py:107` | core.md (validation) |

---

## refuse (violates a cardinal rule or the Tier-D ledger)

| Item | Why refuse |
|---|---|
| **Custom JSON serializer derivation (orjson/ujson re-implement)** | Ledger lines 34/44. pydantic-core is already Rust-fast; orjson is already a soft-dep; orjson/ujson are *deprecated in FastAPI 0.136.1*. Nothing to build. (Blazeio's ujson-as-runtime-dep is REFUSE: core.md M10 — slower than pydantic-core on typed models + violates pydantic-only rule.) |
| **simd-json custom serialize path** | Ledger line 59. The honest position: pydantic-core handles typed, orjson/pydantic-core handle untyped. Wiring simd-json adds a Rust dep + a serde round-trip for a path that's already C-fast; no measured win. (Also: the ledger's claim we "already have" it is false — but the conclusion "don't build it" stands.) |
| **Arrow IPC / pickle serde / msgpack / CBOR binary wire** | Ledger line 46. No HTTP/JSON-API analogue; pickle-on-edge = RCE; binary formats are user-side (the user owns the AI payload shape). Canonical stays JSON (stdlib). |
| **pydantic-core path for `dumps_canonical`** | pydantic-core `to_json` has no stable sorted-key mode; canonical's whole point is deterministic sorted bytes. Stdlib `json.dumps(sort_keys=True)` is byte-stable and dep-free. Re-implementing a Rust sorted-key canonical encoder = a Rust-side dict-tree walk over MB payloads (the exact case `serialize.rs:64` warns needs `consume_budget`) for zero throughput win on a non-hot path (canonicalization is opt-in, called by the user before caching, not per-request). Not worth it. |
| **SIMD base64 for embeddings** | Ledger line 52. `pip install pybase64` already gives the 3×; parity expected, no build. |
| **`Page[T]` response envelope / pagination serialization wrapper** | Ledger line 62. 5–7 teams each ship a different shape; `fastapi-pagination` exists. (Lancelot SHOULD build the `PageParams` DoS *cap* — but that's a query-param/limit concern, not serialization; out of this domain.) |
| **Per-chunk Pydantic validation on streams** | Ledger line 40 / CLAUDE.md Tier-D. Perf cliff. (The existing per-item `dump_json` on auto-wrapped `AsyncGenerator[Model]` is fine — it's serialize, not re-validate.) |
| **Coupling the bytes fast-path to a Starlette internal** (e.g. matching `test_explicit_response_class_uses_json_dumps` which mocks `starlette.responses.json.dumps`) | Conformance-ceiling item #2 (CLAUDE.md). We serialize via pydantic-core (faster, arguably more correct); matching the mock means coupling to a Starlette internal. Stays RED by design. |

---

## Buildability / gate summary

| Item | Size | Gate risk | Net |
|---|---|---|---|
| Decimal int/float fix (`_json_default`) | S | none (fallback path; conformance test is vendored) | correctness win, financial/untyped APIs |
| `_json_default` table completeness (`deque`/`Pattern`/`_sa` filter first) | S | none | parity win, DB-web archetype footgun |
| Doc honesty (simd-json claim, orjson-deferred-is-done) | S | none (docs) | prevents wrong future build decisions |
| I1/I2 (orjson ordering note, streaming-serialize confirm) | S | none | no-op / document-only |

**No M or L builds in this domain.** The serialization byte-path is mature: the two proven wins are shipped and gated correctly, the whole "faster serializer" space is correctly REFUSED, and what remains is small correctness/parity polish on the `jsonable_encoder` *fallback* surface plus two doc corrections. The highest-value single change is the **Decimal int/float one-liner** (silent corruption risk, zero gate cost).
