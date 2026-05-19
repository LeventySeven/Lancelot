"""P4/M4 LEDGER (quarantined, excluded from the ratchet) — security/CORS
long-tail not in M1-P4 scope (deep/03 §1.4/§1.5/§2 + spec line 68):
  1. OAuth2 password flow + the byte-exact form 422 shape (needs M1-P6
     forms);
  2. OpenID Connect scheme (`open_id_connect_url`);
  3. HTTPDigest scheme;
  4. OAuth2 SCOPE ENFORCEMENT — MUST-13: FastAPI does NOT enforce; Lancelot
     plumbs scopes into OpenAPI only. Full SecurityScopes injection +
     user-side enforcement parity is M4.
  5. CORS genuinely-unimplemented tail: `allow_origin_regex` /
     wildcard-subdomain origins, `expose_headers`
     (Access-Control-Expose-Headers), and
     `access-control-request-private-network`. (V1 + A2 + A3 fixed in
     M1-P4 T12 and BYTE-verified vs real fastapi 0.136.1: literal-`*`,
     `Vary: Origin`, preflight-400 origin/method/**headers** incl. ACRH
     validation, `sorted(SAFELISTED∪allow_headers)`, unconditional ACAC,
     Content-Length — see tests/test_m1_p4_byte_parity.py.)
  6. OpenAPI TRANSITIVE-DEPENDENCY PARAMETER FLATTENING (P7/DI×OpenAPI):
     a `Depends(fn)` where `fn` itself declares a query/path/header/cookie
     param — FastAPI flattens that field into the *operation's*
     `parameters` list. Runtime DI already resolves it (solve_async
     recurses the sub-Plan); the `422`-response PREDICATE is now
     FastAPI-faithful for this shape (recursive `_plan_has_field`, fixed
     in M1-P4 T12 + byte-verified). What remains RED is only the
     transitive field appearing in `parameters`. Surfaced by the M1-P4
     byte oracle; graduate when the OpenAPI ratchet reaches it.
Graduate assertions into conformance/vendor/ as P6/M4 implements them."""
import pytest

import fastapi
from fastapi.testclient import TestClient as FA
import lancelot
from lancelot.testclient import TestClient as LC


def test_oauth2_password_form_422_byte_shape_M1P6():
    # Full OAuth2PasswordRequestForm parsing + the string_pattern_mismatch
    # 422 (deep/03 §1.4) needs M1-P6 forms — RED until then.
    pytest.fail("M1-P6: OAuth2 form parsing + byte-exact 422 not yet implemented")


def test_openid_connect_and_digest_schemes_M4():
    pytest.fail("M4: OpenIdConnect + HTTPDigest schemes not yet implemented")


def test_scope_enforcement_parity_M4():
    # MUST-13: scopes are plumbed (OpenAPI) not enforced in M1; full
    # SecurityScopes-injection parity is M4.
    pytest.fail("M4: OAuth2 scope SecurityScopes injection/enforcement parity")


def test_transitive_dep_param_in_operation_parameters_P7():
    """A dependency's OWN query param must appear in the operation's
    `parameters` (FastAPI flattens transitive dep fields). The 422-response
    predicate for this shape is already FastAPI-faithful (M1-P4 T12); only
    the `parameters` listing is RED. Excluded from the ratchet until the
    OpenAPI flattening lands."""
    import lancelot
    from lancelot.testclient import TestClient as LC
    from lancelot.params import Depends

    def _inner(page: int = 1):
        return page

    app = lancelot.App()

    @app.get("/x")
    def x(v=Depends(_inner)):
        return {}

    with FA(fastapi.FastAPI()) as _:
        pass
    fa = fastapi.FastAPI()

    def _fi(page: int = 1):
        return page

    @fa.get("/x")
    def fx(v=fastapi.Depends(_fi)):
        return {}

    with LC(app) as lc, FA(fa) as f:
        lp = lc.get("/openapi.json").json()["paths"]["/x"]["get"].get(
            "parameters", [])
        fp = f.get("/openapi.json").json()["paths"]["/x"]["get"].get(
            "parameters", [])
        assert {p["name"] for p in fp} == {"page"}  # FastAPI flattens it
        assert {p["name"] for p in lp} == {"page"}, (
            "RED: Lancelot does not yet flatten transitive dep params into "
            "the operation `parameters` (P7 OpenAPI ratchet item)")
