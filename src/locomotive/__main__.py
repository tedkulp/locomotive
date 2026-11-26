"""``python -m locomotive``: one process, one uvicorn worker, settings from LOCOMOTIVE_* env vars."""

import os

import uvicorn

from locomotive.app import create_app
from locomotive.settings import Settings


def main() -> None:
    settings = Settings()  # type: ignore[call-arg]  (required values come from env)
    uvicorn.run(
        create_app(settings),
        host=os.environ.get("LOCOMOTIVE_HOST", "0.0.0.0"),
        port=int(os.environ.get("LOCOMOTIVE_PORT", "8000")),
        workers=1,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
