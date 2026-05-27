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
import importlib
import re
import struct
import time
import traceback
from typing import Any, Tuple

try:
    import Live  # type: ignore
except ImportError:  # pragma: no cover - only imports when loaded inside Live
    Live = None

from .handler import AbletonOSCHandler
from . import probe as _probe
from .workflow import rebuild_arrangement_from_clips

try:
    importlib.reload(_probe)
except Exception:
    pass

describe_object = _probe.describe_object
has_member = _probe.has_member
search_members = _probe.search_members


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
        self.osc_server.add_handler("/live/lom/batch", self._handle_batch)
        self.osc_server.add_handler("/live/lom/batch_struct", self._handle_batch_struct)
        self.osc_server.add_handler("/live/lom/start_listen", self._handle_start_listen)
        self.osc_server.add_handler("/live/lom/stop_listen", self._handle_stop_listen)
        self.osc_server.add_handler("/live/probe/describe", self._handle_probe_describe)
        self.osc_server.add_handler("/live/probe/search", self._handle_probe_search)
        self.osc_server.add_handler("/live/probe/has", self._handle_probe_has)
        self.osc_server.add_handler(
            "/live/workflow/rebuild_arrangement_from_clips",
            self._handle_workflow_rebuild_arrangement_from_clips,
        )
        self.osc_server.add_handler(
            "/live/workflow/probe_codecs",
            self._handle_workflow_probe_codecs,
        )
        self.osc_server.add_handler(
            "/live/workflow/export_arrangement_inventory",
            self._handle_workflow_export_arrangement_inventory,
        )
        self.osc_server.add_handler(
            "/live/workflow/ensure_tracks",
            self._handle_workflow_ensure_tracks,
        )

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

    def _handle_batch(self, params):
        started = time.time()
        try:
            payload = json.loads(params[0]) if params else {}
            ops = payload if isinstance(payload, list) else payload.get("ops", [])
            stop_on_error = bool(
                False if isinstance(payload, list) else payload.get("stopOnError", False)
            )
            if not isinstance(ops, list):
                raise ValueError("batch payload must contain an ops array")

            results = []
            for index, op in enumerate(ops):
                try:
                    results.append(self._run_batch_op(index, op))
                except Exception as exc:
                    result = {
                        "index": index,
                        "ok": False,
                        "op": op.get("op") if isinstance(op, dict) else None,
                        "path": op.get("path") if isinstance(op, dict) else None,
                        "error": str(exc),
                    }
                    results.append(result)
                    if stop_on_error:
                        break

            ok = all(result.get("ok") for result in results)
            return (
                json.dumps(
                    {
                        "ok": ok,
                        "elapsedMs": int(round((time.time() - started) * 1000)),
                        "results": results,
                    },
                    separators=(",", ":"),
                ),
            )
        except Exception as exc:
            self.logger.warning("lom/batch failed: %s", traceback.format_exc())
            return (
                json.dumps(
                    {"ok": False, "error": str(exc), "results": []},
                    separators=(",", ":"),
                ),
            )

    def _handle_batch_struct(self, params):
        started = time.time()
        try:
            if not params or not isinstance(params[0], (bytes, bytearray)):
                raise ValueError("batch_struct expects one OSC blob argument")
            decoded = _decode_struct_batch_request(bytes(params[0]))
            stop_on_error = bool(decoded.get("stopOnError", False))
            ops = decoded.get("ops", [])

            results = []
            for index, op in enumerate(ops):
                try:
                    results.append(self._run_batch_op(index, op))
                except Exception as exc:
                    result = {
                        "index": index,
                        "ok": False,
                        "op": op.get("op") if isinstance(op, dict) else None,
                        "path": op.get("path") if isinstance(op, dict) else None,
                        "error": str(exc),
                    }
                    results.append(result)
                    if stop_on_error:
                        break

            return (
                _encode_struct_batch_response(
                    {
                        "elapsedMs": int(round((time.time() - started) * 1000)),
                        "results": results,
                    }
                ),
            )
        except Exception as exc:
            self.logger.warning("lom/batch_struct failed: %s", traceback.format_exc())
            return (
                _encode_struct_batch_response(
                    {
                        "elapsedMs": int(round((time.time() - started) * 1000)),
                        "results": [
                            {
                                "index": 0,
                                "ok": False,
                                "error": str(exc),
                            }
                        ],
                    }
                ),
            )

    def _run_batch_op(self, index, op):
        if not isinstance(op, dict):
            raise ValueError("batch op %d must be an object" % index)

        kind = op.get("op")
        path = op.get("path")
        if kind not in ("get", "set", "call"):
            raise ValueError("batch op %d has unsupported op: %r" % (index, kind))
        if not isinstance(path, str) or not path:
            raise ValueError("batch op %d missing path" % index)

        if kind == "get":
            _parent, _attr, value = self._resolve_path(path)
            return {
                "index": index,
                "ok": True,
                "op": kind,
                "path": path,
                "value": _jsonable(value),
            }

        if kind == "set":
            value = self._decode_arg(op.get("value", None))
            parent, attr, _current = self._resolve_path(path)
            if attr is None:
                raise ValueError("cannot set: path does not end at a scalar attribute")
            setattr(parent, attr, value)
            return {"index": index, "ok": True, "op": kind, "path": path}

        args = op.get("args", [])
        if not isinstance(args, list):
            raise ValueError("batch call op %d args must be an array" % index)
        decoded_args = tuple(self._decode_arg(arg) for arg in args)
        parent, attr, method = self._resolve_path(path)
        decoded_args = self._prepare_call_args(parent, attr, decoded_args)
        value = method(*decoded_args) if callable(method) else None
        return {
            "index": index,
            "ok": True,
            "op": kind,
            "path": path,
            "value": _jsonable(value),
        }

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

    def _handle_probe_describe(self, params):
        if not params:
            return ()
        path = params[0]
        include_private = _as_bool(params[1]) if len(params) > 1 else False
        names_only = _as_bool(params[4]) if len(params) > 4 else False
        default_max = 50 if names_only else 20
        max_limit = 50 if names_only else 20
        max_members = _bounded_int(
            params[2] if len(params) > 2 else default_max,
            default_max,
            1,
            max_limit,
        )
        offset = _bounded_int(
            params[3] if len(params) > 3 else 0,
            0,
            0,
            1000000,
        )
        try:
            _parent, _attr, value = self._resolve_path(path)
            result = describe_object(
                value,
                include_private=include_private,
                max_members=max_members,
                offset=offset,
                names_only=names_only,
            )
            result["path"] = path
        except Exception as exc:
            self.logger.warning("probe/describe %s failed: %s", path, exc)
            result = {"path": path, "ok": False, "error": str(exc)}
        return (path, _ARG_JSON_PREFIX + json.dumps(_jsonable(result), separators=(",", ":")))

    def _handle_probe_search(self, params):
        if len(params) < 2:
            return ()
        path = params[0]
        query = params[1]
        include_private = bool(params[2]) if len(params) > 2 else False
        names_only = _as_bool(params[3]) if len(params) > 3 else False
        max_members = _bounded_int(
            params[4] if len(params) > 4 else 50,
            50,
            1,
            50,
        )
        try:
            _parent, _attr, value = self._resolve_path(path)
            result = search_members(
                value,
                query,
                include_private=include_private,
                max_members=max_members,
                names_only=names_only,
            )
            result["path"] = path
        except Exception as exc:
            self.logger.warning("probe/search %s %s failed: %s", path, query, exc)
            result = {"path": path, "query": query, "ok": False, "error": str(exc)}
        return (path, query, _ARG_JSON_PREFIX + json.dumps(_jsonable(result), separators=(",", ":")))

    def _handle_probe_has(self, params):
        if len(params) < 2:
            return ()
        path = params[0]
        name = params[1]
        try:
            _parent, _attr, value = self._resolve_path(path)
            result = has_member(value, name)
            result["path"] = path
        except Exception as exc:
            self.logger.warning("probe/has %s %s failed: %s", path, name, exc)
            result = {"path": path, "name": name, "ok": False, "error": str(exc)}
        return (path, name, _ARG_JSON_PREFIX + json.dumps(_jsonable(result), separators=(",", ":")))

    def _handle_workflow_rebuild_arrangement_from_clips(self, params):
        return rebuild_arrangement_from_clips(self, params)

    def _handle_workflow_ensure_tracks(self, params):
        from . import workflow

        return workflow.ensure_tracks(self, params)

    def _handle_workflow_probe_codecs(self, params):
        modules = (
            "google.protobuf",
            "msgpack",
            "cbor2",
            "pickle",
            "marshal",
            "struct",
            "array",
            "base64",
            "zlib",
            "json",
        )
        results = {}
        for module in modules:
            try:
                imported = importlib.import_module(module)
                version = getattr(imported, "__version__", None)
                results[module] = {"available": True, "version": version}
            except Exception as exc:
                results[module] = {
                    "available": False,
                    "error": exc.__class__.__name__,
                }

        return (
            json.dumps(
                {"ok": True, "modules": results},
                separators=(",", ":"),
            ),
        )

    def _handle_workflow_export_arrangement_inventory(self, params):
        from . import workflow

        return workflow.export_arrangement_inventory(self, params)

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


_STRUCT_REQUEST_MAGIC = b"LMB1"
_STRUCT_RESPONSE_MAGIC = b"LMR1"
_STRUCT_OP_GET = 1
_STRUCT_OP_SET = 2
_STRUCT_OP_CALL = 3
_STRUCT_TAG_NULL = 0
_STRUCT_TAG_FALSE = 1
_STRUCT_TAG_TRUE = 2
_STRUCT_TAG_INT32 = 3
_STRUCT_TAG_FLOAT64 = 4
_STRUCT_TAG_STRING_REF = 5
_STRUCT_TAG_ARRAY = 6
_STRUCT_TAG_OBJECT = 7


def _decode_struct_batch_request(blob):
    reader = _StructReader(blob)
    reader.expect(_STRUCT_REQUEST_MAGIC)
    stop_on_error = reader.u8() != 0
    strings = _read_struct_string_table(reader)
    op_count = reader.u16()
    ops = []
    for _index in range(op_count):
        code = reader.u8()
        path = strings[reader.u16()]
        if code == _STRUCT_OP_GET:
            ops.append({"op": "get", "path": path})
        elif code == _STRUCT_OP_SET:
            ops.append(
                {
                    "op": "set",
                    "path": path,
                    "value": _read_struct_value(reader, strings),
                }
            )
        elif code == _STRUCT_OP_CALL:
            arg_count = reader.u16()
            args = [_read_struct_value(reader, strings) for _ in range(arg_count)]
            ops.append({"op": "call", "path": path, "args": args})
        else:
            raise ValueError("unsupported struct batch op code: %r" % code)
    return {"stopOnError": stop_on_error, "ops": ops}


def _encode_struct_batch_response(response):
    strings = _StructStringTable()
    for result in response.get("results", []):
        if result.get("ok"):
            _collect_struct_strings(_jsonable(result.get("value", None)), strings)
        else:
            _collect_struct_strings(str(result.get("error", "")), strings)

    writer = _StructWriter()
    writer.raw(_STRUCT_RESPONSE_MAGIC)
    writer.u32(max(0, int(response.get("elapsedMs", 0))))
    _write_struct_string_table(writer, strings.values)
    results = response.get("results", [])
    writer.u16(len(results))
    for result in results:
        writer.u16(int(result.get("index", 0)))
        ok = bool(result.get("ok"))
        writer.u8(1 if ok else 0)
        if ok:
            _write_struct_value(writer, _jsonable(result.get("value", None)), strings)
        else:
            _write_struct_value(writer, str(result.get("error", "")), strings)
    return writer.bytes()


def _collect_struct_strings(value, table):
    if isinstance(value, str):
        table.intern(value)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect_struct_strings(item, table)
    elif isinstance(value, dict):
        for key, item in value.items():
            table.intern(str(key))
            _collect_struct_strings(item, table)


def _write_struct_string_table(writer, strings):
    writer.u16(len(strings))
    for value in strings:
        writer.string(value)


def _read_struct_string_table(reader):
    return [reader.string() for _ in range(reader.u16())]


def _write_struct_value(writer, value, strings):
    if value is None:
        writer.u8(_STRUCT_TAG_NULL)
    elif value is False:
        writer.u8(_STRUCT_TAG_FALSE)
    elif value is True:
        writer.u8(_STRUCT_TAG_TRUE)
    elif isinstance(value, int) and -2147483648 <= value <= 2147483647:
        writer.u8(_STRUCT_TAG_INT32)
        writer.i32(value)
    elif isinstance(value, (int, float)):
        writer.u8(_STRUCT_TAG_FLOAT64)
        writer.f64(float(value))
    elif isinstance(value, str):
        writer.u8(_STRUCT_TAG_STRING_REF)
        writer.u16(strings.index(value))
    elif isinstance(value, (list, tuple)):
        writer.u8(_STRUCT_TAG_ARRAY)
        writer.u16(len(value))
        for item in value:
            _write_struct_value(writer, item, strings)
    elif isinstance(value, dict):
        items = list(value.items())
        writer.u8(_STRUCT_TAG_OBJECT)
        writer.u16(len(items))
        for key, item in items:
            writer.u16(strings.index(str(key)))
            _write_struct_value(writer, item, strings)
    else:
        _write_struct_value(writer, repr(value), strings)


def _read_struct_value(reader, strings):
    tag = reader.u8()
    if tag == _STRUCT_TAG_NULL:
        return None
    if tag == _STRUCT_TAG_FALSE:
        return False
    if tag == _STRUCT_TAG_TRUE:
        return True
    if tag == _STRUCT_TAG_INT32:
        return reader.i32()
    if tag == _STRUCT_TAG_FLOAT64:
        return reader.f64()
    if tag == _STRUCT_TAG_STRING_REF:
        return strings[reader.u16()]
    if tag == _STRUCT_TAG_ARRAY:
        return [_read_struct_value(reader, strings) for _ in range(reader.u16())]
    if tag == _STRUCT_TAG_OBJECT:
        result = {}
        for _ in range(reader.u16()):
            result[strings[reader.u16()]] = _read_struct_value(reader, strings)
        return result
    raise ValueError("unsupported struct batch value tag: %r" % tag)


class _StructStringTable:
    def __init__(self):
        self.values = []
        self._index = {}

    def intern(self, value):
        value = str(value)
        if value in self._index:
            return self._index[value]
        index = len(self.values)
        self.values.append(value)
        self._index[value] = index
        return index

    def index(self, value):
        return self._index[str(value)]


class _StructWriter:
    def __init__(self):
        self._chunks = []

    def raw(self, value):
        self._chunks.append(bytes(value))

    def u8(self, value):
        self._chunks.append(struct.pack(">B", int(value)))

    def u16(self, value):
        self._chunks.append(struct.pack(">H", int(value)))

    def u32(self, value):
        self._chunks.append(struct.pack(">I", int(value)))

    def i32(self, value):
        self._chunks.append(struct.pack(">i", int(value)))

    def f64(self, value):
        self._chunks.append(struct.pack(">d", float(value)))

    def string(self, value):
        encoded = str(value).encode("utf-8")
        self.u32(len(encoded))
        self._chunks.append(encoded)

    def bytes(self):
        return b"".join(self._chunks)


class _StructReader:
    def __init__(self, data):
        self._data = data
        self._offset = 0

    def expect(self, expected):
        actual = self._read(len(expected))
        if actual != expected:
            raise ValueError("invalid struct batch magic: %r" % actual)

    def u8(self):
        return struct.unpack(">B", self._read(1))[0]

    def u16(self):
        return struct.unpack(">H", self._read(2))[0]

    def u32(self):
        return struct.unpack(">I", self._read(4))[0]

    def i32(self):
        return struct.unpack(">i", self._read(4))[0]

    def f64(self):
        return struct.unpack(">d", self._read(8))[0]

    def string(self):
        size = self.u32()
        return self._read(size).decode("utf-8")

    def _read(self, size):
        end = self._offset + size
        if end > len(self._data):
            raise ValueError("truncated struct batch payload")
        chunk = self._data[self._offset : end]
        self._offset = end
        return chunk


def _as_bool(value):
    if isinstance(value, str):
        return value.lower() in ("1", "true", "yes", "on")
    return bool(value)


def _bounded_int(value, default, minimum, maximum):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))
