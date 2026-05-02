import json
import types
import sys


class _Component:
    def __init__(self, *args, **kwargs):
        pass


class _AbletonOSCHandler:
    def __init__(self, manager):
        self.manager = manager
        self.osc_server = manager.osc_server
        self.logger = types.SimpleNamespace(warning=lambda *_args: None)
        self.init_api()

    def init_api(self):
        pass


sys.modules.setdefault("Live", types.ModuleType("Live"))
sys.modules.setdefault("ableton", types.ModuleType("ableton"))
sys.modules.setdefault("ableton.v2", types.ModuleType("ableton.v2"))
sys.modules.setdefault(
    "ableton.v2.control_surface", types.ModuleType("ableton.v2.control_surface")
)
component_module = types.ModuleType("ableton.v2.control_surface.component")
component_module.Component = _Component
sys.modules.setdefault("ableton.v2.control_surface.component", component_module)
handler_module = types.ModuleType("abletonosc.handler")
handler_module.AbletonOSCHandler = _AbletonOSCHandler
sys.modules.setdefault("abletonosc.handler", handler_module)

from abletonosc.browser import BrowserHandler


class FakeOscServer:
    def add_handler(self, *_args):
        pass


class FakeManager:
    def __init__(self, browser):
        self.osc_server = FakeOscServer()
        self._browser = browser


class FakeBrowserHandler(BrowserHandler):
    def _browser(self):
        return self.manager._browser


class BrowserItem:
    def __init__(
        self,
        name,
        uri,
        *,
        children=(),
        is_loadable=False,
        is_folder=False,
        source="",
    ):
        self.name = name
        self.uri = uri
        self.children = tuple(children)
        self.is_loadable = is_loadable
        self.is_folder = is_folder
        self.source = source


def make_handler():
    audio_effects = BrowserItem(
        "Audio Effects",
        "query:AudioFx",
        children=(
            BrowserItem("EQ Eight", "query:AudioFx#Eq8", is_loadable=True),
        ),
    )
    clips = BrowserItem(
        "Clips",
        "query:Clips",
        children=(
            BrowserItem("Gate Kit.alc", "query:Clips#GateKit", is_loadable=True),
        ),
    )
    browser = types.SimpleNamespace(
        sounds=BrowserItem("Sounds", "query:Sounds"),
        drums=BrowserItem("Drums", "query:Drums"),
        instruments=BrowserItem("Instruments", "query:Synths"),
        audio_effects=audio_effects,
        midi_effects=BrowserItem("MIDI Effects", "query:MidiFx"),
        max_for_live=BrowserItem("Max for Live", "query:M4L"),
        plugins=BrowserItem("Plug-Ins", "query:Plugins"),
        clips=clips,
        samples=BrowserItem("Samples", "query:Samples"),
        packs=BrowserItem("Packs", "query:LivePacks"),
        user_library=BrowserItem("User Library", "query:UserLibrary"),
    )
    return FakeBrowserHandler(FakeManager(browser))


def payloads(result):
    return [json.loads(raw) for raw in result[2:]]


def test_browser_search_can_be_restricted_to_one_root():
    handler = make_handler()

    result = handler._handle_search(("Gate", 25, "audio_effects"))

    assert result[:2] == ("Gate", 25)
    assert payloads(result) == []


def test_browser_search_root_filter_finds_audio_effects_without_clip_matches():
    handler = make_handler()

    result = handler._handle_search(("EQ Eight", 25, "audio_effects"))

    matches = payloads(result)
    assert len(matches) == 1
    assert matches[0]["name"] == "EQ Eight"
    assert matches[0]["root"] == "audio_effects"


def test_browser_resolve_uses_search_result_cache():
    handler = make_handler()
    item = BrowserItem("EQ Eight", "query:AudioFx#EQ%20Eight", is_loadable=True)
    handler._search_cache = {item.uri: item}

    assert handler._resolve_item(item.uri) is item
