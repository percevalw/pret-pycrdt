import pytest
from pycrdt import Doc, Map, Array

pytestmark = pytest.mark.anyio


def test_map_item_identity():
    doc = Doc()
    doc["map"] = m = Map()
    m["child"] = Map()
    assert m["child"] is m["child"]


def test_array_item_identity():
    doc = Doc()
    doc["arr"] = arr = Array()
    arr.append(Map())
    assert arr[0] is arr[0]
