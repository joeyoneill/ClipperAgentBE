# agent/tools/list_available_videos.py
# Tool Usage: List User's Available / Pinned Vault Videos

# Imports
from google.adk.tools import ToolContext
from pydantic import BaseModel, Field
from typing import Any, Literal

# Custom Dependencies
from agent.tools.helpers import get_allowed_user_videos

################################################################
# Tool Return Models
################################################################

class AvailableVideoItem(BaseModel):
    video_id: str = Field(description="Unique Firestore ID of the long-form video.")
    filename: str = Field(description="Original filename of the uploaded video.")
    duration_seconds: int | None = Field(
        default=None, description="Total duration of the video in seconds."
    )
    transcript_preview: str = Field(
        description="First 300 characters of the video's full transcript."
    )


class ListAvailableVideosSummary(BaseModel):
    scope: Literal["PINNED_TAPES", "ALL_VAULT_TAPES"]
    video_count: int
    filenames: list[str]


class ListAvailableVideosResponse(BaseModel):
    summary: ListAvailableVideosSummary = Field(
        description="Concise telemetry summary for UI trace logs."
    )
    scope: Literal["PINNED_TAPES", "ALL_VAULT_TAPES"] = Field(
        description="Whether results are filtered by the user's Tape Selector or Vault-wide."
    )
    videos: list[AvailableVideoItem] = Field(
        description="List of completed videos available in the current session scope."
    )

################################################################
# TOOL FUNCTION
################################################################

def list_available_videos(tool_context: ToolContext) -> ListAvailableVideosResponse:
    """Lists the user's completed long-form videos available in the current Tape Selector scope.
    
    Call this tool when the user asks what videos/tapes they have uploaded, or when you want to check available filenames and durations.
    
    Returns:
        Structured `ListAvailableVideosResponse` with available videos and active scope.
    """
    # Get Allowed Videos from state -> db
    pinned_ids: list[str] = tool_context.state.get("selected_video_ids") or []
    videos_map = get_allowed_user_videos(tool_context)

    # Create Video List for return
    video_list = [
        AvailableVideoItem(
            video_id=vid,
            filename=vdata.get("filename", "untitled.mp4"),
            duration_seconds=vdata.get("duration_seconds"),
            transcript_preview=(vdata.get("transcript") or "")[:300],
        )
        for vid, vdata in videos_map.items()
    ]

    # Craft return obj & return
    scope_label = "PINNED_TAPES" if pinned_ids else "ALL_VAULT_TAPES"
    return ListAvailableVideosResponse(
        summary=ListAvailableVideosSummary(
            scope=scope_label,
            video_count=len(video_list),
            filenames=[v.filename for v in video_list],
        ),
        scope=scope_label,
        videos=video_list,
    )