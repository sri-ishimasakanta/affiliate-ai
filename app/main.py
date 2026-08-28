from fastapi import FastAPI

from app.api.exception_handlers import register_exception_handlers
from app.api.v1.router import router as api_v1_router
from app.config.settings import settings

app = FastAPI(title=settings.app_name, debug=settings.debug)

register_exception_handlers(app)
app.include_router(api_v1_router, prefix="/api/v1")


@app.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}
