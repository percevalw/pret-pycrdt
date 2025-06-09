import pickle

import pytest
from pycrdt import Array, Doc, Map

pytestmark = pytest.mark.anyio


def test_pickle_identity():
    doc = Doc()
    doc["map"] = m = Map()
    arr = Array([1, 2, 3])
    m["child"] = arr

    data = pickle.dumps((doc["map"], arr))
    unpickled_m, unpickled_a = pickle.loads(data)

    assert unpickled_a is unpickled_m["child"]
    assert unpickled_m.to_py() == {"child": [1, 2, 3]}


def test_pickle_doc_and_children():
    doc = Doc()
    doc["map"] = m = Map()
    m["nums"] = Array([4, 5])

    data = pickle.dumps(doc)
    new_doc = pickle.loads(data)

    assert new_doc["map"]["nums"].to_py() == [4, 5]
