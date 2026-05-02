import time
import pytest
from pathlib import Path

#--------------------------------------------------------------------------------
# Add . to the path so that pythonosc can be imported, enabling unit testing
# without any external dependencies
#--------------------------------------------------------------------------------
import sys
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.append(str(PROJECT_ROOT))

from client import AbletonOSCClient, TICK_DURATION

# Keep pure unit tests importable from the monorepo root without loading the
# Ableton remote-script package initializer, which imports Live-only modules.
import types
if "abletonosc" not in sys.modules:
    abletonosc_package = types.ModuleType("abletonosc")
    abletonosc_package.__path__ = [str(PROJECT_ROOT / "abletonosc")]
    sys.modules["abletonosc"] = abletonosc_package

# Live tick is 100ms. Wait for this long plus a short additional buffer.
TICK_DURATION = 0.125

@pytest.fixture(scope="module")
def client() -> AbletonOSCClient:
    client = AbletonOSCClient()
    yield client
    client.stop()

def wait_one_tick():
    """
    Sleep for one Ableton Live tick (100ms).
    """
    time.sleep(TICK_DURATION)

c = AbletonOSCClient()
c.send_message("/live/api/reload")
c.stop()
