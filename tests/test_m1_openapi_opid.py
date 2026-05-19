from lancelot.routing import Route
from lancelot.openapi import operation_id, summary


def test_operation_id_and_summary():
    r = Route("GET", "/items/", lambda: None, name="read_items")
    assert operation_id(r) == "read_items_items__get"
    rp = Route("GET", "/path_examples/{item_id}", lambda: None, name="path_examples")
    assert operation_id(rp) == "path_examples_path_examples__item_id__get"
    assert summary(Route("GET", "/x", lambda: None, name="read_items")) == "Read Items"
