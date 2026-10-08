# agent/tools/get_expanded_window.py
# Tool Usage: 5-Chunk Context Window Expansion (Padding +-2 Chunks)

# Imports
from dotenv import load_dotenv
from google.adk.tools import ToolContext
from pydantic import BaseModel, Field
from typing import Any
import os

# Custom Dependencies
from db import db
from agent.tools.helpers import get_allowed_user_videos, parse_word_timestamps

# Load ENV Vars
load_dotenv()
LFVIDEO_SEGMENT_COLLECTION_NAME = os.environ["LFVIDEO_SEGMENT_COLLECTION_NAME"]

################################################################
# Tool Return Models
################################################################

class ExpandedWindowSummary(BaseModel):
    video_filename: str | None = None
    segment_range: str | None = None
    time_window: str | None = None
    segments_loaded: int = 0
    word_count: int = 0
    error: str | None = None


class ExpandedSegmentWindowResponse(BaseModel):
    summary: ExpandedWindowSummary = Field(
        description="Concise telemetry summary for UI trace logs."
    )
    video_id: str | None = Field(default=None, description="The long-form video ID.")
    video_filename: str | None = Field(default=None, description="The video filename.")
    window_start_sec: float | None = Field(
        default=None,
        description="Start timestamp in seconds of the expanded 5-segment window.",
    )
    window_end_sec: float | None = Field(
        default=None,
        description="End timestamp in seconds of the expanded 5-segment window.",
    )
    stitched_transcript: str | None = Field(
        default=None,
        description="Contiguous segment transcripts across the loaded window.",
    )
    word_level_timeline: str | None = Field(
        default=None,
        description="Space-separated '[start_sec-end_sec] word' entries for picking exact natural clip boundaries.",
    )
    error: str | None = Field(
        default=None, description="Error message if the window could not be loaded."
    )

################################################################
# Tool Function
################################################################

def get_expanded_segment_window(
    video_id: str,
    tool_context: ToolContext,
    center_segment_index: int,
    pad_before: int = 2,
    pad_after: int = 2,
) -> ExpandedSegmentWindowResponse:
    """Fetches a contiguous window of 30s segments around `center_segment_index`.
    
    Loads `pad_before` chunks before (default 2) and `pad_after` chunks after
    (default 2) and returns the stitched transcript and word-level timestamps
    (`[start_sec-end_sec] word`) so natural clip boundaries can be chosen.

    Args:
        video_id: The ID of the video containing the target segment.
        center_segment_index: The integer `segment_index` to center the window on.
        pad_before: Number of 30s segments before center to include (default 2).
        pad_after: Number of 30s segments after center to include (default 2).
    
    Returns:
        Structured `ExpandedSegmentWindowResponse` with the stitched transcript and word timeline.
    """
    # Get Allowed Videos
    uid = tool_context.user_id
    allowed_videos = get_allowed_user_videos(tool_context)
    if video_id not in allowed_videos:
        err_msg = "Target video is not in the user's active Tape Selector scope."
        return ExpandedSegmentWindowResponse(
            summary=ExpandedWindowSummary(error=err_msg),
            error=err_msg,
        )

    # Protective boundaries from hallucinations of large/negative indexes
    pad_before = max(0, min(int(pad_before), 4))
    pad_after = max(0, min(int(pad_after), 4))
    start_idx = max(0, int(center_segment_index) - pad_before)
    end_idx = int(center_segment_index) + pad_after

    # Get Segments from db
    seg_collection = db.collection(LFVIDEO_SEGMENT_COLLECTION_NAME)
    doc_refs = [
        seg_collection.document(f"{video_id}_seg_{idx:04d}")
        for idx in range(start_idx, end_idx + 1)
    ]
    
    loaded_segments: list[dict[str, Any]] = []
    for snap in db.get_all(doc_refs):
        if not snap.exists:
            continue
        sdata = snap.to_dict() or {}
        if sdata.get("uid") == uid and sdata.get("video_id") == video_id:
            loaded_segments.append(sdata)
    if not loaded_segments:
        err_msg = "No segments found in the requested window."
        return ExpandedSegmentWindowResponse(
            summary=ExpandedWindowSummary(error=err_msg),
            error=err_msg,
        )

    # sort segments by index
    loaded_segments.sort(key=lambda s: int(s.get("segment_index", 0)))

    # Get time bounds
    window_start_sec = float(loaded_segments[0].get("start_sec", 0.0))
    window_end_sec = float(loaded_segments[-1].get("end_sec", 0.0))

    # Build Full Transcript
    stitched_transcript_parts: list[str] = []
    all_words_timeline: list[str] = []
    total_words = 0
    for seg in loaded_segments:
        t_text = (seg.get("transcript_text") or "").strip()
        if t_text:
            stitched_transcript_parts.append(
                f"[Seg #{seg.get('segment_index')}: {float(seg.get('start_sec', 0)):.1f}s - {float(seg.get('end_sec', 0)):.1f}s] {t_text}"
            )
        words = parse_word_timestamps(seg.get("words", []))
        total_words += len(words)
        for w in words:
            all_words_timeline.append(f"[{w.start_sec:.2f}-{w.end_sec:.2f}] {w.word}")

    # Get Original Video's file name
    v_filename = allowed_videos[video_id].get("filename", "untitled.mp4")

    # return response obj
    return ExpandedSegmentWindowResponse(
        summary=ExpandedWindowSummary(
            video_filename=v_filename,
            segment_range=f"seg_{loaded_segments[0].get('segment_index'):04d} .. seg_{loaded_segments[-1].get('segment_index'):04d}",
            time_window=f"{window_start_sec:.1f}s - {window_end_sec:.1f}s ({window_end_sec - window_start_sec:.1f}s)",
            segments_loaded=len(loaded_segments),
            word_count=total_words,
        ),
        video_id=video_id,
        video_filename=v_filename,
        window_start_sec=window_start_sec,
        window_end_sec=window_end_sec,
        stitched_transcript="\n".join(stitched_transcript_parts),
        word_level_timeline=" ".join(all_words_timeline),
    )