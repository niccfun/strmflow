from fastapi import APIRouter

from strmflow.api.routes import auth, emby302, media, system, transfers

router = APIRouter(prefix="/api")
router.include_router(auth.router)
router.include_router(system.router)
router.include_router(emby302.router)
router.include_router(media.router)
router.include_router(transfers.router)
