import sys
import types
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.append(str(PROJECT_ROOT))

if "abletonosc_plus" not in sys.modules:
    root_package = types.ModuleType("abletonosc_plus")
    root_package.__path__ = [str(PROJECT_ROOT)]
    sys.modules["abletonosc_plus"] = root_package
if "abletonosc_plus.abletonosc" not in sys.modules:
    abletonosc_package = types.ModuleType("abletonosc_plus.abletonosc")
    abletonosc_package.__path__ = [str(PROJECT_ROOT / "abletonosc")]
    sys.modules["abletonosc_plus.abletonosc"] = abletonosc_package

from abletonosc_plus.abletonosc.osc_server import OSCServer


class FakeMessage:
    address = "/test"
    params = ()


class FakeSocket:
    def __init__(self):
        self.sent = []

    def setblocking(self, _blocking):
        pass

    def bind(self, _addr):
        pass

    def sendto(self, data, remote_addr):
        self.sent.append((data, remote_addr))


def test_process_message_replies_to_request_source_port(monkeypatch):
    fake_socket = FakeSocket()
    monkeypatch.setattr("socket.socket", lambda *_args, **_kwargs: fake_socket)

    server = OSCServer()
    server.add_handler("/test", lambda _params: ("ok",))

    server.process_message(FakeMessage(), ("127.0.0.1", 49152))

    assert fake_socket.sent
    assert fake_socket.sent[-1][1] == ("127.0.0.1", 49152)
