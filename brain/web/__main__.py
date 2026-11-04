"""brain/.venv/bin/python -m brain.web [--host 127.0.0.1] [--port 8001]"""
import argparse
import os

import uvicorn

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.getenv("PORT", "8001")))
    a = ap.parse_args()
    uvicorn.run("brain.web.app:create_default_app", factory=True, host=a.host, port=a.port, proxy_headers=True)
