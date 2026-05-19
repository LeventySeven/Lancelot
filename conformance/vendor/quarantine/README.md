# Quarantine — RED-first conformance seed

Sub-plans (P3..P7) add FastAPI-shaped tests for *not-yet-implemented*
features HERE first (RED). They are EXCLUDED from the ratchet ratio
(`conformance/runner.py --ignore`). When the feature lands and the test
goes green, MOVE the file up into `conformance/vendor/` — the active
pass-count rises and the ratchet floor ratchets up with it.

This lets each sub-plan keep strict RED-first TDD without the
"parity may only rise" ratchet hard-failing CI on the new RED test.
