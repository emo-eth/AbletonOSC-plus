from typing import Any, Tuple

from .handler import AbletonOSCHandler


class MixerHandler(AbletonOSCHandler):
    def __init__(self, manager):
        super().__init__(manager)
        self.class_identifier = "mixer"

    def init_api(self):
        for prop in ("volume", "panning", "crossfader", "cue_volume", "output_meter_level", "name"):
            self.osc_server.add_handler("/live/master/get/%s" % prop, self._master_getter(prop))
        for prop in ("volume", "panning", "crossfader", "cue_volume", "name"):
            self.osc_server.add_handler("/live/master/set/%s" % prop, self._master_setter(prop))

        for prop in ("name", "volume", "panning", "mute", "solo", "color", "color_index", "output_meter_level"):
            self.osc_server.add_handler("/live/return/get/%s" % prop, self._return_getter(prop))
        for prop in ("name", "volume", "panning", "mute", "solo", "color", "color_index"):
            self.osc_server.add_handler("/live/return/set/%s" % prop, self._return_setter(prop))

        self.osc_server.add_handler("/live/track/set/mixer/crossfade_assign", self._handle_set_crossfade_assign)

    def _read_property(self, track, prop):
        mixer = track.mixer_device
        if hasattr(mixer, prop):
            value = getattr(mixer, prop)
            if hasattr(value, "value"):
                return value.value
            return value
        return getattr(track, prop)

    def _write_property(self, track, prop, value):
        mixer = track.mixer_device
        if hasattr(mixer, prop):
            target = getattr(mixer, prop)
            if hasattr(target, "value"):
                target.value = value
            else:
                setattr(mixer, prop, value)
        else:
            setattr(track, prop, value)

    def _master_getter(self, prop):
        def handler(params: Tuple[Any] = ()):
            return (self._read_property(self.song.master_track, prop),)
        return handler

    def _master_setter(self, prop):
        def handler(params: Tuple[Any] = ()):
            self._write_property(self.song.master_track, prop, params[0])
            return (params[0],)
        return handler

    def _return_getter(self, prop):
        def handler(params: Tuple[Any] = ()):
            return_index = int(params[0])
            track = self.song.return_tracks[return_index]
            return return_index, self._read_property(track, prop)
        return handler

    def _return_setter(self, prop):
        def handler(params: Tuple[Any] = ()):
            return_index = int(params[0])
            value = params[1]
            track = self.song.return_tracks[return_index]
            self._write_property(track, prop, value)
            return return_index, value
        return handler

    def _handle_set_crossfade_assign(self, params: Tuple[Any] = ()):
        track_index, value = int(params[0]), params[1]
        track = self.song.tracks[track_index]
        self._write_property(track, "crossfade_assign", value)
        return track_index, value
