# Chat Models

# Imports
from datetime import datetime
from pydantic import BaseModel, Field
from typing import Any, Literal

# Custom Dependencies
from models.misc import WordTimestamp
from utils.misc import get_utc_now

################################################################
# Helper Models
################################################################

class ClipCandidate(BaseModel):
    """
    "proposed clip card" that the agent outputs via the propose_clip_candidate tool
    """
    id: str
    title: str
    video_id: str
    video_filename: str

    start_sec: float
    end_sec: float
    duration_sec: float

    transcript_text: str
    words: list[WordTimestamp] = Field(default_factory=list)
    rationale: str
    preview_url: str | None = None


class AgentTraceStep(BaseModel):
    """
    A single step in the agent's internal reasoning and tool execution pipeline during a turn.
    """
    id: str
    step_type: Literal["THOUGHT", "TOOL_CALL", "TOOL_RESULT"]
    title: str
    content: str | None = None
    tool_name: str | None = None
    tool_args: dict[str, Any] | None = None
    tool_summary: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=get_utc_now)

################################################################
# Chat Session & UI Message Models
################################################################

class ChatMessageModel(BaseModel):
    """
    A single message bubble in the Chat UI, persisted inside the ui_messages array on the chat_sessions Firestore document.
    """
    id: str
    role: Literal["user", "model", "system"]
    text: str
    trace: list[AgentTraceStep] = Field(default_factory=list)
    clip_candidates: list[ClipCandidate] = Field(default_factory=list)
    interrupted: bool = False
    created_at: datetime = Field(default_factory=get_utc_now)


class ChatSessionSummary(BaseModel):
    """
    lightweight (no messages array). When the left sidebar calls GET /api/chat/sessions to list all of a user's past chat threads, we only fetch the summary fields so Firestore doesn't send megabytes of message history and traces over the network just to render thread titles.
    """
    id: str
    uid: str
    title: str
    selected_video_ids: list[str] = Field(default_factory=list) #VIDs pinned for chat session, empty[]=all
    created_at: datetime = Field(default_factory=get_utc_now)
    updated_at: datetime = Field(default_factory=get_utc_now)


class ChatSessionDetail(ChatSessionSummary):
    """
    inherits from ChatSessionSummary and includes the full messages: list[ChatMessageModel] (with trace and clip_candidates) when the user clicks a specific chat session (GET /api/chat/sessions/{session_id}).
    """
    messages: list[ChatMessageModel] = Field(default_factory=list)

################################################################
# Chat REST Endpoint Models
################################################################

class ChatRequest(BaseModel):
    """
    Legacy REST chat endpoint non-streamed, really for testing
    """
    user_id: str
    session_id:str
    query: str


class CreateChatSessionRequest(BaseModel):
    """
    request body for `POST /api/chat/sessions`
    USAGE: creating a new thread and optionally pre-pinning specific video IDs
    """
    title: str | None = None
    selected_video_ids: list[str] = Field(default_factory=list)


class UpdateChatSessionRequest(BaseModel):
    """
    request body for `PATCH /api/chat/sessions/{session_id}`
    USAGE: renaming a thread or changing which videos are pinned in the Tape Selector
    """
    title: str | None = None
    selected_video_ids: list[str] | None = None

################################################################
# Chat WebSocket Models
################################################################

class WSClientMessage(BaseModel):
    """Inbound frame from React Frontend -> FastAPI WebSocket."""
    type: Literal["USER_MESSAGE", "INTERRUPT", "ADD_CONTEXT", "SET_VIDEOS"]
    text: str | None = None
    selected_video_ids: list[str] | None = None


class WSServerMessage(BaseModel):
    """Outbound frame from FastAPI WebSocket -> React Frontend."""
    type: Literal[
        "SESSION_INIT",
        "AGENT_THOUGHT",
        "TRACE_STEP",
        "AGENT_TOKEN",
        "CLIP_CANDIDATE",
        "TURN_COMPLETE",
        "TURN_INTERRUPTED",
        "CONTEXT_ADDED",
        "VIDEOS_UPDATED",
        "ERROR",
    ]
    data: dict[str, Any] = Field(default_factory=dict)