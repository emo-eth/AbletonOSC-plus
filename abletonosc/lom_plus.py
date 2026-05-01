"""Full-coverage LOM handler.

Exposes a small generic OSC surface that reaches every property in the Live
Object Model, not just the ones AbletonOSC has bespoke handlers for. Backs
the @springfield/ableton-lom-mcp typed MCP server.

OSC API:
    /live/lom/get       <dotted path>               -> (path, value, ...)
    /live/lom/set       <dotted path> <value>       -> fire-and-forget
    /live/lom/call      <dotted path> [args...]     -> (path, result, ...)
    /live/lom/start_listen <dotted path>            -> sends /live/lom/get on change
    /live/lom/stop_listen  <dotted path>

Special argument strings for /live/lom/set and /live/lom/call:
    @lom:<dotted path>   resolves the path and passes the Live object itself
    @json:<json>         decodes JSON and passes the resulting dict/list

Path syntax:
    live_set.tempo
    live_set.tracks[0].name
    live_set.tracks[0].clip_slots[3].clip.notes
    live_app.view.focused_document_view

Roots: live_set, live_app, this_device, control_surfaces.
Index into collections with [n] (zero-based) or ["key"] for named lookups.
"""

from __future__ import annotations

import json
import re
import traceback
from typing import Any, Tuple

try:
    import Live  # type: ignore
except ImportError:  # pragma: no cover - only imports when loaded inside Live
    Live = None

from .handler import AbletonOSCHandler


_SEGMENT_RE = re.compile(
    r'^(?P<name>[A-Za-z_][A-Za-z_0-9]*)(?:\[(?P<index>\d+)\]|\["(?P<key>[^"]+)"\])?$'
)
_ARG_LOM_REF_PREFIX = "@lom:"
_ARG_JSON_PREFIX = "@json:"


class LomPlusHandler(AbletonOSCHandler):
    """Generic /live/lom/* handler covering the entire Live Object Model."""

    def __init__(self, manager):
        super().__init__(manager)
        self.class_identifier = "lom"
        self._listeners = {}

    def init_api(self):
        self.osc_server.add_handler("/live/lom/get", self._handle_get)
        self.osc_server.add_handler("/live/lom/set", self._handle_set)
        self.osc_server.add_handler("/live/lom/call", self._handle_call)
        self.osc_server.add_handler("/live/lom/start_listen", self._handle_start_listen)
        self.osc_server.add_handler("/live/lom/stop_listen", self._handle_stop_listen)

    # --- path resolution ---------------------------------------------------

    def _resolve_root(self, name: str):
        song = self.song
        if name == "live_set":
            return song
        if name == "live_app":
            if Live is None:
                raise RuntimeError("Live module unavailable outside Ableton")
            return Live.Application.get_application()
        if name == "this_device":
            # The remote script doesn't have a persistent "this_device"; M4L does.
            # Fall back to first device of first track for developer introspection.
            if not song.tracks:
                return None
            if not song.tracks[0].devices:
                return None
            return song.tracks[0].devices[0]
        if name == "control_surfaces":
            if Live is None:
                raise RuntimeError("Live module unavailable outside Ableton")
            return list(Live.Application.get_application().control_surfaces)
        raise ValueError("unknown LOM root: %s" % name)

    def _resolve_path(self, dotted: str):
        """Walk a dotted path, returning (parent, terminal_attr_or_None, value)."""
        parts = dotted.split(".")
        if not parts:
            raise ValueError("empty path")
        root_part = parts[0]
        root_m = _SEGMENT_RE.match(root_part)
        if not root_m:
            raise ValueError("invalid root segment: %r" % root_part)
        obj = self._resolve_root(root_m.group("name"))
        if obj is None:
            raise ValueError("root %r resolved to None" % root_part)
        if root_m.group("index") is not None:
            obj = obj[int(root_m.group("index"))]
        elif root_m.group("key") is not None:
            obj = getattr(obj, root_m.group("key"))

        terminal_attr = None
        for i, part in enumerate(parts[1:]):
            m = _SEGMENT_RE.match(part)
            if not m:
                raise ValueError("invalid path segment: %r" % part)
            name = m.group("name")
            is_last = i == len(parts) - 2
            if is_last and m.group("index") is None and m.group("key") is None:
                terminal_attr = name
                if name == "length" and not hasattr(obj, name):
                    return obj, terminal_attr, len(obj)
                return obj, terminal_attr, getattr(obj, name)
            obj = getattr(obj, name)
            if m.group("index") is not None:
                obj = obj[int(m.group("index"))]
            elif m.group("key") is not None:
                obj = obj[m.group("key")]

        return obj, terminal_attr, obj

    # --- OSC handlers ------------------------------------------------------

    def _handle_get(self, params):
        if not params:
            return ()
        path = params[0]
        try:
            _parent, _attr, value = self._resolve_path(path)
        except Exception as exc:
            self.logger.warning("lom/get %s failed: %s", path, exc)
            return (path, None, str(exc))
        rv = _to_osc(value)
        if isinstance(rv, tuple):
            return (path,) + rv
        return (path, rv)

    def _handle_set(self, params):
        if not params:
            return
        path = params[0]
        value = self._decode_arg(params[1]) if len(params) > 1 else None
        try:
            parent, attr, _current = self._resolve_path(path)
            if attr is None:
                raise ValueError("cannot set: path does not end at a scalar attribute")
            setattr(parent, attr, value)
        except Exception as exc:
            self.logger.warning("lom/set %s = %s failed: %s", path, value, exc)

    def _handle_call(self, params):
        if not params:
            return ()
        path = params[0]
        args = tuple(self._decode_arg(arg) for arg in params[1:])
        try:
            parent, attr, method = self._resolve_path(path)
            args = self._prepare_call_args(parent, attr, args)
            rv = method(*args) if callable(method) else None
        except Exception as exc:
            self.logger.warning("lom/call %s%r failed: %s", path, args, exc)
            return (path, None, str(exc))
        rv = _to_osc(rv)
        if isinstance(rv, tuple):
            return (path,) + rv
        return (path, rv)

    def _handle_start_listen(self, params):
        if not params:
            return
        path = params[0]
        if path in self._listeners:
            self._handle_stop_listen((path,))
        try:
            parent, attr, _value = self._resolve_path(path)
            if attr is None:
                raise ValueError("cannot listen: path is not a scalar")
            add_fn = getattr(parent, "add_%s_listener" % attr, None)
            remove_fn = getattr(parent, "remove_%s_listener" % attr, None)
            if add_fn is None or remove_fn is None:
                raise ValueError("no listener for %s" % attr)

            def notify():
                try:
                    value = _to_osc(getattr(parent, attr))
                    if isinstance(value, tuple):
                        self.osc_server.send("/live/lom/get", (path,) + value)
                    else:
                        self.osc_server.send("/live/lom/get", (path, value))
                except Exception:
                    self.logger.warning(
                        "listener %s failed: %s", path, traceback.format_exc()
                    )

            add_fn(notify)
            self._listeners[path] = (parent, attr, remove_fn, notify)
            notify()
        except Exception as exc:
            self.logger.warning("lom/start_listen %s failed: %s", path, exc)

    def _handle_stop_listen(self, params):
        if not params:
            return
        path = params[0]
        entry = self._listeners.pop(path, None)
        if not entry:
            return
        _parent, _attr, remove_fn, notify = entry
        try:
            remove_fn(notify)
        except Exception:
            pass

    def clear_api(self):
        super().clear_api()
        for path in list(self._listeners.keys()):
            self._handle_stop_listen((path,))

    def _decode_arg(self, value):
        if isinstance(value, str):
            if value.startswith(_ARG_LOM_REF_PREFIX):
                ref_path = value[len(_ARG_LOM_REF_PREFIX) :]
                _parent, _attr, ref_value = self._resolve_path(ref_path)
                return ref_value
            if value.startswith(_ARG_JSON_PREFIX):
                return json.loads(value[len(_ARG_JSON_PREFIX) :])
        return value

    # --- Live API argument compatibility -----------------------------------

    def _prepare_call_args(self, parent, attr, args):
        """Bridge Max-style JSON payloads to Live's Python API signatures."""
        if Live is None or not attr or not args:
            return args

        if attr == "add_new_notes":
            return (tuple(self._note_specifications(args[0])),) + args[1:]

        if attr == "apply_note_modifications":
            return (self._note_modifications(parent, args[0]),) + args[1:]

        if attr == "replace_selected_notes":
            return (
                tuple(
                    self._legacy_note_tuple(note)
                    for note in self._notes_arg(args[0])
                ),
            )

        if attr in (
            "get_notes_by_id",
            "remove_notes_by_id",
            "select_notes_by_id",
        ):
            return (tuple(self._note_ids_arg(args[0])),) + args[1:]

        if attr == "duplicate_notes_by_id":
            payload = args[0]
            if isinstance(payload, dict):
                note_ids = tuple(payload.get("note_ids", ()))
                destination = payload.get("destination_time", None)
                transposition = payload.get("transposition_amount", 0)
                return (note_ids, destination, transposition) + args[1:]
            return (tuple(self._note_ids_arg(payload)),) + args[1:]

        if attr == "get_notes_extended" and isinstance(args[0], dict):
            payload = args[0]
            return (
                payload.get("from_pitch", 0),
                payload.get("pitch_span", 128),
                payload.get("from_time", -8192),
                payload.get("time_span", 16384),
            ) + args[1:]

        if attr in (
            "get_all_notes_extended",
            "get_selected_notes_extended",
        ) and isinstance(args[0], dict):
            return args[1:]

        return args

    def _notes_arg(self, value):
        if isinstance(value, dict):
            return value.get("notes", ())
        return value or ()

    def _note_ids_arg(self, value):
        if isinstance(value, dict):
            return value.get("note_ids", ())
        return value or ()

    def _note_specifications(self, value):
        for note in self._notes_arg(value):
            yield self._note_specification(note)

    def _note_specification(self, note):
        if not isinstance(note, dict):
            return note

        kwargs = self._note_kwargs(note, include_note_id=False)
        try:
            return Live.Clip.MidiNoteSpecification(**kwargs)
        except TypeError:
            required = {
                key: kwargs[key]
                for key in ("start_time", "duration", "pitch", "velocity", "mute")
                if key in kwargs
            }
            spec = Live.Clip.MidiNoteSpecification(**required)
            for key, value in kwargs.items():
                if key in required:
                    continue
                try:
                    setattr(spec, key, value)
                except Exception:
                    pass
            return spec

    def _note_modifications(self, clip, value):
        note_dicts = [
            note for note in self._notes_arg(value) if isinstance(note, dict)
        ]
        if len(note_dicts) == 0:
            return self._notes_arg(value)

        note_ids = tuple(
            note["note_id"] for note in note_dicts if "note_id" in note
        )
        existing = clip.get_notes_by_id(note_ids)
        by_id = {note.note_id: note for note in existing}

        for payload in note_dicts:
            note = by_id.get(payload.get("note_id"))
            if note is None:
                continue
            for key, value in self._note_kwargs(payload, include_note_id=False).items():
                try:
                    setattr(note, key, value)
                except Exception:
                    pass
        return existing

    def _note_kwargs(self, note, include_note_id):
        fields = (
            "note_id",
            "pitch",
            "start_time",
            "duration",
            "velocity",
            "mute",
            "probability",
            "velocity_deviation",
            "release_velocity",
        )
        result = {}
        for field in fields:
            if field == "note_id" and not include_note_id:
                continue
            if field in note:
                result[field] = note[field]
        return result

    def _legacy_note_tuple(self, note):
        if not isinstance(note, dict):
            return tuple(note)
        return (
            note.get("pitch", 60),
            note.get("start_time", 0),
            note.get("duration", 1),
            note.get("velocity", 100),
            note.get("mute", False),
        )


def _to_osc(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return _ARG_JSON_PREFIX + json.dumps(_jsonable(value), separators=(",", ":"))
    to_json = getattr(value, "to_json", None)
    if callable(to_json):
        return _ARG_JSON_PREFIX + json.dumps(
            _jsonable(to_json()), separators=(",", ":")
        )
    if isinstance(value, (list, tuple)):
        return tuple(_to_osc(v) for v in value)
    try:
        return tuple(_to_osc(v) for v in value)
    except TypeError:
        return repr(value)


def _jsonable(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    to_json = getattr(value, "to_json", None)
    if callable(to_json):
        return _jsonable(to_json())
    try:
        return [_jsonable(v) for v in value]
    except TypeError:
        return repr(value)
