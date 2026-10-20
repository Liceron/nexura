"""Every SDK error survives pickling (multiprocessing, task queues, process pools)."""

from __future__ import annotations

import importlib
import inspect
import pickle
import pkgutil
from typing import Any

import nexura_agents
import pytest
from nexura_agents.errors import NexuraError


def _all_error_classes() -> list[type[NexuraError]]:
    for module in pkgutil.walk_packages(nexura_agents.__path__, "nexura_agents."):
        try:
            importlib.import_module(module.name)
        except ImportError:  # optional extras that are not installed
            continue

    def walk(cls: type) -> list[type]:
        found = []
        for sub in cls.__subclasses__():
            found += [sub, *walk(sub)]
        return found

    classes = {c for c in walk(NexuraError) if c.__module__.startswith("nexura_agents")}
    return sorted(classes, key=lambda c: f"{c.__module__}.{c.__qualname__}")


def _dummy(annotation: Any) -> Any:
    text = str(annotation)
    if "BaseException" in text:
        return ValueError("cause")
    if "dict" in text:
        return {"k": "v"}
    if "list" in text:
        return ["a"]
    if "float" in text:
        return 1.5
    if "int" in text:
        return 2
    return "value"


def _build(cls: type[NexuraError]) -> NexuraError:
    kwargs: dict[str, Any] = {}
    for name, param in inspect.signature(cls.__init__).parameters.items():
        if name == "self" or param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        if param.default is param.empty:
            kwargs[name] = _dummy(param.annotation)
    return cls(**kwargs)


ERROR_CLASSES = _all_error_classes()


def test_the_error_hierarchy_was_discovered() -> None:
    names = {c.__name__ for c in ERROR_CLASSES}
    assert {"UsageLimitExceededError", "MaxTurnsExceededError", "ToolExecutionError"} <= names
    assert len(ERROR_CLASSES) > 50


@pytest.mark.parametrize("cls", ERROR_CLASSES, ids=lambda c: c.__name__)
def test_error_round_trips_through_pickle(cls: type[NexuraError]) -> None:
    err = _build(cls)
    copy = pickle.loads(pickle.dumps(err))
    assert type(copy) is cls
    assert str(copy) == str(err)
    assert copy.args == err.args
    assert set(vars(copy)) == set(vars(err))
    for key, value in vars(err).items():
        if isinstance(value, BaseException):
            assert repr(vars(copy)[key]) == repr(value)
        else:
            assert vars(copy)[key] == value, key


def test_cause_is_kept() -> None:
    from nexura_agents.errors import ToolExecutionError

    err = ToolExecutionError("lookup", original_error=KeyError("missing"))
    copy = pickle.loads(pickle.dumps(err))
    assert isinstance(copy.__cause__, KeyError)
    assert copy.tool_name == "lookup"


def test_unpicklable_cause_is_replaced_by_a_readable_stand_in() -> None:
    import threading

    from nexura_agents.errors import ToolExecutionError

    class CauseWithLock(Exception):
        def __init__(self, message: str) -> None:
            super().__init__(message)
            self.lock = threading.Lock()

    err = ToolExecutionError("lookup", original_error=CauseWithLock("db down"))
    copy = pickle.loads(pickle.dumps(err))
    assert copy.tool_name == "lookup"
    assert str(copy) == str(err)
    assert isinstance(copy.original_error, RuntimeError)
    assert "CauseWithLock: db down" in str(copy.original_error)
