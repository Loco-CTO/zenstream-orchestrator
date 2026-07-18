import os
import sys

sys.dont_write_bytecode = True

from dotenv import load_dotenv
import uvicorn

load_dotenv()

if __name__ == "__main__":
    uvicorn.run(
        "app.app:app",
        host=os.getenv("ORCHESTRATOR_HOST", "127.0.0.1"),
        port=int(os.getenv("ORCHESTRATOR_PORT", "9090")),
        reload=os.getenv("USE_RELOADER", "").lower() in {"1", "true", "yes"},
    )
