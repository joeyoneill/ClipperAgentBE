# FastAPI App main file

# Imports
from fastapi import FastAPI
from fastapi.responses import RedirectResponse

# Routers
from routes.chat import router as chat_router

# Init FastAPI
app = FastAPI()

# Connect Routers
app.include_router(chat_router)

# Redirect to /docs
@app.get("/", tags=["General"])
def redirect():
    return RedirectResponse("/docs")