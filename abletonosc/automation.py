from typing import Any, Tuple

from .handler import AbletonOSCHandler


class AutomationHandler(AbletonOSCHandler):
    def __init__(self, manager):
        super().__init__(manager)
        self.class_identifier = "automation"

    def init_api(self):
        self.osc_server.add_handler("/live/automation/get/envelope", self._handle_get_envelope)
        self.osc_server.add_handler("/live/automation/insert/point", self._handle_insert_point)
        self.osc_server.add_handler("/live/automation/clear/envelope", self._handle_clear_envelope)

    def _resolve_target(self, params):
        track_index = int(params[0])
        track = self.song.tracks[track_index]
        target_type = str(params[1])
        if target_type == "device":
            device_index = int(params[2])
            parameter_index = int(params[3])
            return track.devices[device_index].parameters[parameter_index], 4
        if target_type == "mixer":
            mixer_target = str(params[2])
            if mixer_target == "send":
                send_index = int(params[3])
                return track.mixer_device.sends[send_index], 4
            if hasattr(track.mixer_device, mixer_target):
                return getattr(track.mixer_device, mixer_target), 3
            return getattr(track, mixer_target), 3
        raise ValueError("unknown automation target type: %s" % target_type)

    def _resolve_source_and_target(self, params):
        source_type = str(params[0])
        if source_type == "clip":
            clip_track = int(params[1])
            clip_slot = int(params[2])
            clip = self.song.tracks[clip_track].clip_slots[clip_slot].clip
            target, consumed = self._resolve_target(params[3:])
            return source_type, clip, target, 3 + consumed
        if source_type == "arrangement":
            target, consumed = self._resolve_target(params[1:])
            return source_type, None, target, 1 + consumed
        raise ValueError("unknown automation source type: %s" % source_type)

    def _envelope_for(self, source_type, source, target):
        owners = [source] if source is not None else [target, self.song]
        method_names = (
            "automation_envelope",
            "get_envelope",
            "get_automation_envelope",
            "get_automation_envelope_for_parameter",
        )
        for owner in owners:
            if owner is None:
                continue
            for method_name in method_names:
                method = getattr(owner, method_name, None)
                if callable(method):
                    try:
                        return method(target)
                    except Exception:
                        pass
        raise RuntimeError("automation envelope API is unavailable for %s" % source_type)

    def _point_time(self, point):
        for attr in ("time", "beat_time", "start_time"):
            try:
                return getattr(point, attr)
            except Exception:
                pass
        return None

    def _point_value(self, point):
        for attr in ("value", "dest_value"):
            try:
                return getattr(point, attr)
            except Exception:
                pass
        return None

    def _read_points(self, envelope):
        for attr in ("points", "automation_points", "steps"):
            try:
                value = getattr(envelope, attr)
                return value() if callable(value) else value
            except Exception:
                pass
        return ()

    def _handle_get_envelope(self, params: Tuple[Any] = ()):
        try:
            source_type, source, target, _consumed = self._resolve_source_and_target(params)
            envelope = self._envelope_for(source_type, source, target)
            values = []
            for point in self._read_points(envelope):
                time = self._point_time(point)
                value = self._point_value(point)
                if time is not None and value is not None:
                    values.extend((time, value))
            return tuple(params) + tuple(values)
        except Exception as exc:
            self.logger.warning("automation/get/envelope %s failed: %s", params, exc)
            return tuple(params)

    def _insert_envelope_point(self, envelope, time, value):
        for method_name, args in (
            ("insert_point", (time, value)),
            ("add_point", (time, value)),
            ("insert_step", (time, 0.0, value)),
        ):
            method = getattr(envelope, method_name, None)
            if callable(method):
                method(*args)
                return
        raise RuntimeError("automation envelope has no insert method")

    def _handle_insert_point(self, params: Tuple[Any] = ()):
        try:
            source_type, source, target, consumed = self._resolve_source_and_target(params)
            time = params[consumed]
            value = params[consumed + 1]
            envelope = self._envelope_for(source_type, source, target)
            self._insert_envelope_point(envelope, time, value)
            return tuple(params)
        except Exception as exc:
            self.logger.warning("automation/insert/point %s failed: %s", params, exc)

    def _handle_clear_envelope(self, params: Tuple[Any] = ()):
        try:
            source_type, source, target, _consumed = self._resolve_source_and_target(params)
            if source_type == "clip" and hasattr(source, "clear_envelope"):
                source.clear_envelope(target)
                return tuple(params)
            envelope = self._envelope_for(source_type, source, target)
            clear = getattr(envelope, "clear", None)
            if callable(clear):
                clear()
            return tuple(params)
        except Exception as exc:
            self.logger.warning("automation/clear/envelope %s failed: %s", params, exc)
