# Vendored FastAPI test corpus — PINNED, READ-ONLY, NEVER EDITED

`tests/` (581 `.py`) and `docs_src/` (454 `.py`) are FastAPI **0.136.1**'s
own released test suite + tutorial apps, extracted **byte-unmodified** from
the official sdist (`fastapi-0.136.1.tar.gz`, verified `diff -rq` clean).

**Cardinal rule (harness-design §4):** a vendored test is *never* edited.
Modifying a test to make it pass converts a measurement into a lie. The
conformance number is whatever this exact suite yields when driven through
`conformance/conftest.py`'s swap against Lancelot.

`FASTAPI_VERSION` pins the corpus. Bumping it (e.g. 0.136 → 0.137) is a
deliberate, reviewed event — a FastAPI upgrade, not a parity change.

Classification (Class A run / A2 snapshot-xfail / B compat / C excluded)
is computed deterministically by `conformance/classify.py`; exclusions are
append-only and must each import a FastAPI-private symbol (the anti-cheat
in `conformance/ratchet.py`) — a behavioral test can never be parked in
the exclude bin.
