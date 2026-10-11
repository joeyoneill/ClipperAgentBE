# Chat View Flow Routes

# imports
from fastapi import APIRouter

# custom routers
from routes.chat.chat_sessions import router as chat_session_router
from routes.chat.stream import router as stream_router

# Init Router
router = APIRouter(prefix="/chat")

# Add Routes
router.include_router(chat_session_router)
router.include_router(stream_router)