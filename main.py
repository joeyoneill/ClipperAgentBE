# FastAPI App main file

# Imports
from fastapi import FastAPI
from fastapi.responses import RedirectResponse

# Init FastAPI
app = FastAPI()

# Redirect to /docs
@app.get("/", tags=["General"])
def redirect():
    return RedirectResponse("/docs")