"""M1-P7 STATUS LEDGER (RED, quarantined, ratchet-excluded). M1-P7
delivers the **honest gated measurement infrastructure**, not a >=90%
claim: the pinned real fastapi 0.136.1 test corpus (581 files), the
deterministic AST classifier (172 → 156 after `inline_snapshot` rule;
distribution A=156 A2=306 EXCLUDE=18 B=12), the swap-driven runner, and
the anti-cheat ratchet (`classify.py --check` blocks exclude.toml drift;
floor only-rises). The measured baseline is **91/249 = 36.5%** of the
Class-A test surface. RED here = the M1 EXIT gap the ratchet drives the
floor up toward (≥90% / 100%-of-v0-subset), the §3-69 real-Rust lane,
and Lane A2 byte-snapshots:

  1. The M1 ≥90% / 100%-of-v0-subset target requires the FastAPI-feature
     surface NOT yet in Lancelot. Concrete top failing families at the
     m1-p7-green baseline (158 total fails/errors across the 156 A-files):
       7  test_invalid_path_param          (invalid-param 422 edges)
       6  test_allow_inf_nan_in_enforcing  (pydantic constraint surface)
       6  test_forms_single_model          (Form-MODEL body, not just scalars)
       5  test_query_cookie_header_model_extra_params
       4  test_invalid_sequence_param
       4  test_json_type
       4  test_tutorial/test_dependencies/test_tutorial008d (yield edges)
       4  test_tutorial/test_additional_status_codes  (status_code surface)
       3  test_forms_from_non_typing_sequences
       3  test_tutorial/test_strict_content_type     (content-type strict)
       2  test_required_noneable
       2  test_router_redirect_slashes
     Plus FastAPI's full FastAPI(...) constructor kwargs (docs_url,
     redoc_url, openapi_url, default_response_class, separate_input_
     output_schemas, ...) which Lancelot's App.__init__ does not accept.
     These ARE the M1 exit work — feature breadth, not measurement gaming.
  2. Lane A2 (306 snapshot/inline_snapshot tests): byte-exact OpenAPI /
     Swagger-HTML / ReDoc-HTML. v0 requires semantic-equiv only; the
     byte-exact lane is v1+ (separate ratchet).
  3. spec §3-69 (Rust pipeline unification): TestClient drives the
     Python ASGI seam today; the real-network Robyn-integration lane
     (Rust hyper/socket/SSE/graceful-drain/SO_REUSEPORT) is structurally
     not reached by the FastAPI ASGI lane — M3/release work.
Graduate into conformance/vendor/ as the corresponding feature lands;
the ratchet only-rises ensures the number cannot regress."""
import pytest


def test_M1_exit_target_90pct_or_100pct_of_v0_subset():
    pytest.fail("M1 exit: ratchet from 36% -> >=90% / 100%-of-v0-subset "
                "by closing the failing-family gap above (Form-model, "
                "status_code/Response surface, App-constructor kwargs, "
                "etc.) — feature work, not measurement gaming")


def test_lane_A2_openapi_html_snapshots_v1plus():
    pytest.fail("Lane A2: 306 byte-exact OpenAPI/Swagger/ReDoc-HTML "
                "snapshots; v0 is semantic-equiv only (separate ratchet)")


def test_real_rust_lane_S369_robyn_integration_M3():
    pytest.fail("spec §3-69 / M3: real-network Robyn-integration lane "
                "(Rust hyper/socket/SSE/drain/SO_REUSEPORT) — the ASGI "
                "lane structurally cannot exercise it")
