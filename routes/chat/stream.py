# routes/chat/stream.py
# Bidirectional WebSocket Streaming Endpoint for ADK 2.0 Chat & Clipping Agent

# Imports
import asyncio
import time
from typing import Any
from uuid import uuid4

from fastapi import (
    APIRouter,
    Query,
    WebSocket,
    WebSocketDisconnect,
    status
)
from google.adk import Runner
from google.adk.agents.run_config import RunConfig
from google.adk.agents._streaming_mode import StreamingMode
from google.adk.events import Event, EventActions
from google.adk.sessions.session import Session
from google.genai.types import Content, Part

# Custom Dependencies
from agent.agent import clipping_app, orchestrator_agent
from models.chat import (
    AgentTraceStep,
    ClipCandidate,
    ServerMessageType,
    WSClientMessage,
    WSServerMessage,
)
from utils.auth import UserInfo, verify_firebase_jwt
from utils.chat_sessions import (
    CHAT_APP_NAME,
    session_service,
    session_to_chat_summary,
    unwrap_tool_response,
    update_session_state_fields,
    validate_owned_video_ids
)
from utils.misc import get_utc_now
from utils.observability import get_tracer, log_analytics_event

# Init Router
router = APIRouter(tags=["Chat Stream"])

# Init Tracer
tracer = get_tracer(__name__)

# Init ADK 2.0 Runner Singleton
runner = Runner(
    app=clipping_app,
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
) -> None:
    """
    Websocket connection endpoint to handle agent chat stream

    1. First-Message Authentication Handshake (AUTH frame)
    2. Session Resolution (SESSION_INIT frame)
    3. Bidirectional Agent Loop
    """
    await websocket.accept()

    # First-Message Authentication Handshake
    try:
        raw_auth = await asyncio.wait_for(websocket.receive_json(), timeout=10.0)
        auth_msg = WSClientMessage.model_validate(raw_auth)
        if auth_msg.type != "AUTH" or not auth_msg.token:
            await _send_ws_frame(
                websocket,
                "ERROR",
                {"message": "First WebSocket frame must be an AUTH message with a token."},
            )
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        user: UserInfo = verify_firebase_jwt(auth_msg.token)
        if auth_msg.session_id:
            session_id = auth_msg.session_id
    except asyncio.TimeoutError:
        await _send_ws_frame(
            websocket,
            "ERROR",
            {"message": "WebSocket authentication timed out."},
        )
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    except Exception as e:
        await _send_ws_frame(
            websocket,
            "ERROR",
            {"message": f"WebSocket authentication failed: {e}"},
        )
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    
    # Resolve or create the user's ADK Session in Firestore
    try:
        session: Session | None = None
        active_session_id: str | None = None
        if session_id:
            session = await session_service.get_session(
                app_name=CHAT_APP_NAME,
                user_id=user.uid,
                session_id=session_id,
            )
            if session:
                active_session_id = session.id
        
        # Send initial handshake to React frontend (session is omitted if in draft mode)
        await _send_ws_frame(
            websocket,
            "SESSION_INIT",
            {"session": session_to_chat_summary(session).model_dump(mode="json")}
            if session
            else {},
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
        if (
            has_uncommitted_text
            and streamed_text_buffer.strip()
            and active_session_id is not None
        ):
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
        if active_session_id is None or session is None:
            return
        sid: str = active_session_id
        streamed_text_buffer = ""
        has_uncommitted_text = False
        
        try:
            # Refresh session header to check if we should auto-title on the first turn
            current_session = await session_service.get_session(
                app_name=CHAT_APP_NAME,
                user_id=user.uid,
                session_id=sid,
            )
            if current_session:
                session = current_session
            
            state_delta: dict[str, Any] = {"last_turn_interrupted": False}
            current_title = (
                (session.state or {}).get("title", "NEW CLIPPING LOG")
                if session
                else "NEW CLIPPING LOG"
            )
            if current_title == "NEW CLIPPING LOG":
                cleaned = " ".join(user_text.strip().split())
                new_title = (cleaned[:42] + "...") if len(cleaned) > 45 else cleaned
                if new_title:
                    state_delta["title"] = new_title
            
            user_msg = Content(
                role="user",
                parts=[Part(text=user_text)],
            )
            turn_start = time.perf_counter()

            with tracer.start_as_current_span(
                "websocket.chat_turn",
                attributes={
                    "user.id": user.uid,
                    "session.id": sid,
                    "chat.selected_video_count": len(
                        (session.state or {}).get("selected_video_ids", [])
                    ),
                },
            ):
            
                async for ev in runner.run_async(
                    user_id=user.uid,
                    session_id=sid,
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
                    session_id=sid,
                )
                if updated_session:
                    session = updated_session

                # Observability
                log_analytics_event(
                    "AGENT_TURN_METRICS",
                    uid=user.uid,
                    session_id=sid,
                    duration_ms=round((time.perf_counter() - turn_start) * 1000.0, 1),
                )
                
                await _send_ws_frame(
                    websocket,
                    "TURN_COMPLETE",
                    {
                        "session_id": sid,
                        "session": (
                            session_to_chat_summary(session).model_dump(mode="json")
                            if session
                            else None
                        ),
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
            
            # USER_MESSAGE: Start a new conversational turn (lazily creating session on first message)
            if client_msg.type == "USER_MESSAGE":
                text = (client_msg.text or "").strip()
                if not text:
                    continue
                await _stop_active_turn()
                verified_vids = (
                    validate_owned_video_ids(client_msg.selected_video_ids, user.uid)
                    if client_msg.selected_video_ids is not None
                    else []
                )
                
                # Lazy creation: persist the session in Firestore on the very first message
                if session is None or active_session_id is None:
                    cleaned = " ".join(text.split())
                    first_title = (cleaned[:42] + "...") if len(cleaned) > 45 else cleaned
                    initial_state = {
                        "title": first_title or "NEW CLIPPING LOG",
                        "selected_video_ids": verified_vids,
                        "created_at": get_utc_now().isoformat(),
                    }
                    session = await session_service.create_session(
                        app_name=CHAT_APP_NAME,
                        user_id=user.uid,
                        state=initial_state,
                    )
                    active_session_id = session.id
                    await _send_ws_frame(
                        websocket,
                        "SESSION_INIT",
                        {"session": session_to_chat_summary(session).model_dump(mode="json")},
                    )
                
                elif client_msg.selected_video_ids is not None:
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
                    if active_session_id is not None:
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