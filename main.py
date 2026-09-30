# FastAPI App main file

# Imports
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
import os

# Routers
from routes.chat import router as chat_router

# Load Env Vars
load_dotenv()

# Init FastAPI
app = FastAPI()

# Enable CORS
FE_URL = os.environ['FRONTEND_URL']
app.add_middleware(
    CORSMiddleware,
    allow_origins=[FE_URL],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Connect Routers
app.include_router(chat_router)

# Redirect to /docs
@app.get("/", tags=["General"])
def redirect():
    return RedirectResponse("/docs")

# auth test
from fastapi import Depends
from utils.auth import UserInfo, get_current_user
@app.post("/auth_test")
def auth_test(user: UserInfo = Depends(get_current_user)):
    return {
        "message": f"Hello, {user.email}",
        "user_id": user.uid,
        "user_info": user
    }