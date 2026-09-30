# Chat Models

# Imports
from pydantic import BaseModel

# Chat Request Model
class ChatRequest(BaseModel):
    user_id: str
    session_id:str
    query: str