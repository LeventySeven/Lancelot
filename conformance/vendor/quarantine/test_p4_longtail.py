"""P4/M4 LEDGER (quarantined, excluded from the ratchet) — security/CORS
long-tail not in M1-P4 scope (deep/03 §1.4/§1.5/§2 + spec line 68):
  1. OAuth2 password flow + the byte-exact form 422 shape (needs M1-P6
     forms);
  2. OpenID Connect scheme (`open_id_connect_url`);
  3. HTTPDigest scheme;
  4. OAuth2 SCOPE ENFORCEMENT — MUST-13: FastAPI does NOT enforce; Lancelot
     plumbs scopes into OpenAPI only. Full SecurityScopes injection +
     user-side enforcement parity is M4.
  5. Full CORS preflight header-set ordering / regex origins / vary.
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
