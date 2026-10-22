"""AT-3054 probe: a local stand-in for the Responses API.

The first turn asks codex to run MOCK_CMD through its exec_command tool, so
the command runs exactly where a model's command would; the second turn
ends the session. Nothing leaves the runner and no model is invoked. The
server prints nothing and keeps nothing.
"""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

CMD = os.environ["MOCK_CMD"]
TURNS = {"n": 0}
USAGE = {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}


def _sse(rid, item):
    events = [
        {"type": "response.created", "response": {"id": rid}},
        {"type": "response.output_item.done", "item": item},
        {"type": "response.completed", "response": {"id": rid, "usage": USAGE}},
    ]
    return "".join(
        f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events
    ).encode()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length", 0)))
        TURNS["n"] += 1
        rid = f"resp_{TURNS['n']}"
        if TURNS["n"] == 1:
            source = "await tools.exec_command({cmd: %s}); text('ok');" % json.dumps(CMD)
            item = {"type": "custom_tool_call", "id": "ctc_1",
                    "call_id": "call_1", "name": "exec", "input": source}
        else:
            item = {"type": "message", "role": "assistant", "id": "msg_1",
                    "content": [{"type": "output_text", "text": "done"}]}
        body = _sse(rid, item)
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.send_response(404)
        self.end_headers()


HTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
