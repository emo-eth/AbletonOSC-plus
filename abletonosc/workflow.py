"""Workflow batch operations.

These handlers intentionally keep creative/planning logic outside Live. The
client sends a fully planned operation list; Live validates object names and
executes the edit locally to avoid hundreds of OSC round trips.
"""

from __future__ import annotations

import json
import importlib
import re
import struct
import time
import traceback

from .handler import AbletonOSCHandler


_ARRANGEMENT_CLIP_RE = re.compile(
    r"^live_set\.tracks\[(?P<track>\d+)\]\.arrangement_clips\[(?P<clip>\d+)\]$"
)
_WORKFLOW_PAYLOAD_MAGIC = b"WFP1"


class _ValueTag:
    NULL = 0
    FALSE = 1
    TRUE = 2
    INT32 = 3
    FLOAT64 = 4
    STRING_REF = 5
    ARRAY = 6
    OBJECT = 7


class WorkflowHandler(AbletonOSCHandler):
    def __init__(self, manager):
        super().__init__(manager)
        self.class_identifier = "workflow"

    def init_api(self):
        self.osc_server.add_handler(
            "/live/workflow/rebuild_arrangement_from_clips",
            self._handle_rebuild_arrangement_from_clips,
        )
        self.osc_server.add_handler(
            "/live/workflow/probe_codecs",
            self._handle_probe_codecs,
        )
        self.osc_server.add_handler(
            "/live/workflow/export_arrangement_inventory",
            self._handle_export_arrangement_inventory,
        )
        self.osc_server.add_handler(
            "/live/workflow/ensure_tracks",
            self._handle_ensure_tracks,
        )

    def _handle_rebuild_arrangement_from_clips(self, params):
        return rebuild_arrangement_from_clips(self, params)

    def _handle_probe_codecs(self, _params):
        return probe_codecs()

    def _handle_export_arrangement_inventory(self, params):
        return export_arrangement_inventory(self, params)

    def _handle_ensure_tracks(self, params):
        return ensure_tracks(self, params)

    def _validate_clear_specs(self, specs):
        tracks = []
        seen = set()
        for spec in specs:
            track_index = _required_int(spec, "track")
            if track_index in seen:
                continue
            track = self._track(track_index)
            expected_name = spec.get("expectedName")
            if expected_name is not None and track.name != expected_name:
                raise ValueError(
                    "clear track %d name mismatch: expected %r, got %r"
                    % (track_index, expected_name, track.name)
                )
            tracks.append(track)
            seen.add(track_index)
        return tracks

    def _validate_copy_specs(self, specs):
        ops = []
        for i, spec in enumerate(specs):
            source_clip = self._arrangement_clip_from_path(spec.get("source"))
            expected_clip_name = spec.get("expectedClipName")
            if expected_clip_name is not None and source_clip.name != expected_clip_name:
                raise ValueError(
                    "copy %d source clip name mismatch: expected %r, got %r"
                    % (i, expected_clip_name, source_clip.name)
                )

            destination_track_index = _required_int(spec, "destinationTrack")
            destination_track = self._track(destination_track_index)
            destination_track_name = spec.get("destinationTrackName")
            if (
                destination_track_name is not None
                and destination_track.name != destination_track_name
            ):
                raise ValueError(
                    "copy %d destination track name mismatch: expected %r, got %r"
                    % (i, destination_track_name, destination_track.name)
                )

            destination_time = float(spec.get("destinationTime", 0))
            if destination_time < 0:
                raise ValueError("copy %d destinationTime must be >= 0" % i)
            ops.append(
                {
                    "source_clip": source_clip,
                    "destination_track": destination_track,
                    "destination_time": destination_time,
                }
            )
        return ops

    def _track(self, index):
        if index < 0 or index >= len(self.song.tracks):
            raise ValueError("track index out of range: %d" % index)
        return self.song.tracks[index]

    def _arrangement_clip_from_path(self, path):
        if not isinstance(path, str):
            raise ValueError("source must be an arrangement clip LOM path")
        match = _ARRANGEMENT_CLIP_RE.match(path)
        if not match:
            raise ValueError("unsupported source path: %r" % path)
        track = self._track(int(match.group("track")))
        clip_index = int(match.group("clip"))
        clips = track.arrangement_clips
        if clip_index < 0 or clip_index >= len(clips):
            raise ValueError("arrangement clip index out of range: %r" % path)
        return clips[clip_index]


def _required_int(spec, key):
    if key not in spec:
        raise ValueError("missing required field %r" % key)
    return int(spec[key])


def _required_string(spec, key):
    value = spec.get(key)
    if not isinstance(value, str) or value == "":
        raise ValueError("missing required string field %r" % key)
    return value


def _error(message):
    return json.dumps({"ok": False, "error": message}, separators=(",", ":"))


def _decode_workflow_payload_arg(value):
    if isinstance(value, (bytes, bytearray)):
        return _decode_struct_workflow_payload(bytes(value))
    if isinstance(value, str):
        return json.loads(value)
    raise ValueError("workflow payload must be a JSON string or OSC blob")


def _decode_struct_workflow_payload(blob):
    reader = _StructReader(blob, _WORKFLOW_PAYLOAD_MAGIC)
    strings = _read_struct_string_table(reader)
    return _read_struct_value(reader, strings)


def _read_struct_string_table(reader):
    return [reader.string() for _ in range(reader.u16())]


def _read_struct_value(reader, strings):
    tag = reader.u8()
    if tag == _ValueTag.NULL:
        return None
    if tag == _ValueTag.FALSE:
        return False
    if tag == _ValueTag.TRUE:
        return True
    if tag == _ValueTag.INT32:
        return reader.i32()
    if tag == _ValueTag.FLOAT64:
        return reader.f64()
    if tag == _ValueTag.STRING_REF:
        return strings[reader.u16()]
    if tag == _ValueTag.ARRAY:
        return [_read_struct_value(reader, strings) for _ in range(reader.u16())]
    if tag == _ValueTag.OBJECT:
        result = {}
        for _ in range(reader.u16()):
            result[strings[reader.u16()]] = _read_struct_value(reader, strings)
        return result
    raise ValueError("unsupported workflow payload value tag: %r" % tag)


class _StructReader:
    def __init__(self, data, magic):
        self.data = data
        self.offset = 0
        actual = self._read(len(magic))
        if actual != magic:
            raise ValueError("invalid workflow payload magic: %r" % actual)

    def u8(self):
        return struct.unpack(">B", self._read(1))[0]

    def u16(self):
        return struct.unpack(">H", self._read(2))[0]

    def i32(self):
        return struct.unpack(">i", self._read(4))[0]

    def f64(self):
        return struct.unpack(">d", self._read(8))[0]

    def string(self):
        size = self.u16()
        return self._read(size).decode("utf-8")

    def _read(self, size):
        end = self.offset + size
        if end > len(self.data):
            raise ValueError("truncated workflow payload")
        chunk = self.data[self.offset : end]
        self.offset = end
        return chunk


def _same_name(left, right):
    return str(left).lower() == str(right).lower()


def _find_track_name(tracks, name, start, end):
    end = min(end, len(tracks))
    for index in range(start, end):
        if _same_name(tracks[index].name, name):
            return index
    return None


def _looks_like_episode_header(name):
    return re.match(r"^ep\d", str(name), re.I) is not None


def _find_insert_index(tracks, header_index, scan_end, insert_before_name):
    start = header_index + 1
    end = min(scan_end, len(tracks))
    if insert_before_name:
        found = _find_track_name(tracks, insert_before_name, start, end)
        if found is not None:
            return found
    for index in range(start, end):
        if _looks_like_episode_header(tracks[index].name):
            return index
    return end


def _existing_named_tracks(tracks, names, start, end):
    wanted = set(names)
    rows = []
    for index in range(start, min(end, len(tracks))):
        name = tracks[index].name
        if name in wanted:
            rows.append({"track": index, "name": name})
    return rows


def _apply_input_routing(track, input_routing_type, input_routing_channel):
    try:
        if input_routing_type is not None:
            _set_routing_display_name(
                track,
                "input_routing_type",
                "available_input_routing_types",
                input_routing_type,
            )
        if input_routing_channel is not None:
            _set_routing_display_name(
                track,
                "input_routing_channel",
                "available_input_routing_channels",
                input_routing_channel,
            )
    except Exception as exc:
        return str(exc)
    return None


def _set_routing_display_name(track, target_attr, available_attr, display_name):
    requested = str(display_name)
    for option in getattr(track, available_attr):
        if option.display_name == requested:
            setattr(track, target_attr, option)
            return
    raise ValueError("routing option not available: %s" % requested)


def probe_codecs():
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

    return (json.dumps({"ok": True, "modules": results}, separators=(",", ":")),)


def rebuild_arrangement_from_clips(handler, params):
    started = time.time()
    if not params:
        return (_error("missing workflow payload"),)

    try:
        payload = _decode_workflow_payload_arg(params[0])
        clear_specs = payload.get("clear", [])
        copy_specs = payload.get("copies", [])

        clear_tracks = _validate_clear_specs(handler, clear_specs)
        copy_ops = _validate_copy_specs(handler, copy_specs)

        deleted = 0
        for track in clear_tracks:
            for clip in list(track.arrangement_clips):
                track.delete_clip(clip)
                deleted += 1

        copied = 0
        for op in copy_ops:
            op["destination_track"].duplicate_clip_to_arrangement(
                op["source_clip"],
                op["destination_time"],
            )
            copied += 1

        elapsed_ms = int(round((time.time() - started) * 1000))
        return (
            json.dumps(
                {
                    "ok": True,
                    "deleted": deleted,
                    "copied": copied,
                    "elapsedMs": elapsed_ms,
                },
                separators=(",", ":"),
            ),
        )
    except Exception as exc:
        handler.logger.warning(
            "workflow rebuild_arrangement_from_clips failed: %s",
            traceback.format_exc(),
        )
        return (_error(str(exc)),)


def ensure_tracks(handler, params):
    started = time.time()
    if not params:
        return (_error("missing workflow payload"),)

    try:
        payload = _decode_workflow_payload_arg(params[0])
        header_name = _required_string(payload, "headerName")
        track_names = [
            str(name) for name in payload.get("trackNames", []) if str(name)
        ]
        if not track_names:
            raise ValueError("trackNames must contain at least one name")

        tracks = handler.song.tracks
        scan_start = int(payload.get("searchStart", 0))
        scan_end = int(payload.get("searchEnd", len(tracks)))
        if scan_start < 0 or scan_end < scan_start:
            raise ValueError(
                "invalid search range: start=%d end=%d" % (scan_start, scan_end)
            )
        scan_end = min(scan_end, len(tracks))

        insert_before_name = payload.get("insertBeforeName")
        create_header = bool(payload.get("createHeader", False))
        input_routing_type = payload.get("inputRoutingType")
        input_routing_channel = payload.get("inputRoutingChannel")

        header_index = _find_track_name(tracks, header_name, scan_start, scan_end)
        created_header = None
        if header_index is None:
            if not create_header:
                raise ValueError("header track not found: %s" % header_name)
            insert_index = None
            if insert_before_name:
                insert_index = _find_track_name(
                    tracks, insert_before_name, scan_start, scan_end
                )
            if insert_index is None:
                insert_index = scan_end
            handler.song.create_audio_track(insert_index)
            header_track = handler.song.tracks[insert_index]
            header_track.name = header_name
            created_header = {"track": insert_index, "name": header_name}
            header_index = insert_index
            scan_end += 1

        insert_index = _find_insert_index(
            handler.song.tracks,
            header_index,
            min(scan_end, len(handler.song.tracks)),
            insert_before_name,
        )
        existing = _existing_named_tracks(
            handler.song.tracks,
            track_names,
            header_index + 1,
            insert_index,
        )
        existing_names = set(row["name"] for row in existing)
        missing_names = [name for name in track_names if name not in existing_names]

        created = []
        routing_errors = []
        for name in missing_names:
            handler.song.create_audio_track(insert_index)
            track = handler.song.tracks[insert_index]
            track.name = name
            created.append({"track": insert_index, "name": name})

            error = _apply_input_routing(
                track,
                input_routing_type,
                input_routing_channel,
            )
            if error is not None:
                routing_errors.append(
                    {"track": insert_index, "name": name, "error": error}
                )

            insert_index += 1
            scan_end += 1

        final_insert_index = _find_insert_index(
            handler.song.tracks,
            header_index,
            min(scan_end, len(handler.song.tracks)),
            insert_before_name,
        )
        final_existing = _existing_named_tracks(
            handler.song.tracks,
            track_names,
            header_index + 1,
            final_insert_index,
        )
        final_names = set(row["name"] for row in final_existing)
        missing_after = [name for name in track_names if name not in final_names]

        elapsed_ms = int(round((time.time() - started) * 1000))
        result = {
            "ok": True,
            "elapsedMs": elapsed_ms,
            "headerTrack": header_index,
            "insertBeforeTrack": final_insert_index,
            "scanned": {
                "start": scan_start,
                "end": min(scan_end, len(handler.song.tracks)),
            },
            "existing": existing,
            "created": created,
            "missing": missing_after,
        }
        if created_header is not None:
            result["createdHeader"] = created_header
        if routing_errors:
            result["routingErrors"] = routing_errors

        return (json.dumps(result, separators=(",", ":")),)
    except Exception as exc:
        handler.logger.warning(
            "workflow ensure_tracks failed: %s",
            traceback.format_exc(),
        )
        return (_error(str(exc)),)


def export_arrangement_inventory(handler, params):
    started = time.time()
    try:
        payload = _decode_workflow_payload_arg(params[0]) if params else {}
        include_clip_names = bool(payload.get("includeClipNames", True))
        include_clip_timing = bool(payload.get("includeClipTiming", False))
        include_clip_color = bool(payload.get("includeClipColor", False))
        include_clip_audio = bool(payload.get("includeClipAudio", False))
        include_clip_file_path = bool(payload.get("includeClipFilePath", False))
        include_clip_warping = bool(payload.get("includeClipWarping", False))
        compact = bool(payload.get("compact", False))
        track_indices = _inventory_track_indices(handler, payload)
        tracks = []

        for track_index in track_indices:
            track = _track(handler, track_index)
            if compact:
                tracks.append(
                    _compact_inventory_track(
                        track_index,
                        track,
                        include_clip_names,
                        include_clip_timing,
                        include_clip_color,
                        include_clip_audio,
                        include_clip_file_path,
                        include_clip_warping,
                    )
                )
                continue
            row = {
                "track": track_index,
                "name": track.name,
                "readable": True,
                "clips": [],
            }
            try:
                for clip_index, clip in enumerate(track.arrangement_clips):
                    clip_row = {"index": clip_index}
                    if include_clip_names:
                        clip_row["name"] = clip.name
                    if include_clip_timing:
                        clip_row["start"] = clip.start_time
                        clip_row["length"] = clip.length
                    if include_clip_color:
                        clip_row["color"] = clip.color
                    if include_clip_audio:
                        clip_row["isAudioClip"] = clip.is_audio_clip
                    if include_clip_file_path:
                        clip_row["filePath"] = clip.file_path
                    if include_clip_warping:
                        clip_row["warping"] = clip.warping
                    row["clips"].append(clip_row)
            except Exception as exc:
                row["readable"] = False
                row["error"] = str(exc)
                row["clips"] = []
            tracks.append(row)

        elapsed_ms = int(round((time.time() - started) * 1000))
        return (
            json.dumps(
                {
                    "ok": True,
                    "elapsedMs": elapsed_ms,
                    "compact": compact,
                    "tracks": tracks,
                },
                separators=(",", ":"),
            ),
        )
    except Exception as exc:
        handler.logger.warning(
            "workflow export_arrangement_inventory failed: %s",
            traceback.format_exc(),
        )
        return (_error(str(exc)),)


def _inventory_track_indices(handler, payload):
    if "tracks" in payload:
        return [_required_track_index(handler, int(track)) for track in payload["tracks"]]

    start = int(payload.get("start", 0))
    end = int(payload.get("end", len(handler.song.tracks)))
    if start < 0 or end < start:
        raise ValueError("invalid track range: start=%d end=%d" % (start, end))
    end = min(end, len(handler.song.tracks))
    return list(range(start, end))


def _required_track_index(handler, index):
    if index < 0 or index >= len(handler.song.tracks):
        raise ValueError("track index out of range: %d" % index)
    return index


def _compact_inventory_track(
    track_index,
    track,
    include_clip_names,
    include_clip_timing,
    include_clip_color,
    include_clip_audio,
    include_clip_file_path,
    include_clip_warping,
):
    clips = []
    try:
        for clip_index, clip in enumerate(track.arrangement_clips):
            if (
                include_clip_timing
                or include_clip_color
                or include_clip_audio
                or include_clip_file_path
                or include_clip_warping
            ):
                clip_row = {"index": clip_index}
                if include_clip_names:
                    clip_row["name"] = clip.name
                if include_clip_timing:
                    clip_row["start"] = clip.start_time
                    clip_row["length"] = clip.length
                if include_clip_color:
                    clip_row["color"] = clip.color
                if include_clip_audio:
                    clip_row["isAudioClip"] = clip.is_audio_clip
                if include_clip_file_path:
                    clip_row["filePath"] = clip.file_path
                if include_clip_warping:
                    clip_row["warping"] = clip.warping
                clips.append(clip_row)
            elif include_clip_names:
                clips.append(clip.name)
            else:
                clips.append(clip_index)
        return [track_index, track.name, 1, clips]
    except Exception as exc:
        return [track_index, track.name, 0, str(exc)]


def _validate_clear_specs(handler, specs):
    tracks = []
    seen = set()
    for spec in specs:
        track_index = _required_int(spec, "track")
        if track_index in seen:
            continue
        track = _track(handler, track_index)
        expected_name = spec.get("expectedName")
        if expected_name is not None and track.name != expected_name:
            raise ValueError(
                "clear track %d name mismatch: expected %r, got %r"
                % (track_index, expected_name, track.name)
            )
        tracks.append(track)
        seen.add(track_index)
    return tracks


def _validate_copy_specs(handler, specs):
    ops = []
    for i, spec in enumerate(specs):
        source_clip = _arrangement_clip_from_path(handler, spec.get("source"))
        expected_clip_name = spec.get("expectedClipName")
        if expected_clip_name is not None and source_clip.name != expected_clip_name:
            raise ValueError(
                "copy %d source clip name mismatch: expected %r, got %r"
                % (i, expected_clip_name, source_clip.name)
            )

        destination_track_index = _required_int(spec, "destinationTrack")
        destination_track = _track(handler, destination_track_index)
        destination_track_name = spec.get("destinationTrackName")
        if (
            destination_track_name is not None
            and destination_track.name != destination_track_name
        ):
            raise ValueError(
                "copy %d destination track name mismatch: expected %r, got %r"
                % (i, destination_track_name, destination_track.name)
            )

        destination_time = float(spec.get("destinationTime", 0))
        if destination_time < 0:
            raise ValueError("copy %d destinationTime must be >= 0" % i)
        ops.append(
            {
                "source_clip": source_clip,
                "destination_track": destination_track,
                "destination_time": destination_time,
            }
        )
    return ops


def _track(handler, index):
    if index < 0 or index >= len(handler.song.tracks):
        raise ValueError("track index out of range: %d" % index)
    return handler.song.tracks[index]


def _arrangement_clip_from_path(handler, path):
    if not isinstance(path, str):
        raise ValueError("source must be an arrangement clip LOM path")
    match = _ARRANGEMENT_CLIP_RE.match(path)
    if not match:
        raise ValueError("unsupported source path: %r" % path)
    track = _track(handler, int(match.group("track")))
    clip_index = int(match.group("clip"))
    clips = track.arrangement_clips
    if clip_index < 0 or clip_index >= len(clips):
        raise ValueError("arrangement clip index out of range: %r" % path)
    return clips[clip_index]
