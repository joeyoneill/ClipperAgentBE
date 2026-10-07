# Short-form Content Sliced Video Models

# Imports
from datetime import datetime
from pydantic import BaseModel, Field
from typing import Literal

# Custom Dependencies
from models.misc import WordTimestamp
from utils.misc import get_utc_now

################################################################
# Short-Form Video DB Base Model
################################################################

class SFVideo(BaseModel):
    """
    Firestore document model for the `sf_videos` collection.
    Created when a user approves a ClipCandidate in Chat ("Save to Reel")
    """
    id: str | None = None
    uid: str
    source_video_id: str
    source_filename: str
    session_id: str | None = None
    candidate_id: str | None = None
    
    # Clip Metadata & Word-Level Boundaries
    title: str
    start_sec: float
    end_sec: float
    duration_seconds: float
    transcript_text: str
    words: list[WordTimestamp] = Field(default_factory=list)
    rationale: str | None = None
    
    # Core State Machine (mirrors LFVideo)
    status: Literal["PENDING_CUT", "PROCESSING", "SUCCESSFUL", "FAILED"] = "PENDING_CUT"
    is_complete: bool = False
    gcs_uri: str | None = None
    error_msg: str | None = None
    
    # Timestamps
    created_at: datetime = Field(default_factory=get_utc_now)
    updated_at: datetime = Field(default_factory=get_utc_now)

################################################################
# Short-Form Video REST Endpoint Models
################################################################

class SaveClipRequest(BaseModel):
    """
    Request body for `POST /api/clips/save` (or `/api/sf_videos/save`)
    USAGE: Human-in-the-loop approval of a ClipCandidate card in Chat.
    """
    candidate_id: str
    video_id: str
    session_id: str | None = None
    title: str
    start_sec: float
    end_sec: float
    transcript_text: str
    words: list[WordTimestamp] = Field(default_factory=list)
    rationale: str | None = None