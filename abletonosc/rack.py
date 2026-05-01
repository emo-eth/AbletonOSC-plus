from typing import Any, Tuple

from .handler import AbletonOSCHandler


class RackHandler(AbletonOSCHandler):
    def __init__(self, manager):
        super().__init__(manager)
        self.class_identifier = "rack"

    def init_api(self):
        self.osc_server.add_handler("/live/rack/get/chains", self._handle_get_chains)
        self.osc_server.add_handler("/live/rack/get/drum_pads/name", self._handle_get_drum_pad_names)
        self.osc_server.add_handler("/live/rack/get/drum_pads/note", self._handle_get_drum_pad_notes)
        self.osc_server.add_handler("/live/rack/get/drum_pads/mute", self._handle_get_drum_pad_mutes)
        self.osc_server.add_handler("/live/rack/get/drum_pads/solo", self._handle_get_drum_pad_solos)
        self.osc_server.add_handler("/live/rack/get/drum_pads/has_chain", self._handle_get_drum_pad_has_chain)
        self.osc_server.add_handler("/live/rack/get/selected_drum_pad", self._handle_get_selected_drum_pad)
        self.osc_server.add_handler("/live/rack/set/selected_drum_pad", self._handle_set_selected_drum_pad)

        for prop in ("name", "mute", "solo", "color", "color_index"):
            self.osc_server.add_handler("/live/chain/get/%s" % prop, self._chain_getter(prop))
            self.osc_server.add_handler("/live/chain/set/%s" % prop, self._chain_setter(prop))
        self.osc_server.add_handler("/live/chain/get/devices/name", self._handle_get_chain_device_names)

    def _rack(self, params):
        track_index, device_index = int(params[0]), int(params[1])
        return track_index, device_index, self.song.tracks[track_index].devices[device_index]

    def _chain(self, params):
        track_index, device_index, rack = self._rack(params)
        chain_index = int(params[2])
        return track_index, device_index, chain_index, rack.chains[chain_index]

    def _drum_pads(self, rack):
        try:
            return tuple(rack.drum_pads)
        except Exception:
            return ()

    def _handle_get_chains(self, params: Tuple[Any] = ()):
        track_index, device_index, rack = self._rack(params)
        return (track_index, device_index) + tuple(chain.name for chain in rack.chains)

    def _drum_pad_values(self, params, getter):
        track_index, device_index, rack = self._rack(params)
        values = []
        for pad in self._drum_pads(rack):
            try:
                values.append(getter(pad))
            except Exception:
                values.append(None)
        return (track_index, device_index) + tuple(values)

    def _handle_get_drum_pad_names(self, params: Tuple[Any] = ()):
        return self._drum_pad_values(params, lambda pad: pad.name)

    def _handle_get_drum_pad_notes(self, params: Tuple[Any] = ()):
        return self._drum_pad_values(params, lambda pad: pad.note)

    def _handle_get_drum_pad_mutes(self, params: Tuple[Any] = ()):
        return self._drum_pad_values(params, lambda pad: pad.mute)

    def _handle_get_drum_pad_solos(self, params: Tuple[Any] = ()):
        return self._drum_pad_values(params, lambda pad: pad.solo)

    def _handle_get_drum_pad_has_chain(self, params: Tuple[Any] = ()):
        return self._drum_pad_values(params, lambda pad: len(pad.chains) > 0)

    def _handle_get_selected_drum_pad(self, params: Tuple[Any] = ()):
        track_index, device_index, rack = self._rack(params)
        pads = self._drum_pads(rack)
        selected = None
        try:
            selected = rack.view.selected_drum_pad
        except Exception:
            pass
        selected_index = -1
        if selected is not None:
            try:
                selected_index = list(pads).index(selected)
            except ValueError:
                selected_index = -1
        return track_index, device_index, selected_index

    def _handle_set_selected_drum_pad(self, params: Tuple[Any] = ()):
        track_index, device_index, rack = self._rack(params)
        pad_index = int(params[2])
        try:
            rack.view.selected_drum_pad = rack.drum_pads[pad_index]
        except Exception as exc:
            self.logger.warning("rack/set/selected_drum_pad %s failed: %s", params, exc)
        return track_index, device_index, pad_index

    def _chain_getter(self, prop):
        def handler(params: Tuple[Any] = ()):
            track_index, device_index, chain_index, chain = self._chain(params)
            return track_index, device_index, chain_index, getattr(chain, prop)
        return handler

    def _chain_setter(self, prop):
        def handler(params: Tuple[Any] = ()):
            track_index, device_index, chain_index, chain = self._chain(params)
            value = params[3]
            setattr(chain, prop, value)
            return track_index, device_index, chain_index, value
        return handler

    def _handle_get_chain_device_names(self, params: Tuple[Any] = ()):
        track_index, device_index, chain_index, chain = self._chain(params)
        return (track_index, device_index, chain_index) + tuple(device.name for device in chain.devices)
