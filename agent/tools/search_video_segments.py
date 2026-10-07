# agent/tools/search_video_segments.py
# Tool Usage: Multimodal Vector Search (RAG) on lf_video_segments Firestore Collection

# Imports
from dotenv import load_dotenv
from google.adk.tools import ToolContext
from google.cloud.firestore_v1.base_query import FieldFilter
from google.cloud.firestore_v1.base_vector_query import DistanceMeasure
from google.cloud.firestore_v1.vector import Vector
from pydantic import BaseModel, Field
from typing import Any, Literal
import os

# Custom Dependencies
from db import db
from agent.tools.helpers import embed_query_text, get_allowed_user_videos

# Load ENV Vars
load_dotenv()
LFVIDEO_SEGMENT_COLLECTION_NAME = os.environ["LFVIDEO_SEGMENT_COLLECTION_NAME"]

################################################################
# Tool Return Models
################################################################

class MatchedVideoSegment(BaseModel):
    segment_doc_id: str = Field(
        description="Firestore segment document ID (<video_id>_seg_<idx>)."
    )
    video_id: str = Field(description="Parent long-form video ID.")
    video_filename: str = Field(description="Parent video filename.")
    segment_index: int = Field(
        description="0-based integer index of this 30-second segment."
    )
    start_sec: float = Field(description="Start time of this 30s segment in seconds.")
    end_sec: float = Field(description="End time of this 30s segment in seconds.")
    transcript_text: str = Field(description="Spoken transcript within this 30s segment.")
    matched_on: list[Literal["transcript", "visual"]] = Field(
        description="Which embedding modalities matched the query ('transcript', 'visual')."
    )
    rrf_score: float = Field(description="Reciprocal Rank Fusion relevance score.")


class SearchVideoSegmentsSummary(BaseModel):
    query: str
    search_mode: Literal["hybrid", "text", "visual"]
    scope: Literal["PINNED_TAPES", "ALL_VAULT_TAPES"]
    matches_found: int
    top_segments: list[str] = Field(default_factory=list)
    reason: str | None = None


class SearchVideoSegmentsResponse(BaseModel):
    summary: SearchVideoSegmentsSummary = Field(
        description="Concise telemetry summary for UI trace logs."
    )
    matches: list[MatchedVideoSegment] = Field(
        default_factory=list,
        description="Ranked list of matching 30-second video segments.",
    )

################################################################
# Tool Function
################################################################

def search_video_segments(
    query: str,
    tool_context: ToolContext,
    search_mode: Literal["hybrid", "text", "visual"] = "hybrid",
    top_k: int = 5,
) -> SearchVideoSegmentsResponse:
    """Searches the user's 30-second video segments using semantic vector search. Automatically restricts the search to the user's owned videos and any videos currently pinned in the UI Tape Selector (`selected_video_ids` in session state).
    
    Args:
        query: Natural language description of the topic, quote, or visual scene to find.
        search_mode: 'text' for spoken dialogue/quotes, 'visual' for visual scenes/actions,
            or 'hybrid' (default) to search both and fuse the rankings.
        top_k: Number of top matching 30-second segments to return (1 to 10).
    
    Returns:
        Structured `SearchVideoSegmentsResponse` containing ranked 30s segment matches.
    """
    uid = tool_context.user_id
    pinned_ids: list[str] = tool_context.state.get("selected_video_ids") or []
    scope_label: Literal["PINNED_TAPES", "ALL_VAULT_TAPES"] = (
        "PINNED_TAPES" if pinned_ids else "ALL_VAULT_TAPES"
    )
    top_k = max(1, min(int(top_k), 10))
    
    allowed_videos = get_allowed_user_videos(tool_context)
    if not allowed_videos:
        return SearchVideoSegmentsResponse(
            summary=SearchVideoSegmentsSummary(
                query=query,
                search_mode=search_mode,
                scope=scope_label,
                matches_found=0,
                reason="No completed videos in active scope.",
            ),
            matches=[],
        )
    
    target_video_ids: list[str | None] = (
        list(allowed_videos.keys()) if pinned_ids else [None]
    )
    
    query_vec = Vector(embed_query_text(query))
    seg_collection = db.collection(LFVIDEO_SEGMENT_COLLECTION_NAME)
    
    fields_to_search: list[str] = []
    if search_mode in ("hybrid", "text"):
        fields_to_search.append("text_embedding")
    if search_mode in ("hybrid", "visual"):
        fields_to_search.append("video_embedding")
    
    fused_scores: dict[str, float] = {}
    segment_payloads: dict[str, dict[str, Any]] = {}
    matched_modalities: dict[str, list[Literal["transcript", "visual"]]] = {}
    
    for target_vid in target_video_ids:
        for vec_field in fields_to_search:
            base_q = seg_collection.where(filter=FieldFilter("uid", "==", uid))
            if target_vid is not None:
                base_q = base_q.where(filter=FieldFilter("video_id", "==", target_vid))
            
            vec_q = base_q.find_nearest(
                vector_field=vec_field,
                query_vector=query_vec,
                distance_measure=DistanceMeasure.COSINE,
                limit=top_k,
            )
            
            for rank, snap in enumerate(vec_q.get()):
                sdata = snap.to_dict() or {}
                vid = sdata.get("video_id", "")
                if vid not in allowed_videos:
                    continue
                
                doc_id = snap.id
                fused_scores[doc_id] = fused_scores.get(doc_id, 0.0) + (
                    1.0 / (60.0 + rank + 1)
                )
                modality: Literal["transcript", "visual"] = (
                    "transcript" if vec_field == "text_embedding" else "visual"
                )
                matched_modalities.setdefault(doc_id, []).append(modality)
                
                if doc_id not in segment_payloads:
                    segment_payloads[doc_id] = {
                        "segment_doc_id": doc_id,
                        "video_id": vid,
                        "video_filename": allowed_videos[vid].get("filename", "untitled.mp4"),
                        "segment_index": int(sdata.get("segment_index", 0)),
                        "start_sec": float(sdata.get("start_sec", 0.0)),
                        "end_sec": float(sdata.get("end_sec", 0.0)),
                        "transcript_text": sdata.get("transcript_text", ""),
                    }
    
    ranked_doc_ids = sorted(
        fused_scores.keys(), key=lambda d: fused_scores[d], reverse=True
    )[:top_k]
    
    matches: list[MatchedVideoSegment] = []
    for doc_id in ranked_doc_ids:
        raw = segment_payloads[doc_id]
        matches.append(
            MatchedVideoSegment(
                **raw,
                matched_on=sorted(set(matched_modalities.get(doc_id, []))),
                rrf_score=round(fused_scores[doc_id], 5),
            )
        )
    
    return SearchVideoSegmentsResponse(
        summary=SearchVideoSegmentsSummary(
            query=query,
            search_mode=search_mode,
            scope=scope_label,
            matches_found=len(matches),
            top_segments=[
                f"{m.video_filename} [seg #{m.segment_index}: {m.start_sec:.1f}s-{m.end_sec:.1f}s]"
                for m in matches[:3]
            ],
        ),
        matches=matches,
    )