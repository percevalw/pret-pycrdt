import pytest
from pycrdt import Array, Doc, Map

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


def test_multi_docs():
    doc1 = Doc()
    doc2 = Doc()
    doc1["map"] = m1 = Map()
    doc2["map"] = m2 = Map()
    m1["child"] = Map()
    m2["child"] = Map()
    assert doc1["map"] is doc1["map"]
    assert doc2["map"] is doc2["map"]
    assert doc1["map"] is not doc2["map"]
    assert doc1["map"]["child"] is doc1["map"]["child"]
    assert doc2["map"]["child"] is doc2["map"]["child"]
    assert doc1["map"]["child"] is not doc2["map"]["child"]


def test_event():
    doc = Doc()
    doc["map"] = m = Map()
    m["child"] = Array()

    was_same = False
    def on_change(events):
        nonlocal was_same
        print("event target id", m["child"].integrated.branch_id())
        was_same = events[0].target is m["child"]

    print("child id", m["child"].integrated.branch_id())
    m.observe_deep(on_change)
    m["child"].append(3)
    assert was_same, "Event target should be the same object as m['child']"
