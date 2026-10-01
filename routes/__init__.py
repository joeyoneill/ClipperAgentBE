# Route Management File

# Imports
from fastapi import APIRouter

# Routers
from routes.chat import router as chat_router
from routes.lf_videos import router as lf_videos_router

# Initialize Main Management Router
router = APIRouter(prefix="/api")

# Connect Routers
router.include_router(chat_router)
router.include_router(lf_videos_router)