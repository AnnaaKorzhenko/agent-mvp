"""brain/.venv/bin/python -m brain.mcp_server [--host 127.0.0.1] [--port 8000]"""
import argparse
import os

import uvicorn

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")))
    a = ap.parse_args()
    uvicorn.run("brain.mcp_server.server:create_app", factory=True, host=a.host, port=a.port, proxy_headers=True)
