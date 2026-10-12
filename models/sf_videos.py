# Short-form Content Sliced Video Models

# Imports
from datetime import datetime
from pydantic import BaseModel, Field
from typing import Literal

# Custom Dependencies
from models.misc import WordTimestamp
from utils.misc import get_utc_now

################################################################
# Export / Download Option Types
################################################################

AspectRatioType = Literal["original", "9:16_crop", "9:16_pad"]
ResolutionType = Literal["1080p", "720p"]

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
    rendered_variants: dict[str, str] = Field(default_factory=dict)
    error_msg: str | None = None
    
    # Timestamps
    created_at: datetime = Field(default_factory=get_utc_now)
    updated_at: datetime = Field(default_factory=get_utc_now)


class SFVideoResponse(SFVideo):
    """
    Returned to the React Frontend in the REEL view; includes a signed `preview_url`
    of the parent LFVideo so the user can preview `#t={start_sec},{end_sec}` inline.
    """
    preview_url: str | None = None

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


class UpdateSFVideoRequest(BaseModel):
    """
    Request body for `PATCH /api/sf_videos/{sf_id}`
    USAGE: Renaming a saved clip title in the Reel view.
    """
    title: str


class DownloadSFVideoRequest(BaseModel):
    """
    Request body for `POST /api/sf_videos/{sf_id}/download`
    USAGE: On-demand rendering & downloading of a clip with configurable
    aspect ratio, resolution (1080p Full HD vs 720p Fast), and burned-in subtitles.
    """
    aspect_ratio: AspectRatioType = "original"
    resolution: ResolutionType = "1080p"
    subtitles: bool = False


class DownloadSFVideoResponse(BaseModel):
    """
    Response body for `POST /api/sf_videos/{sf_id}/download`
    """
    download_url: str
    filename: str
    variant_key: str
    cached: bool