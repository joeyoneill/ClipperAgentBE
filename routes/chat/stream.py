# routes/chat/stream.py
# Bidirectional WebSocket Streaming Endpoint for ADK 2.0 Chat & Clipping Agent

# Imports
import asyncio
import time
from typing import Any
from uuid import uuid4

from fastapi import (
    APIRouter,
    Depends,
    Query,
    WebSocket,
    WebSocketDisconnect,
)
from google.adk import Runner
from google.adk.agents.run_config import RunConfig
from google.adk.agents._streaming_mode import StreamingMode
from google.adk.events import Event, EventActions
from google.adk.sessions.session import Session
from google.genai.types import Content, Part

# Custom Dependencies
from agent.agent import orchestrator_agent
from models.chat import (
    AgentTraceStep,
    ClipCandidate,
    ServerMessageType,
    WSClientMessage,
    WSServerMessage,
)
from utils.auth import UserInfo, get_ws_current_user
from utils.chat_sessions import (
    CHAT_APP_NAME,
    session_service,
    session_to_chat_summary,
    unwrap_tool_response,
    update_session_state_fields,
    validate_owned_video_ids
)
from utils.misc import get_utc_now

# Init Router
router = APIRouter(tags=["Chat Stream"])

# Init ADK 2.0 Runner Singleton
runner = Runner(
    app_name=CHAT_APP_NAME,
    agent=orchestrator_agent,
    session_service=session_service,
)


################################################################
# WebSocket Outbound Frame Helper
################################################################

async def _send_ws_frame(
    websocket: WebSocket,
    msg_type: ServerMessageType,
    data: dict[str, Any] | None = None,
) -> None:
    """
    Validates and sends a typed WSServerMessage JSON frame over the WebSocket.
    """
    frame = WSServerMessage(
        type=msg_type,
        data=data or {},
    )
    await websocket.send_json(frame.model_dump(mode="json"))

################################################################
# WS: /api/chat/stream Endpoint
################################################################

@router.websocket("/stream")
async def agent_stream(
    websocket: WebSocket,
    session_id: str | None = Query(default=None),
    user: UserInfo = Depends(get_ws_current_user),
) -> None:
    """
    Websocket connection endpoint to handle agent chat stream

    1. Authentication Handshake
    2. 
    3. Agent Loop
    """
    await websocket.accept()

    # Resolve or create the user's ADK Session in Firestore
    try:
        loaded_session: Session | None = None
        if session_id:
            loaded_session = await session_service.get_session(
                app_name=CHAT_APP_NAME,
                user_id=user.uid,
                session_id=session_id,
            )
        
        if loaded_session is None:
            initial_state = {
                "title": "NEW CLIPPING LOG",
                "selected_video_ids": [],
                "created_at": get_utc_now().isoformat(),
            }
            loaded_session = await session_service.create_session(
                app_name=CHAT_APP_NAME,
                user_id=user.uid,
                session_id=session_id,
                state=initial_state,
            )
        
        session: Session = loaded_session
        active_session_id = session.id

        # Send initial session handshake to React frontend
        await _send_ws_frame(
            websocket,
            "SESSION_INIT",
            {"session": session_to_chat_summary(session).model_dump(mode="json")},
        )
    except Exception as e:
        await _send_ws_frame(
            websocket,
            "ERROR",
            {"message": f"Failed to initialize chat session: {e}"},
        )
        await websocket.close()
        return

    # ---- Per-connection Concurrency State ----

    active_turn_task: asyncio.Task | None = None
    active_abort_signal: asyncio.Event | None = None
    streamed_text_buffer: str = ""
    has_uncommitted_text: bool = False

    # ---- Turn Cancellation & Interruption Handling Helper ----

    async def _stop_active_turn() -> None:
        """
        Signals ADK 2.0's native `abort_signal` to halt the running turn and seal
        any dangling tool calls in Firestore. If the agent was interrupted mid-sentence
        before the final non-partial text event was committed, persists the partial text.
        """
        nonlocal active_turn_task, active_abort_signal, streamed_text_buffer, has_uncommitted_text
        if active_turn_task is None or active_turn_task.done():
            active_turn_task = None
            active_abort_signal = None
            return
        
        # Wait briefly for ADK to synthesize & commit INVOCATION_ABORTED events
        if active_abort_signal is not None:
            active_abort_signal.set()
        active_turn_task.cancel()
        try:
            await active_turn_task
        except (asyncio.CancelledError, Exception):
            pass

        # If interrupted mid-sentence, persist the partial streamed text to Firestore
        if has_uncommitted_text and streamed_text_buffer.strip():
            try:
                latest_session = await session_service.get_session(
                    app_name=CHAT_APP_NAME,
                    user_id=user.uid,
                    session_id=active_session_id,
                )
                if latest_session:
                    partial_event = Event(
                        invocation_id=str(uuid4()),
                        author=orchestrator_agent.name,
                        content=Content(
                            role="model",
                            parts=[Part(text=streamed_text_buffer)],
                        ),
                        actions=EventActions(
                            state_delta={"last_turn_interrupted": True}
                        ),
                        timestamp=time.time(),
                    )
                    await session_service.append_event(
                        session=latest_session,
                        event=partial_event,
                    )
            except Exception:
                pass
        
        # Reset coroutine state
        streamed_text_buffer = ""
        has_uncommitted_text = False
        active_turn_task = None
        active_abort_signal = None

    # ---- Single Chat Turn Execution Helper ----

    async def _run_agent_turn(
        user_text: str,
        abort_signal: asyncio.Event,
    ) -> None:
        """
        Executes a single conversational turn via `runner.run_async` with
        `StreamingMode.SSE` and streams thoughts, tool traces, clip candidates,
        and text deltas over the WebSocket.
        """
        nonlocal session, streamed_text_buffer, has_uncommitted_text
        streamed_text_buffer = ""
        has_uncommitted_text = False
        
        try:
            # Refresh session header to check if we should auto-title on the first turn
            current_session = await session_service.get_session(
                app_name=CHAT_APP_NAME,
                user_id=user.uid,
                session_id=active_session_id,
            )
            if current_session:
                session = current_session
            
            state_delta: dict[str, Any] = {"last_turn_interrupted": False}
            current_title = (session.state or {}).get("title", "NEW CLIPPING LOG")
            if current_title == "NEW CLIPPING LOG":
                cleaned = " ".join(user_text.strip().split())
                new_title = (cleaned[:42] + "...") if len(cleaned) > 45 else cleaned
                if new_title:
                    state_delta["title"] = new_title
            
            user_msg = Content(
                role="user",
                parts=[Part(text=user_text)],
            )
            
            async for ev in runner.run_async(
                user_id=user.uid,
                session_id=active_session_id,
                new_message=user_msg,
                state_delta=state_delta,
                run_config=RunConfig(streaming_mode=StreamingMode.SSE)
            ):
                if abort_signal.is_set():
                    break
                
                if not ev.content or not ev.content.parts:
                    continue
                
                ev_dt = get_utc_now()
                
                # Partial Streaming Events (Token & Thought Deltas)
                if ev.partial:
                    for part in ev.content.parts:
                        if getattr(part, "thought", False) and part.text:
                            await _send_ws_frame(
                                websocket,
                                "AGENT_THOUGHT",
                                {"delta": part.text, "branch": ev.branch},
                            )
                        elif part.text and not ev.branch:
                            streamed_text_buffer += part.text
                            has_uncommitted_text = True
                            await _send_ws_frame(
                                websocket,
                                "AGENT_TOKEN",
                                {"delta": part.text},
                            )
                    continue
                
                # Non-Partial Committed Events (Traces, Tool Calls, Final Text)
                if ev.author in ("user", "system"):
                    continue
                
                for part in ev.content.parts:
                    # Finalized Thought Trace Step
                    if getattr(part, "thought", False) and part.text:
                        step = AgentTraceStep(
                            id=str(uuid4()),
                            step_type="THOUGHT",
                            title="AGENT REASONING",
                            content=part.text,
                            created_at=ev_dt,
                        )
                        await _send_ws_frame(
                            websocket,
                            "TRACE_STEP",
                            {"step": step.model_dump(mode="json")},
                        )
                    
                    # Tool Call Trace Step
                    elif part.function_call:
                        fc = part.function_call
                        step = AgentTraceStep(
                            id=fc.id or str(uuid4()),
                            step_type="TOOL_CALL",
                            title=f"CALLING {fc.name}",
                            tool_name=fc.name,
                            tool_args=dict(fc.args) if fc.args else {},
                            created_at=ev_dt,
                        )
                        await _send_ws_frame(
                            websocket,
                            "TRACE_STEP",
                            {"step": step.model_dump(mode="json")},
                        )
                    
                    # Tool Result Trace Step + ClipCandidate Extraction
                    elif part.function_response:
                        fr = part.function_response
                        resp_dict = unwrap_tool_response(fr.response)
                        step = AgentTraceStep(
                            id=fr.id or str(uuid4()),
                            step_type="TOOL_RESULT",
                            title=f"COMPLETED {fr.name}",
                            tool_name=fr.name,
                            tool_summary=resp_dict.get("summary") or resp_dict,
                            created_at=ev_dt,
                        )
                        await _send_ws_frame(
                            websocket,
                            "TRACE_STEP",
                            {"step": step.model_dump(mode="json")},
                        )
                        
                        # Emit structured ClipCandidate card immediately if proposed
                        if fr.name == "propose_clip_candidate" and resp_dict.get("candidate"):
                            cand = ClipCandidate.model_validate(resp_dict["candidate"])
                            await _send_ws_frame(
                                websocket,
                                "CLIP_CANDIDATE",
                                {"candidate": cand.model_dump(mode="json")},
                            )
                    
                    # Final Committed Visible Text (root agent only)
                    elif part.text and not ev.branch:
                        # Fallback if the model emitted text without prior partial chunks
                        if not streamed_text_buffer:
                            await _send_ws_frame(
                                websocket,
                                "AGENT_TOKEN",
                                {"delta": part.text},
                            )
                        has_uncommitted_text = False
            
            # If the turn completed without being aborted, notify the client
            if not abort_signal.is_set():
                updated_session = await session_service.get_session(
                    app_name=CHAT_APP_NAME,
                    user_id=user.uid,
                    session_id=active_session_id,
                )
                if updated_session:
                    session = updated_session
                
                await _send_ws_frame(
                    websocket,
                    "TURN_COMPLETE",
                    {
                        "session_id": active_session_id,
                        "session": session_to_chat_summary(session).model_dump(mode="json"),
                    },
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if not abort_signal.is_set():
                await _send_ws_frame(
                    websocket,
                    "ERROR",
                    {"message": f"Agent execution error: {e}"},
                )

    # ---- Main Websocket Receive & Dispatch Loop ----

    try:
        while True:
            raw_payload = await websocket.receive_json()
            try:
                client_msg = WSClientMessage.model_validate(raw_payload)
            except Exception as e:
                await _send_ws_frame(
                    websocket,
                    "ERROR",
                    {"message": f"Invalid WebSocket message format: {e}"},
                )
                continue
            
            # USER_MESSAGE: Start a new conversational turn
            if client_msg.type == "USER_MESSAGE":
                text = (client_msg.text or "").strip()
                if not text:
                    continue
                await _stop_active_turn()
                if client_msg.selected_video_ids is not None:
                    verified_vids = validate_owned_video_ids(
                        client_msg.selected_video_ids,
                        user.uid
                    )
                    latest_session = await session_service.get_session(
                        app_name=CHAT_APP_NAME,
                        user_id=user.uid,
                        session_id=active_session_id,
                    )
                    if latest_session:
                        session = await update_session_state_fields(
                            latest_session, {"selected_video_ids": verified_vids}
                        )
                active_abort_signal = asyncio.Event()
                active_turn_task = asyncio.create_task(
                    _run_agent_turn(text, active_abort_signal)
                )
            
            # INTERRUPT: Immediately halt the in-flight agent turn
            elif client_msg.type == "INTERRUPT":
                await _stop_active_turn()
                await _send_ws_frame(
                    websocket,
                    "TURN_INTERRUPTED",
                    {"session_id": active_session_id},
                )
            
            # ADD_CONTEXT: Interrupt in-flight turn, inject new context, and pivot
            elif client_msg.type == "ADD_CONTEXT":
                steering_text = (client_msg.text or "").strip()
                if not steering_text:
                    continue
                await _stop_active_turn()
                await _send_ws_frame(
                    websocket,
                    "CONTEXT_ADDED",
                    {"session_id": active_session_id, "text": steering_text},
                )
                active_abort_signal = asyncio.Event()
                active_turn_task = asyncio.create_task(
                    _run_agent_turn(
                        f"[USER ADDED CONTEXT / STEERING]: {steering_text}",
                        active_abort_signal,
                    )
                )
            
            # SET_VIDEOS: Update the active Tape Selector scope on the session
            elif client_msg.type == "SET_VIDEOS":
                try:
                    verified_vids = validate_owned_video_ids(
                        client_msg.selected_video_ids or [],
                        user.uid,
                    )
                    latest_session = await session_service.get_session(
                        app_name=CHAT_APP_NAME,
                        user_id=user.uid,
                        session_id=active_session_id,
                    )
                    if latest_session:
                        session = await update_session_state_fields(
                            latest_session,
                            {"selected_video_ids": verified_vids},
                        )
                    await _send_ws_frame(
                        websocket,
                        "VIDEOS_UPDATED",
                        {"selected_video_ids": verified_vids},
                    )
                except Exception as e:
                    await _send_ws_frame(
                        websocket,
                        "ERROR",
                        {"message": f"Failed to update pinned videos: {e}"},
                    )
    
    except WebSocketDisconnect:
        await _stop_active_turn()
    finally:
        await _stop_active_turn()