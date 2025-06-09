from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from contextlib import contextmanager
from functools import lru_cache, partial
from inspect import signature
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Iterable,
    Type,
    Union,
    cast,
    get_type_hints,
    overload,
)
from weakref import WeakValueDictionary

import anyio
from anyio import BrokenResourceError, create_memory_object_stream
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from typing_extensions import Literal

from ._pycrdt import Doc as _Doc
from ._pycrdt import Subscription
from ._pycrdt import Transaction as _Transaction
from ._transaction import ReadTransaction, Transaction

if TYPE_CHECKING:
    from ._doc import Doc

try:
    import importlib.metadata as importlib_metadata
except ImportError:
    import importlib_metadata  # type: ignore[no-redef]

anyio_version = importlib_metadata.version("anyio")


base_types: dict[Any, type[BaseType | BaseDoc]] = {}
event_types: dict[Any, type[BaseEvent]] = {}
integrated_cache: WeakValueDictionary[Any, BaseType | BaseDoc] = WeakValueDictionary()
_do_cache = True



@contextmanager
def no_cache():
    """Decorator to disable caching for a function."""
    global _do_cache
    _do_cache = False
    try:
        yield
    finally:
        _do_cache = True

def forbid_read_transaction(txn: Transaction):
    if isinstance(txn, ReadTransaction):
        raise RuntimeError("Read-only transaction cannot be used to modify document structure")


def _iter_children(obj: Any) -> Iterable[tuple[int | str, Any]]:
    """Yield child key/value pairs for supported container types."""
    from ._array import Array
    from ._doc import Doc
    from ._map import Map
    from ._xml import XmlElement, XmlFragment

    if isinstance(obj, Doc):
        for k in obj.keys():
            yield k, obj[k]
    elif isinstance(obj, Map):
        for k in obj.keys():
            yield k, obj[k]
    elif isinstance(obj, Array):
        for i in range(len(obj)):
            yield i, obj[i]
    elif isinstance(obj, (XmlElement, XmlFragment)):
        children = obj.children
        for i in range(len(children)):
            yield i, children[i]


def _find_path(doc: "Doc", target: Any) -> list[int | str]:
    """Return the path to `target` within `doc`."""

    stack: list[tuple[list[int | str], Any]] = [([], doc)]
    while stack:
        path, obj = stack.pop()
        if obj is target:
            return path
        for key, child in _iter_children(obj):
            stack.append((path + [key], child))
    raise ValueError("Object not found in document")


def _get_by_path(doc: "Doc", path: list[int | str]) -> Any:
    """Return the object at `path` inside `doc`."""

    obj: Any = doc
    from ._array import Array
    from ._doc import Doc
    from ._map import Map
    from ._xml import XmlElement, XmlFragment
    for key in path:
        if isinstance(obj, Doc):
            assert isinstance(key, str), "Doc keys must be strings"
            obj = obj[key]
        elif isinstance(obj, Map):
            assert isinstance(key, str), "Map keys must be strings"
            obj = obj[key]
        elif isinstance(obj, Array):
            assert isinstance(key, int), "Array keys must be integers"
            obj = obj[key]
        elif isinstance(obj, (XmlElement, XmlFragment)):
            assert isinstance(key, int), "Xml items keys must be integers"
            obj = obj.children[key]
        else:
            raise TypeError(f"Cannot follow path segment {key!r} on {obj!r}")
    return obj


def _rebuild_obj(doc: "Doc", path: list[int | str]) -> Any:
    return _get_by_path(doc, path)


class BaseDoc:
    _doc: _Doc
    _twin_doc: BaseDoc | None
    _txn: Transaction | None
    _txn_lock: threading.Lock
    _txn_async_lock: anyio.Lock
    _allow_multithreading: bool
    _Model: Any
    _subscriptions: list[Subscription]
    _origins: dict[int, Any]

    def __init__(
        self,
        *,
        client_id: int | None = None,
        doc: _Doc | None = None,
        Model=None,
        allow_multithreading: bool = False,
        **data,
    ) -> None:
        super().__init__(**data)
        if doc is None:
            doc = _Doc(client_id)
        self._doc = doc
        if _do_cache:
            integrated_cache[("doc", self._doc.guid())] = self
        self._txn = None
        self._txn_lock = threading.Lock()
        self._txn_async_lock = anyio.Lock()
        self._Model = Model
        self._subscriptions = []
        self._origins = {}
        self._allow_multithreading = allow_multithreading

class BaseType(ABC):
    _doc: Doc | None
    _prelim: Any | None
    _integrated: Any
    _type_name: str
    _subscriptions: list[Subscription]

    def __init__(
        self,
        init: Any = None,
        *,
        _doc: Doc | None = None,
        _integrated: Any = None,
    ) -> None:
        self._type_name = self.__class__.__name__.lower()
        self._subscriptions = []
        self._send_streams: dict[bool, set[MemoryObjectSendStream[BaseEvent | list[BaseEvent]]]] = {
            False: set(),
            True: set(),
        }
        self._event_subscription: dict[bool, Subscription] = {}
        # private API
        if _integrated is not None:
            self._doc = _doc
            self._prelim = None
            self._integrated = _integrated
            return
        # public API
        self._doc = None
        self._prelim = init
        self._integrated = None

    @abstractmethod
    def to_py(self) -> Any: ...

    @abstractmethod
    def _get_or_insert(self, name: str, doc: Doc) -> Any: ...

    @abstractmethod
    def _init(self, value: Any | None) -> None: ...

    def _forbid_read_transaction(self, txn: Transaction):
        forbid_read_transaction(txn)

    def _integrate(self, doc: Doc, integrated: Any) -> Any:
        prelim = self._prelim
        self._doc = doc
        self._prelim = None
        self._integrated = integrated
        if _do_cache:
            cache_key = ("type", doc.guid, integrated.branch_id())
            integrated_cache[cache_key] = self
        return prelim

    def _do_and_integrate(self, action: str, value: BaseType, txn: _Transaction, *args) -> None:
        method = getattr(self._integrated, f"{action}_{value.type_name}_prelim")
        integrated = method(txn, *args)
        assert self._doc is not None
        prelim = value._integrate(self._doc, integrated)
        value._init(prelim)

    def _maybe_as_type_or_doc(self, obj: Any) -> Any:
        for k, v in base_types.items():
            if isinstance(obj, k):
                cache_key: Any
                res: BaseType | BaseDoc
                if issubclass(v, BaseDoc):
                    cache_key = ("doc", obj.guid())
                    cached = integrated_cache.get(cache_key, None)
                    if cached is not None:
                        return cached
                    # create a BaseDoc
                    res = v(doc=obj)
                else:
                    cache_key = ("type", self.doc.guid, obj.branch_id())
                    cached = integrated_cache.get(cache_key, None)
                    if cached is not None:
                        return cached
                    # create a BaseType
                    res = v(_doc=self.doc, _integrated=obj)
                if _do_cache:
                    integrated_cache[cache_key] = res
                return res
        # that was a primitive value, try to preserve integers
        if isinstance(obj, float) and obj.is_integer():
            return int(obj)
        return obj

    @property
    def integrated(self) -> Any:
        if self._integrated is None:
            raise RuntimeError("Not integrated in a document yet")
        return self._integrated

    @property
    def doc(self) -> Doc:
        """
        The document this shared type belongs to.

        Raises:
            RuntimeError: Not integrated in a document yet.
        """
        if self._doc is None:
            raise RuntimeError("Not integrated in a document yet")
        return self._doc

    @property
    def is_prelim(self) -> bool:
        return self._prelim is not None

    @property
    def is_integrated(self) -> bool:
        return self._integrated is not None

    @property
    def prelim(self) -> Any:
        return self._prelim

    @property
    def type_name(self) -> str:
        return self._type_name

    def observe(self, callback: Callable[[BaseEvent], None]) -> Subscription:
        _callback = partial(observe_callback, callback, self.doc)
        subscription = self.integrated.observe(_callback)
        self._subscriptions.append(subscription)
        return subscription

    def observe_deep(self, callback: Callable[[list[BaseEvent]], None]) -> Subscription:
        """
        Subscribes a callback for all events emitted by this and nested collaborative types.

        Args:
            callback: The callback to call with the list of events.
        """
        _callback = partial(observe_deep_callback, callback, self.doc)
        subscription = self.integrated.observe_deep(_callback)
        self._subscriptions.append(subscription)
        return subscription

    def unobserve(self, subscription: Subscription) -> None:
        """
        Unsubscribes to changes using the given subscription.

        Args:
            subscription: The subscription to unregister.
        """
        self._subscriptions.remove(subscription)
        subscription.drop()

    @overload
    def events(
        self,
        deep: Literal[False],
        max_buffer_size: float = float("inf"),
    ) -> MemoryObjectReceiveStream[BaseEvent]: ...

    @overload
    def events(
        self,
        deep: Literal[True],
        max_buffer_size: float = float("inf"),
    ) -> MemoryObjectReceiveStream[list[BaseEvent]]: ...

    def events(
        self,
        deep: bool = False,
        max_buffer_size: float = float("inf"),
    ):
        """
        Allows to asynchronously iterate over the shared type events, without using a callback.
        A buffer is used to store the events, allowing to iterate at a (temporarily) slower rate
        than they are produced.

        This method must be used with an async context manager and an async for-loop:

        ```py
        async def main():
            async with doc.events() as events:
                async for event in events:
                    update: bytes = event.update
                    ...
        ```

        Args:
            deep: Whether to iterate over the nested events.
            max_buffer_size: Maximum number of events that can be buffered.

        Returns:
            An async iterator over the shared type events.
        """
        observe = self.observe_deep if deep else self.observe
        if not self._send_streams[deep]:
            self._event_subscription[deep] = observe(partial(self._send_event, deep))
        if anyio_version > "4.0.0":
            send_stream, receive_stream = create_memory_object_stream[
                Union[BaseEvent, "list[BaseEvent]"]
            ](max_buffer_size=max_buffer_size)
        else:
            send_stream, receive_stream = create_memory_object_stream(
                max_buffer_size=max_buffer_size,
                item_type=Union[BaseEvent, "list[BaseEvent]"],
            )
        self._send_streams[deep].add(send_stream)
        return receive_stream

    def _send_event(self, deep: bool, event: BaseEvent | list[BaseEvent]):
        to_remove: list[MemoryObjectSendStream[BaseEvent | list[BaseEvent]]] = []
        send_streams = self._send_streams[deep]
        for send_stream in send_streams:
            try:
                send_stream.send_nowait(event)
            except BrokenResourceError:
                to_remove.append(send_stream)
        for send_stream in to_remove:
            send_stream.close()
            send_streams.remove(send_stream)
        if not send_streams:
            self.unobserve(self._event_subscription[deep])

    def __reduce__(self):
        if self._doc is None:
            return type(self), (self.to_py(),)
        path = _find_path(self.doc, self)
        return _rebuild_obj, (self.doc, path)


def observe_callback(
    callback: Callable[[], None] | Callable[[Any], None] | Callable[[Any, ReadTransaction], None],
    doc: Doc,
    event: Any,
):
    param_nb = count_parameters(callback)
    _event = event_types[type(event)](event, doc)
    with doc._read_transaction(event.transaction) as txn:
        params = (_event, txn)
        callback(*params[:param_nb])  # type: ignore[arg-type]


def observe_deep_callback(
    callback: Callable[[], None] | Callable[[Any], None] | Callable[[Any, ReadTransaction], None],
    doc: Doc,
    events: list[Any],
):
    param_nb = count_parameters(callback)
    for idx, event in enumerate(events):
        events[idx] = event_types[type(event)](event, doc)
    with doc._read_transaction(event.transaction) as txn:
        params = (events, txn)
        callback(*params[:param_nb])  # type: ignore[arg-type]


class BaseEvent:
    __slots__ = ()

    def __init__(self, event: Any, doc: Doc):
        slot: str
        for slot in self.__slots__:
            with no_cache():
                processed = process_event(getattr(event, slot), doc)
            setattr(self, slot, processed)

    def __str__(self):
        str_list = []
        slot: Any
        for slot in self.__slots__:
            val = str(getattr(self, slot))
            str_list.append(f"{slot}: {val}")
        ret = ", ".join(str_list)
        return "{" + ret + "}"


def process_event(value: Any, doc: Doc) -> Any:
    if isinstance(value, list):
        for idx, val in enumerate(value):
            value[idx] = process_event(val, doc)
    elif isinstance(value, dict):
        for key, val in value.items():
            value[key] = process_event(val, doc)
    else:
        val_type = type(value)
        if val_type in base_types:
            if val_type is _Doc:
                doc_type: type[BaseDoc] = cast(Type[BaseDoc], base_types[val_type])
                cache_key = ("doc", value.guid())
                cached = integrated_cache.get(cache_key, None)
                if cached is not None:
                    value = cached
                else:
                    value = doc_type(doc=value)
                    integrated_cache[cache_key] = value
            else:
                base_type = cast(Type[BaseType], base_types[val_type])
                with no_cache():
                    value = base_type(_doc=doc, _integrated=value)
                cache_key = ("type", doc.guid, value.integrated.branch_id())
                cached = integrated_cache.get(cache_key, None)
                if cached is not None:
                    value = cached
                else:
                    integrated_cache[cache_key] = value
    return value


@lru_cache(maxsize=1024)
def count_parameters(func: Callable) -> int:
    """Count the number of parameters in a callable"""
    return len(signature(func).parameters)


class Typed:
    _: Any

    def __init__(self) -> None:
        self.__dict__["annotations"] = {
            name: _type
            for name, _type in get_type_hints(type(self).mro()[0]).items()
            if name != "_"
        }

    if not TYPE_CHECKING:

        def __getattr__(self, key: str) -> Any:
            annotations = self.__dict__["annotations"]
            if key not in annotations:
                raise AttributeError(f'"{type(self).mro()[0]}" has no attribute "{key}"')
            expected_type = annotations[key]
            if hasattr(expected_type, "mro") and Typed in expected_type.mro():
                return expected_type(self._[key])
            return self._[key]

        def __setattr__(self, key: str, value: Any) -> None:
            if key == "_":
                self.__dict__["_"] = value
                return
            annotations = self.__dict__["annotations"]
            if key not in annotations:
                raise AttributeError(f'"{type(self).mro()[0]}" has no attribute "{key}"')
            expected_type = annotations[key]
            if hasattr(expected_type, "__origin__"):
                expected_type = expected_type.__origin__
            if hasattr(expected_type, "__args__"):
                expected_types = expected_type.__args__
            else:
                expected_types = (expected_type,)
            if type(value) not in expected_types:
                raise TypeError(
                    f'Incompatible types in assignment (expression has type "{expected_type}", '
                    f'variable has type "{type(value)}")'
                )
            if isinstance(value, Typed):
                value = value._
            self._[key] = value
