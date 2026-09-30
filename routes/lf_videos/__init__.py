# Long-form Video Routes

# imports
from fastapi import APIRouter

# custom routers
from routes.lf_videos.upload import router as upload_router

# Init Router
router = APIRouter(prefix="/lf_videos")

# Add Routes
router.include_router(upload_router)