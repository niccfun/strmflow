from fastapi import APIRouter

from strmflow.api.routes import auth, bdpan, emby302, media, notifications, system, transfers

router = APIRouter(prefix="/api")
router.include_router(auth.router)
router.include_router(bdpan.router)
router.include_router(notifications.router)
router.include_router(system.router)
router.include_router(emby302.router)
router.include_router(media.router)
router.include_router(transfers.router)
