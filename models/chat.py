# Chat Models

# Imports
from datetime import datetime
from pydantic import BaseModel
from typing import Any, Literal

# Chat Request Model
class LegacyChatRequest(BaseModel):
    user_id: str
    session_id:str
    query: str