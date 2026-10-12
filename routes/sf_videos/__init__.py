# sf_videos router

# Imports
from fastapi import APIRouter

# Custom ROuters
from routes.sf_videos.manage import router as manage_router
from routes.sf_videos.save import router as save_router

# Init Router
router = APIRouter(prefix="/sf_videos")

# Add Routes
router.include_router(manage_router)
router.include_router(save_router)