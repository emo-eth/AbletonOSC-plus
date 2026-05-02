from __future__ import annotations

from typing import Any, Dict, List


PRIMITIVE_TYPES = (type(None), bool, int, float, str)


def describe_object(obj: Any, include_private: bool = False, max_members: int = 500) -> Dict[str, Any]:
    members: List[Dict[str, Any]] = []
    for name in sorted(dir(obj)):
        if not include_private and name.startswith("_"):
            continue
        members.append(describe_member(obj, name))
        if len(members) >= max_members:
            break

    return {
        "class": "%s.%s" % (obj.__class__.__module__, obj.__class__.__name__),
        "repr": safe_repr(obj),
        "include_private": bool(include_private),
        "truncated": len(members) >= max_members,
        "members": members,
    }


def search_members(obj: Any, query: str, include_private: bool = False, max_members: int = 500) -> Dict[str, Any]:
    lowered = query.lower()
    described = describe_object(obj, include_private=include_private, max_members=max_members)
    matches = [
        member
        for member in described["members"]
        if lowered in member["name"].lower()
    ]
    return {
        "class": described["class"],
        "repr": described["repr"],
        "query": query,
        "include_private": bool(include_private),
        "matches": matches,
    }


def has_member(obj: Any, name: str) -> Dict[str, Any]:
    if not hasattr_static(obj, name):
        return {
            "name": name,
            "exists": False,
            "kind": "missing",
        }

    member = describe_member(obj, name)
    return {
        "name": name,
        "exists": True,
        "kind": member["kind"],
        "class": member.get("class"),
        "repr": member.get("repr"),
        "error": member.get("error"),
        "listener_for": member.get("listener_for"),
    }


def describe_member(obj: Any, name: str) -> Dict[str, Any]:
    entry: Dict[str, Any] = {"name": name}
    listener_for = listener_property_name(name)
    if listener_for is not None:
        entry["listener_for"] = listener_for

    try:
        value = getattr(obj, name)
    except Exception as exc:
        entry["kind"] = "error"
        entry["error"] = "%s: %s" % (exc.__class__.__name__, str(exc))
        return entry

    entry["kind"] = "callable" if callable(value) else "property"
    entry["class"] = "%s.%s" % (value.__class__.__module__, value.__class__.__name__)
    entry["repr"] = safe_repr(value)
    if isinstance(value, PRIMITIVE_TYPES):
        entry["value"] = value
    elif isinstance(value, (tuple, list)):
        entry["length"] = len(value)
    return entry


def hasattr_static(obj: Any, name: str) -> bool:
    try:
        names = dir(obj)
    except Exception:
        return False
    return name in names


def listener_property_name(name: str) -> str | None:
    if name.startswith("add_") and name.endswith("_listener"):
        return name[len("add_") : -len("_listener")]
    if name.startswith("remove_") and name.endswith("_listener"):
        return name[len("remove_") : -len("_listener")]
    return None


def safe_repr(value: Any, limit: int = 240) -> str:
    try:
        text = repr(value)
    except Exception as exc:
        text = "<repr failed: %s>" % exc
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text
