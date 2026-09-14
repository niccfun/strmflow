import uvicorn

from strmflow.core.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "strmflow.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
    )


if __name__ == "__main__":
    main()
