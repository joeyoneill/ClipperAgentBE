# ADK FirestoreSessionService Initialization & UI Converters

# Imports
from datetime import datetime, timezone
from dotenv import load_dotenv
from google.adk.events import Event, EventActions
from google.adk.integrations.firestore.firestore_session_service import (
    FirestoreSessionService,
)
from google.adk.sessions.session import Session
from google.cloud import firestore
from pydantic import BaseModel
from typing import Any
from uuid import uuid4
import os
import time

# Custom Dependencies
from db import db
from models.chat import (
    AgentTraceStep,
    ChatMessageModel,
    ChatSessionDetail,
    ChatSessionSummary,
    ClipCandidate,
)
from utils.lf_videos import generate_video_signed_read_url
from utils.misc import get_utc_now

# Load Env Vars
load_dotenv()
GCP_PROJECT_ID = os.environ["GCP_PROJECT_ID"]
CHAT_SESSION_COLLECTION_NAME = os.environ["CHAT_SESSION_COLLECTION_NAME"]
LFVIDEO_COLLECTION_NAME = os.environ["LFVIDEO_COLLECTION_NAME"]
CHAT_APP_NAME = "clipping_studio"

################################################################
# Prebuilt ADK Firestore Session Service Singleton
################################################################

session_service = FirestoreSessionService(
    client=firestore.AsyncClient(project=GCP_PROJECT_ID),
    root_collection=CHAT_SESSION_COLLECTION_NAME,
)

################################################################
# Session -> UI Model Converters
################################################################

def _ts_to_datetime(ts: float | None) -> datetime:
    if not ts:
        return get_utc_now()
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def unwrap_tool_response(raw_response: dict[str, Any] | None) -> dict[str, Any]:
    """
    Unwraps ADK's `{'result': ...}` wrapper when a tool returns a Pydantic BaseModel
    instance directly, returning a normalized plain dict for UI trace & card extraction.
    """
    if not raw_response:
        return {}
    resp = dict(raw_response)
    if "result" in resp:
        inner = resp["result"]
        if isinstance(inner, BaseModel):
            return inner.model_dump(mode="json")
        if isinstance(inner, dict):
            return inner
    return resp


def session_to_chat_summary(session: Session) -> ChatSessionSummary:
    """
    Converts an ADK Session into a lightweight ChatSessionSummary for the sidebar.
    Reads `title` and `selected_video_ids` from `session.state`.
    """
    state = session.state or {}
    updated_dt = _ts_to_datetime(session.last_update_time)
    created_iso = state.get("created_at")
    created_dt = (
        datetime.fromisoformat(created_iso)
        if isinstance(created_iso, str)
        else updated_dt
    )

    return ChatSessionSummary(
        id=session.id,
        uid=session.user_id,
        title=state.get("title", "NEW CLIPPING LOG"),
        selected_video_ids=state.get("selected_video_ids", []),
        created_at=created_dt,
        updated_at=updated_dt,
    )


def session_to_chat_detail(session: Session) -> ChatSessionDetail:
    """
    Reconstructs the React UI's `list[ChatMessageModel]` (including full
    `trace: list[AgentTraceStep]` and `clip_candidates: list[ClipCandidate]`)
    directly from ADK's persisted `session.events`.
    """
    summary = session_to_chat_summary(session)
    messages: list[ChatMessageModel] = []
    all_candidates: list[ClipCandidate] = []

    # Current in-progress assistant message bubble being assembled for a turn
    current_model_msg: ChatMessageModel | None = None

    for ev in session.events:
        ev_dt = _ts_to_datetime(ev.timestamp)

        # Check if this event marks the current turn as interrupted
        is_aborted_event = ev.error_code == "INVOCATION_ABORTED" or bool(
            ev.actions
            and ev.actions.state_delta
            and ev.actions.state_delta.get("last_turn_interrupted")
        )
        if is_aborted_event:
            if current_model_msg is None:
                current_model_msg = ChatMessageModel(
                    id=ev.id or str(uuid4()),
                    role="model",
                    text="",
                    trace=[],
                    clip_candidates=[],
                    interrupted=True,
                    created_at=ev_dt,
                )
            else:
                current_model_msg.interrupted = True

        # Skip pure state-delta / system / empty abort events that have no content parts
        if ev.author == "system" or not ev.content or not ev.content.parts:
            continue

        # 1. User Message Event
        if ev.author == "user":
            if ev.branch:
                continue
            if current_model_msg is not None:
                messages.append(current_model_msg)
                current_model_msg = None

            user_text_parts = [
                p.text
                for p in ev.content.parts
                if p.text and not getattr(p, "thought", False)
            ]
            messages.append(
                ChatMessageModel(
                    id=ev.id or str(uuid4()),
                    role="user",
                    text="".join(user_text_parts),
                    created_at=ev_dt,
                )
            )
            continue

        # 2. Agent / Tool Event -> Ensure we have an active model message bubble
        if current_model_msg is None:
            current_model_msg = ChatMessageModel(
                id=ev.id or str(uuid4()),
                role="model",
                text="",
                trace=[],
                clip_candidates=[],
                interrupted=False,
                created_at=ev_dt,
            )

        for part in ev.content.parts:
            # A. Model Thought / Reasoning Part (coalesce consecutive thought parts)
            if getattr(part, "thought", False) and part.text:
                if (
                    current_model_msg.trace
                    and current_model_msg.trace[-1].step_type == "THOUGHT"
                ):
                    current_model_msg.trace[-1].content = (
                        current_model_msg.trace[-1].content or ""
                    ) + part.text
                else:
                    current_model_msg.trace.append(
                        AgentTraceStep(
                            id=str(uuid4()),
                            step_type="THOUGHT",
                            title="AGENT REASONING",
                            content=part.text,
                            created_at=ev_dt,
                        )
                    )

            # B. Tool Call Part
            elif part.function_call:
                fc = part.function_call
                current_model_msg.trace.append(
                    AgentTraceStep(
                        id=fc.id or str(uuid4()),
                        step_type="TOOL_CALL",
                        title=f"CALLING {fc.name}",
                        tool_name=fc.name,
                        tool_args=dict(fc.args) if fc.args else {},
                        created_at=ev_dt,
                    )
                )

            # C. Tool Result Part
            elif part.function_response:
                fr = part.function_response
                resp_dict = unwrap_tool_response(fr.response)
                step_prefix = "ABORTED" if is_aborted_event else "COMPLETED"

                current_model_msg.trace.append(
                    AgentTraceStep(
                        id=fr.id or str(uuid4()),
                        step_type="TOOL_RESULT",
                        title=f"{step_prefix} {fr.name}",
                        tool_name=fr.name,
                        tool_summary=resp_dict.get("summary") or resp_dict,
                        created_at=ev_dt,
                    )
                )

                # Collect ClipCandidate cards; we batch-sign their URLs after the loop
                if fr.name == "propose_clip_candidate" and resp_dict.get("candidate"):
                    cand = ClipCandidate.model_validate(resp_dict["candidate"])
                    current_model_msg.clip_candidates.append(cand)
                    all_candidates.append(cand)

            # D. Visible Assistant Text Part
            elif part.text and not ev.branch:
                current_model_msg.text += part.text

    if current_model_msg is not None:
        messages.append(current_model_msg)

    # Batch-fetch all referenced LFVideo docs in 1 Firestore RPC & sign preview URLs
    if all_candidates:
        unique_vids = {c.video_id for c in all_candidates}
        doc_refs = [
            db.collection(LFVIDEO_COLLECTION_NAME).document(vid)
            for vid in unique_vids
        ]
        signed_url_map: dict[str, str] = {}
        for snap in db.get_all(doc_refs):
            if not snap.exists:
                continue
            vdata = snap.to_dict() or {}
            if vdata.get("uid") == session.user_id and vdata.get("gcs_uri"):
                try:
                    signed_url_map[snap.id] = generate_video_signed_read_url(
                        vdata["gcs_uri"]
                    )
                except Exception:
                    pass

        for cand in all_candidates:
            if cand.video_id in signed_url_map:
                cand.preview_url = signed_url_map[cand.video_id]

    return ChatSessionDetail(
        **summary.model_dump(),
        messages=messages,
    )


async def update_session_state_fields(
    session: Session,
    updates: dict[str, Any],
) -> Session:
    """
    Persists state updates (such as `title` or `selected_video_ids`) into
    ADK's FirestoreSessionService via a lightweight state-delta Event.
    """
    actions_with_update = EventActions(state_delta=updates)
    system_event = Event(
        invocation_id=str(uuid4()),
        author="system",
        actions=actions_with_update,
        timestamp=time.time(),
    )
    await session_service.append_event(session=session, event=system_event)
    return session