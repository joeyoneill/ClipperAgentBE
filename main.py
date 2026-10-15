# FastAPI App main file

# Imports
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
import os

# Custom Dependencies
from routes import router as management_router
from utils.observability import setup_observability

# Load Env Vars
load_dotenv()

# Init FastAPI
app = FastAPI()

# Setup Observability for logging/monitoring/trace
setup_observability("clipper-agent-backend")

# Enable CORS
FE_ORIGINS = [
    origin.strip().rstrip("/")
    for origin in os.environ.get("FRONTEND_URL", "http://localhost:5173").split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=FE_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Connect Management Router
app.include_router(management_router)

# Redirect to /docs
@app.get("/", tags=["General"])
def redirect():
    return RedirectResponse("/docs")

# Health Check
@app.get("/health", tags=["Health"])
def health_check() -> dict[str, str]:
    return {"status": "ok"}