# Long Form Video DB Models

# Imports
from datetime import datetime, timezone
from pydantic import BaseModel, Field
from typing import Literal

# Custom Dependencies
from models.misc import WordTimestamp
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
# LF Video Upload Endpoint Models
################################################################

# Long-form Video Upload Request Model
class LFUploadRequest(BaseModel):
    filename: str = Field(..., description="Original name of the video file")
    content_type: str = Field(..., description="MIME type of the video, e.g. 'video/mp4'")
    file_size_bytes: int | None = Field(None, description="Optional size of the file in bytes")
    duration_seconds: int | None = Field(None, description="Optional video duration in seconds")

# Long-form Video Upload Response Model
class LFUploadResponse(BaseModel):
    video_id: str
    upload_url: str
    gcs_uri: str
    blob_path: str

################################################################
# Upload Failed Endpoint Models
################################################################

# Long-form Video Upload Failed Request Model
class LFUploadFailedRequest(BaseModel):
    error_msg: str = Field(
        default="Upload failed or was aborted by client.",
        description="Reason the upload failed"
    )

################################################################
# RAG Segment Model
################################################################

# Processed Time-Window Segment for Vector Retrieval
class LFVideoSegment(BaseModel):
    id: str | None = None
    video_id: str
    uid: str
    segment_index: int = Field(..., description="0-based index of the segment in the video")
    
    # Time Window (in seconds)
    start_offset: float
    end_offset: float

    # Audio / Transcript Data for this Window
    transcript_text: str = ""
    words: list[WordTimestamp] = Field(default_factory=list)
    
    # 1408-dim Shared Vector Space Embeddings (multimodalembedding@001)
    video_embedding: list[float] | None = None
    text_embedding: list[float] | None = None
    
    # Timestamp
    created_at: datetime = Field(default_factory=get_utc_now)