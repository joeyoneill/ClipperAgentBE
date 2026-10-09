# Chat View Flow Routes

# imports
from fastapi import APIRouter

# custom routers
from routes.chat.chat_sessions import router as chat_session_router

# Init Router
router = APIRouter(prefix="/chat")

# Add Routes
router.include_router(chat_session_router)