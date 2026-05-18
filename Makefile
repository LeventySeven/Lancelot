SHELL := /bin/bash
export PATH := $(HOME)/.cargo/bin:$(PATH)
export PYO3_PYTHON := $(CURDIR)/.venv/bin/python
VENV := $(CURDIR)/.venv/bin

# The authoritative M0 acceptance gate. Each line is merge-blocking: `make`
# aborts on the first non-zero exit. Covers the 7 M0 Definition-of-Done
# assertions:
#   #1 abi3 wheel imports under py3.13 AND py3.14  -> pytest test_m0_acceptance
#      (installs the built dist/ wheel into clean per-interpreter venvs)
#   #2 .await-in-with_gil fails to compile          -> cargo test (trybuild)
#   #3 POST /echo round-trips over a real socket    -> pytest test_m0_echo
#   #4 GIL released across the handler await        -> pytest test_dispatch
#   #5 <=5 framework Rust allocs / request          -> alloc_probe (dhat)
#   #6 two procs bind the same port (SO_REUSEPORT)  -> cargo test (socket::)
#   #7 SIGTERM exits cleanly (exit 0, no traceback) -> pytest test_m0_lifecycle
.PHONY: m0
m0:
	export PATH="$(HOME)/.cargo/bin:$$PATH"; set -e; \
	$(VENV)/maturin build --release --out dist; \
	$(VENV)/maturin develop --release; \
	cargo test; \
	cargo run --bin alloc_probe --features dhat-heap --release; \
	$(VENV)/python -m pytest tests/ -q; \
	echo "M0 ACCEPTANCE: all 7 gates green (DoD #1-#7)"
