"""M17 P4 — response_model_skip_validation (opt-in, fenced).

Pins the skip-validate-then-serialize fast path:
  * `@app.get(..., response_model=M, response_model_skip_validation=True)`
    serializes the handler result through the cached TypeAdapter's
    `dump_json` DIRECTLY — no `model_validate` pass (the user vouches for the
    shape; ~4x cheaper on a large read).
  * The serialization-only flags that DON'T need a validated instance still
    apply (by_alias / exclude_none / include / exclude / exclude_defaults).
  * The ONE flag that genuinely needs a validated instance —
    `response_model_exclude_unset` (it reads `__pydantic_fields_set__`, which
    only a validated instance has; on a raw object pydantic-core raises
    `PydanticSerializationError`) — is INCOMPATIBLE and raises `FastAPIError`
    at REGISTRATION (decoration time), never silently wrong at runtime.
  * Default (skip_validation off) is byte-identical to today (the conformance
    story is the validate+dump path).

If a test goes red, the P4 fence or fast path has regressed."""
import pytest
from pydantic import BaseModel

import lancelot
from lancelot.exceptions import FastAPIError
from lancelot.testclient import TestClient


class Item(BaseModel):
    id: int
    name: str


# ---------------------------------------------------------------------------
# 1. skip_validation serializes the handler result DIRECTLY via dump_json.
#    Use an ORM-ish object (no __pydantic_fields_set__) — under the normal
#    (validating) path this is coerced model-style; under skip we dump_json it
#    straight. A conforming shape must serialize correctly either way.
# ---------------------------------------------------------------------------

class _ORM:
    """A non-BaseModel object exposing the response_model's attributes."""
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_skip_validation_serializes_directly():
    app = lancelot.App()

    @app.get("/item", response_model=Item, response_model_skip_validation=True)
    def get_item():
        # An ORM-style object, NOT an Item instance and NOT a dict — pydantic
        # serialization duck-types its attributes against the model schema.
        return _ORM(id=7, name="widget")

    with TestClient(app) as c:
        r = c.get("/item")
        assert r.status_code == 200
        assert r.json() == {"id": 7, "name": "widget"}


def test_skip_validation_list_of_orm_objects():
    """list[Model] response_model returning ORM objects serializes element-wise
    through dump_json without a validate pass."""
    app = lancelot.App()

    @app.get("/items", response_model=list[Item],
             response_model_skip_validation=True)
    def get_items():
        return [_ORM(id=1, name="a"), _ORM(id=2, name="b")]

    with TestClient(app) as c:
        r = c.get("/items")
        assert r.status_code == 200
        assert r.json() == [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]


def test_skip_validation_does_not_validate():
    """Proof it SKIPS validation: a value pydantic would REJECT on validation
    (a str where int is declared) serializes as-is rather than raising a
    ResponseValidationError. The user vouches for the shape."""
    app = lancelot.App()

    @app.get("/loose", response_model=Item,
             response_model_skip_validation=True)
    def loose():
        # id is declared int; we hand back a str. The validating path would
        # raise ResponseValidationError; the skip path serializes it as-is.
        return {"id": "not-an-int", "name": "x"}

    with TestClient(app) as c:
        r = c.get("/loose")
        assert r.status_code == 200
        assert r.json() == {"id": "not-an-int", "name": "x"}


# ---------------------------------------------------------------------------
# 2. The compatible serialization flags still apply under skip_validation.
#    Verified against pydantic-core 2.13: by_alias / exclude_none / include /
#    exclude / exclude_defaults all work on a raw (non-validated) object.
# ---------------------------------------------------------------------------

class Aliased(BaseModel):
    from pydantic import Field as _F
    value: int = _F(serialization_alias="VALUE")
    note: str | None = None


def test_skip_validation_honors_by_alias_and_exclude_none():
    app = lancelot.App()

    @app.get("/a", response_model=Aliased,
             response_model_skip_validation=True,
             response_model_by_alias=True,
             response_model_exclude_none=True)
    def a():
        return _ORM(value=5, note=None)

    with TestClient(app) as c:
        r = c.get("/a")
        assert r.status_code == 200
        # by_alias -> "VALUE"; exclude_none -> "note" dropped.
        assert r.json() == {"VALUE": 5}


class Defaulted(BaseModel):
    a: int = 10
    b: int = 20


def test_skip_validation_honors_exclude_defaults():
    """exclude_defaults is VALUE-based (compares to the field default), so it
    works on a raw object — it does NOT need __pydantic_fields_set__."""
    app = lancelot.App()

    @app.get("/d", response_model=Defaulted,
             response_model_skip_validation=True,
             response_model_exclude_defaults=True)
    def d():
        return _ORM(a=10, b=99)  # a == default -> dropped; b kept

    with TestClient(app) as c:
        r = c.get("/d")
        assert r.status_code == 200
        assert r.json() == {"b": 99}


# ---------------------------------------------------------------------------
# 3. THE FENCE: exclude_unset needs a validated instance -> raise FastAPIError
#    at REGISTRATION when combined with skip_validation.
# ---------------------------------------------------------------------------

def test_skip_validation_incompatible_flag_raises_at_registration():
    app = lancelot.App()

    with pytest.raises(FastAPIError):
        @app.get("/bad", response_model=Item,
                 response_model_skip_validation=True,
                 response_model_exclude_unset=True)
        def bad():
            return _ORM(id=1, name="x")


def test_skip_validation_exclude_unset_alone_is_fine():
    """Sanity: exclude_unset WITHOUT skip_validation is still allowed (the
    fence only fires on the incompatible COMBINATION)."""
    app = lancelot.App()

    @app.get("/ok", response_model=Item,
             response_model_exclude_unset=True)
    def ok():
        return Item(id=1, name="x")

    with TestClient(app) as c:
        assert c.get("/ok").status_code == 200


# ---------------------------------------------------------------------------
# 4. Default-off is byte-identical to the existing validate+dump path.
# ---------------------------------------------------------------------------

def test_skip_validation_off_byte_identical():
    """With skip_validation OFF (default), a route returning a dict goes
    through model_validate + dump_json exactly as before."""
    app = lancelot.App()

    @app.get("/normal", response_model=Item)
    def normal():
        return {"id": 3, "name": "z", "extra": "dropped-by-validate"}

    with TestClient(app) as c:
        r = c.get("/normal")
        assert r.status_code == 200
        # The validating path COERCES to the model and DROPS unknown keys.
        assert r.json() == {"id": 3, "name": "z"}
