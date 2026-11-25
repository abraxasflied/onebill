"""Android entry point for OneBill.

Runs the unchanged OneBill engine (main.py) as a local server on 127.0.0.1.
Differences from the Windows start-up:
  * no Edge/Chrome window (the Flutter WebView shows the UI)
  * no 180-second auto-exit watchdog (it would close the app when idle)
  * data is stored in the app's private folder (ONEBILL_DATA set by Flutter)
"""
import os
import sys
import threading
from http.server import ThreadingHTTPServer

os.environ["ONEBILL_NOWINDOW"] = "1"  # ONEBILL_DATA is set by Flutter

import main as ob  # the unchanged OneBill engine  # noqa: E402

_server = None


def start(port=None):
    """Start the server in a background thread. Safe to call once."""
    global _server
    if _server is not None:
        return ob.PORT
    if port:
        ob.PORT = int(port)
    ob.init()
    ob.autobackup()
    _server = ThreadingHTTPServer(("127.0.0.1", ob.PORT), ob.H)
    threading.Thread(target=_server.serve_forever, daemon=True).start()
    return ob.PORT


if __name__ == "__main__":
    print("OneBill server on port", start())
    threading.Event().wait()
