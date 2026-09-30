# Long Form Video DB Models

# Imports
from datetime import datetime, timezone
from pydantic import BaseModel, Field
from typing import Literal

# Custom Dependencies
from utils.misc import get_utc_now

################################################################

# Long Form Video DB Base Model
class LFVideo(BaseModel):
    id: str | None = None
    uid: str
    filename: str
    gcs_uri: str

    # Core State Machine
    status: Literal["PENDING", "UPLOADING", "PROCESSING", "SUCCESSFUL", "FAILED"]
    is_complete: bool = False
    error_msg: str | None = None

    # Metadata
    content_type: str | None = None
    duration_seconds: int | None = None
    file_size_bytes: int | None = None
    transcript: str | None = None

    # Timestamps
    created_at: datetime = Field(default_factory=get_utc_now)
    updated_at: datetime = Field(default_factory=get_utc_now)

################################################################

# Long-form Video Upload Request Model
class LFUploadRequest(BaseModel):
    filename: str = Field(..., description="Original name of the video file")
    content_type: str = Field(..., description="MIME type of the video, e.g. 'video/mp4'")
    file_size_bytes: int | None = Field(None, description="Optional size of the file in bytes")

# Long-form Video Upload Response Model
class LFUploadResponse(BaseModel):
    video_id: str
    upload_url: str
    gcs_uri: str
    blob_path: str