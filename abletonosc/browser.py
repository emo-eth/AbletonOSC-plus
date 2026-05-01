import json
from typing import Any, Tuple

import Live

from .handler import AbletonOSCHandler


ROOT_NAMES = (
    "sounds",
    "drums",
    "instruments",
    "audio_effects",
    "midi_effects",
    "max_for_live",
    "plugins",
    "clips",
    "samples",
    "grooves",
    "templates",
    "packs",
    "user_library",
)


class BrowserHandler(AbletonOSCHandler):
    def __init__(self, manager):
        super().__init__(manager)
        self.class_identifier = "browser"

    def init_api(self):
        self.osc_server.add_handler("/live/browser/list/root", self._handle_list_root)
        self.osc_server.add_handler("/live/browser/list", self._handle_list_children)
        self.osc_server.add_handler("/live/browser/search", self._handle_search)
        self.osc_server.add_handler("/live/browser/load_item", self._handle_load_item)
        self.osc_server.add_handler("/live/browser/hotswap", self._handle_hotswap)

    def _browser(self):
        return Live.Application.get_application().browser

    def _root_entries(self):
        browser = self._browser()
        entries = []
        for name in ROOT_NAMES:
            try:
                item = getattr(browser, name)
            except Exception:
                item = None
            if item is not None:
                entries.append((name, item))
        return entries

    def _item_uri(self, item, root_name=None):
        uri = self._safe_get(item, "uri")
        if uri:
            return uri
        if root_name:
            return "root:%s" % root_name
        return None

    def _item_json(self, item, root_name=None):
        payload = {
            "name": self._safe_get(item, "name"),
            "uri": self._item_uri(item, root_name),
            "is_loadable": bool(self._safe_get(item, "is_loadable", False)),
            "is_folder": bool(self._safe_get(item, "is_folder", False)),
            "source": self._safe_get(item, "source"),
        }
        try:
            payload["child_count"] = len(item.children)
        except Exception:
            pass
        if root_name is not None:
            payload["root"] = root_name
        return json.dumps(payload, separators=(",", ":"))

    def _safe_get(self, obj, attr, default=None):
        try:
            value = getattr(obj, attr)
        except Exception:
            return default
        try:
            json.dumps(value)
            return value
        except TypeError:
            return repr(value)

    def _children(self, item):
        try:
            return tuple(item.children)
        except Exception:
            return ()

    def _walk(self):
        visited = set()
        stack = list(reversed(self._root_entries()))
        while stack:
            root_name, item = stack.pop()
            identity = id(item)
            if identity in visited:
                continue
            visited.add(identity)
            yield root_name, item
            children = self._children(item)
            for child in reversed(children):
                stack.append((root_name, child))

    def _resolve_item(self, uri):
        uri = str(uri)
        if uri.startswith("root:"):
            root_name = uri.split(":", 1)[1]
            for name, item in self._root_entries():
                if name == root_name:
                    return item

        for root_name, item in self._walk():
            if self._item_uri(item, root_name) == uri:
                return item
        raise ValueError("browser item not found: %s" % uri)

    def _select_track(self, track_index):
        track = self.song.tracks[int(track_index)]
        self.song.view.selected_track = track
        return track

    def _select_device(self, track, device_index):
        device = track.devices[int(device_index)]
        select_device = getattr(self.song.view, "select_device", None)
        if callable(select_device):
            select_device(device)
        else:
            track.view.selected_device = device
        return device

    def _select_chain(self, track_index, device_index, chain_index):
        track = self._select_track(track_index)
        rack = self._select_device(track, device_index)
        chain = rack.chains[int(chain_index)]
        view = getattr(rack, "view", None)
        if view is not None:
            try:
                view.selected_chain = chain
            except Exception:
                self.logger.warning("Could not select rack chain %s", chain_index)
        try:
            rack.is_showing_chains = True
        except Exception:
            pass
        return chain

    def _handle_list_root(self, params: Tuple[Any] = ()):
        return tuple(self._item_json(item, root_name) for root_name, item in self._root_entries())

    def _handle_list_children(self, params: Tuple[Any] = ()):
        if not params:
            return ()
        uri = params[0]
        try:
            item = self._resolve_item(uri)
            return (uri,) + tuple(self._item_json(child) for child in self._children(item))
        except Exception as exc:
            self.logger.warning("browser/list %s failed: %s", uri, exc)
            return (uri,)

    def _handle_search(self, params: Tuple[Any] = ()):
        if not params:
            return ()
        query = str(params[0])
        limit = int(params[1]) if len(params) > 1 else 50
        query_l = query.lower()
        matches = []
        try:
            for root_name, item in self._walk():
                name = str(self._safe_get(item, "name", "")).lower()
                uri = str(self._item_uri(item, root_name) or "").lower()
                if query_l in name or query_l in uri:
                    matches.append(self._item_json(item, root_name))
                    if len(matches) >= limit:
                        break
        except Exception as exc:
            self.logger.warning("browser/search %s failed: %s", query, exc)
        return (query, limit) + tuple(matches)

    def _parse_load_args(self, params):
        if len(params) == 1:
            return None, None, None, params[0]
        if len(params) == 2:
            return params[0], None, None, params[1]
        if len(params) == 3:
            return params[0], params[1], None, params[2]
        if len(params) == 4:
            return params[0], params[1], params[2], params[3]
        raise ValueError("expected uri, track+uri, track+device+uri, or track+device+chain+uri")

    def _prepare_load_target(self, track_index, device_index, chain_index):
        if track_index is None:
            return
        if device_index is None:
            self._select_track(track_index)
            return
        if chain_index is None:
            track = self._select_track(track_index)
            self._select_device(track, device_index)
            return
        self._select_chain(track_index, device_index, chain_index)

    def _handle_load_item(self, params: Tuple[Any] = ()):
        try:
            track_index, device_index, chain_index, uri = self._parse_load_args(params)
            item = self._resolve_item(uri)
            self._prepare_load_target(track_index, device_index, chain_index)
            self._browser().load_item(item)
            return tuple(params)
        except Exception as exc:
            self.logger.warning("browser/load_item %s failed: %s", params, exc)

    def _handle_hotswap(self, params: Tuple[Any] = ()):
        if not params:
            return
        uri = params[0]
        try:
            item = self._resolve_item(uri)
            self._browser().load_item(item)
            return (uri,)
        except Exception as exc:
            self.logger.warning("browser/hotswap %s failed: %s", uri, exc)
