# agent/tools/propose_clip_candidate.py
# Tool Usage: Propose Clip Candidate Card for Human-in-the-Loop Approval

# Imports
from dotenv import load_dotenv
from google.adk.tools import ToolContext
from pydantic import BaseModel, Field
from typing import Any
from uuid import uuid4
import os

# Custom Dependencies
from agent.tools.helpers import get_allowed_user_videos, parse_word_timestamps
from db import db
from models.chat import ClipCandidate
from models.misc import WordTimestamp
from utils.lf_videos import generate_video_signed_read_url

# Load ENV Vars
load_dotenv()
LFVIDEO_SEGMENT_COLLECTION_NAME = os.environ["LFVIDEO_SEGMENT_COLLECTION_NAME"]

################################################################
# Tool Return Models
################################################################

class ProposeClipSummary(BaseModel):
    title: str | None = None
    video_filename: str | None = None
    time_range: str | None = None
    word_count: int = 0
    error: str | None = None

class ProposeClipCandidateResponse(BaseModel):
    summary: ProposeClipSummary = Field(
        description="Concise telemetry summary for UI trace logs."
    )
    candidate: ClipCandidate | None = Field(
        default=None,
        description="The structured ClipCandidate card rendered inline in the Chat UI.",
    )
    error: str | None = Field(
        default=None, description="Error message if the clip bounds were invalid."
    )

################################################################
# Tool Function
################################################################

def propose_clip_candidate(
    video_id: str,
    title: str,
    start_sec: float,
    end_sec: float,
    rationale: str,
    tool_context: ToolContext,
) -> ProposeClipCandidateResponse:
    """Proposes a short-form video clip candidate for Human-in-the-Loop preview and approval.
    
    Validates video ownership against session state, slices the exact word-level timestamps within `[start_sec, end_sec]`, signs a GCS preview URL, and emits a `ClipCandidate` card to the Chat UI without saving to `sf_videos` until approved by the user.
    
    Args:
        video_id: The source long-form video ID.
        title: A catchy title for the proposed short-form clip.
        start_sec: Exact word-aligned start timestamp in seconds.
        end_sec: Exact word-aligned end timestamp in seconds.
        rationale: Brief explanation of why these boundaries make a great clip.
    
    Returns:
        Structured `ProposeClipCandidateResponse` containing `summary` and `candidate`.
    """
    # ensure requesting user has access to viddeo
    uid = tool_context.user_id
    allowed_videos = get_allowed_user_videos(tool_context)
    if video_id not in allowed_videos:
        err_msg = "Target video is not in the user's active Tape Selector scope."
        return ProposeClipCandidateResponse(
            summary=ProposeClipSummary(error=err_msg),
            error=err_msg,
        )

    # get video data
    vdata = allowed_videos[video_id]
    start_sec = round(float(start_sec), 2)
    end_sec = round(float(end_sec), 2)
    if end_sec <= start_sec:
        err_msg = f"Invalid clip bounds: start_sec ({start_sec}) must be < end_sec ({end_sec})."
        return ProposeClipCandidateResponse(
            summary=ProposeClipSummary(error=err_msg),
            error=err_msg,
        )

    # Fetch overlapping 30s segments to extract exact word timestamps for the clip
    first_seg_idx = max(0, int(start_sec // 30))
    last_seg_idx = max(first_seg_idx, int(end_sec // 30))
    seg_collection = db.collection(LFVIDEO_SEGMENT_COLLECTION_NAME)
    doc_refs = [
        seg_collection.document(f"{video_id}_seg_{idx:04d}")
        for idx in range(first_seg_idx, last_seg_idx + 1)
    ]
    overlapping_segments: list[dict[str, Any]] = []
    for snap in db.get_all(doc_refs):
        if snap.exists:
            sdata = snap.to_dict() or {}
            if sdata.get("uid") == uid and sdata.get("video_id") == video_id:
                overlapping_segments.append(sdata)
    overlapping_segments.sort(key=lambda s: int(s.get("segment_index", 0)))

    # get words to craft sliced transcript
    sliced_words: list[WordTimestamp] = []
    for seg in overlapping_segments:
        for w in parse_word_timestamps(seg.get("words", [])):
            if w.end_sec >= (start_sec - 0.05) and w.start_sec <= (end_sec + 0.05):
                sliced_words.append(w)
    if sliced_words:
        transcript_slice = " ".join(w.word for w in sliced_words)
    else:
        transcript_slice = " ".join(
            (s.get("transcript_text") or "").strip() for s in overlapping_segments).strip()

    # Generate signed GCS read url to preview sliced video
    preview_url: str | None = None
    if vdata.get("gcs_uri"):
        try:
            preview_url = generate_video_signed_read_url(vdata["gcs_uri"])
        except Exception:
            preview_url = None

    # Proposed Clip Candidate Info
    candidate = ClipCandidate(
        id=str(uuid4()),
        title=title.strip() or "UNTITLED CLIP",
        video_id=video_id,
        video_filename=vdata.get("filename", "untitled.mp4"),
        start_sec=start_sec,
        end_sec=end_sec,
        duration_sec=round(end_sec - start_sec, 2),
        transcript_text=transcript_slice,
        words=sliced_words,
        rationale=rationale.strip(),
        preview_url=preview_url,
    )

    # return clip candidate response
    return ProposeClipCandidateResponse(
        summary=ProposeClipSummary(
            title=candidate.title,
            video_filename=candidate.video_filename,
            time_range=f"{candidate.start_sec:.2f}s - {candidate.end_sec:.2f}s ({candidate.duration_sec:.2f}s)",
            word_count=len(candidate.words),
        ),
        candidate=candidate,
    )