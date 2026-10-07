# MISC models used across different files

# Imports
from pydantic import BaseModel

################################################################
# Transcribed text timestamps from Speech-to-text API
################################################################

class WordTimestamp(BaseModel):
    word: str
    start_offset: float
    end_offset: float