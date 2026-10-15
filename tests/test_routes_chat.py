# tests/test_routes_chat.py
# Integration tests for routes/chat/chat_sessions.py and WebSocket routes/chat/stream.py

from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient
from google.adk.events import Event
from google.adk.sessions.session import Session
from google.genai.types import Content, FunctionCall, FunctionResponse, Part

from models.chat import ClipCandidate
from utils.auth import UserInfo


def _make_session(
    session_id: str = "sess_1",
    uid: str = "user_123",
    title: str = "NEW CLIPPING LOG",
    selected_video_ids: list[str] | None = None,
) -> Session:
    return Session(
        id=session_id,
        app_name="clipping_studio",
        user_id=uid,
        state={
            "title": title,
            "selected_video_ids": selected_video_ids or [],
            "created_at": "2026-10-15T00:00:00+00:00",
        },
        events=[],
        last_update_time=1700000000.0,
    )


################################################################
# REST Endpoints: /api/chat/sessions
################################################################


def test_chat_sessions_rest_crud(client: TestClient) -> None:
    s1 = _make_session("s1", title="Log 1")
    s2 = _make_session("s2", title="Log 2")
    mock_list_resp = MagicMock()
    mock_list_resp.sessions = [s1, s2]

    with patch(
        "routes.chat.chat_sessions.session_service.list_sessions",
        new_callable=AsyncMock,
        return_value=mock_list_resp,
    ):
        resp = client.get("/api/chat/sessions")
        assert resp.status_code == 200
        # Reversed newest-first order
        assert [x["id"] for x in resp.json()] == ["s2", "s1"]

    with (
        patch("routes.chat.chat_sessions.validate_owned_video_ids", return_value=["v1"]),
        patch(
            "routes.chat.chat_sessions.session_service.create_session",
            new_callable=AsyncMock,
            return_value=_make_session("s_created", title="Custom Title", selected_video_ids=["v1"]),
        ),
    ):
        create_resp = client.post(
            "/api/chat/sessions",
            json={"title": "Custom Title", "selected_video_ids": ["v1"]},
        )
        assert create_resp.status_code == 201
        assert create_resp.json()["id"] == "s_created"

    with patch(
        "routes.chat.chat_sessions.get_verified_adk_session",
        new_callable=AsyncMock,
        return_value=s1,
    ):
        detail_resp = client.get("/api/chat/sessions/s1")
        assert detail_resp.status_code == 200
        assert detail_resp.json()["id"] == "s1"

    with (
        patch(
            "routes.chat.chat_sessions.get_verified_adk_session",
            new_callable=AsyncMock,
            return_value=s1,
        ),
        patch("routes.chat.chat_sessions.validate_owned_video_ids", return_value=["v2"]),
        patch(
            "routes.chat.chat_sessions.update_session_state_fields",
            new_callable=AsyncMock,
            return_value=_make_session("s1", title="Renamed", selected_video_ids=["v2"]),
        ),
    ):
        patch_resp = client.patch(
            "/api/chat/sessions/s1",
            json={"title": "Renamed", "selected_video_ids": ["v2"]},
        )
        assert patch_resp.status_code == 200
        assert patch_resp.json()["title"] == "Renamed"

    with (
        patch(
            "routes.chat.chat_sessions.get_verified_adk_session",
            new_callable=AsyncMock,
            return_value=s1,
        ),
        patch(
            "routes.chat.chat_sessions.session_service.delete_session",
            new_callable=AsyncMock,
        ) as mock_del,
    ):
        del_resp = client.delete("/api/chat/sessions/s1")
        assert del_resp.status_code == 204
        mock_del.assert_awaited_once()


################################################################
# WebSocket Endpoint: /api/chat/stream
################################################################


def test_websocket_auth_failure_closes_connection(client: TestClient) -> None:
    with client.websocket_connect("/api/chat/stream") as ws:
        ws.send_json({"type": "USER_MESSAGE", "text": "No auth first"})
        err_frame = ws.receive_json()
        assert err_frame["type"] == "ERROR"
        assert "First WebSocket frame must be an AUTH message" in err_frame["data"]["message"]


def test_websocket_full_turn_and_controls(client: TestClient, mock_user: UserInfo) -> None:
    created_session = _make_session("sess_ws_1", title="Find the best moment")
    cand = ClipCandidate(
        id="cand_ws_1",
        title="Viral Highlight",
        video_id="v1",
        video_filename="stream.mp4",
        start_sec=30.0,
        end_sec=55.0,
        duration_sec=25.0,
        transcript_text="That was an unbelievable play!",
        rationale="Captures the peak reaction cleanly.",
    )

    async def fake_run_async(*args: object, **kwargs: object) -> AsyncGenerator[Event, None]:
        # 1. Partial thought delta
        yield Event(
            invocation_id="inv_1",
            author="orchestrator_agent",
            partial=True,
            content=Content(role="model", parts=[Part(text="Searching...", thought=True)]),
        )
        # 2. Partial visible text delta
        yield Event(
            invocation_id="inv_1",
            author="orchestrator_agent",
            partial=True,
            content=Content(role="model", parts=[Part(text="Here is your clip!")]),
        )
        # 3. Committed tool call + tool result with ClipCandidate
        yield Event(
            invocation_id="inv_1",
            author="clip_editor_agent",
            partial=False,
            content=Content(
                role="model",
                parts=[
                    Part(
                        function_call=FunctionCall(
                            id="fc_1",
                            name="propose_clip_candidate",
                            args={"video_id": "v1"},
                        )
                    ),
                    Part(
                        function_response=FunctionResponse(
                            id="fc_1",
                            name="propose_clip_candidate",
                            response={
                                "summary": {"title": "Viral Highlight"},
                                "candidate": cand.model_dump(mode="json"),
                            },
                        )
                    ),
                ],
            ),
        )

    with (
        patch("routes.chat.stream.verify_firebase_jwt", return_value=mock_user),
        patch(
            "routes.chat.stream.session_service.create_session",
            new_callable=AsyncMock,
            return_value=created_session,
        ),
        patch(
            "routes.chat.stream.session_service.get_session",
            new_callable=AsyncMock,
            return_value=created_session,
        ),
        patch("routes.chat.stream.validate_owned_video_ids", return_value=["v1"]),
        patch(
            "routes.chat.stream.update_session_state_fields",
            new_callable=AsyncMock,
            return_value=created_session,
        ),
        patch("routes.chat.stream.runner.run_async", side_effect=fake_run_async),
    ):
        with client.websocket_connect("/api/chat/stream") as ws:
            # 1. First-frame AUTH handshake (draft mode -> empty SESSION_INIT)
            ws.send_json({"type": "AUTH", "token": "valid_jwt"})
            init_frame = ws.receive_json()
            assert init_frame["type"] == "SESSION_INIT"
            assert init_frame["data"] == {}

            # 2. Send first USER_MESSAGE -> lazy session creation + streamed frames
            ws.send_json({"type": "USER_MESSAGE", "text": "Find the best moment"})
            received_types: list[str] = []
            while True:
                frame = ws.receive_json()
                received_types.append(frame["type"])
                if frame["type"] == "TURN_COMPLETE":
                    break

            assert "SESSION_INIT" in received_types
            assert "AGENT_THOUGHT" in received_types
            assert "AGENT_TOKEN" in received_types
            assert "TRACE_STEP" in received_types
            assert "CLIP_CANDIDATE" in received_types
            assert "TURN_COMPLETE" in received_types

            # 3. Test SET_VIDEOS control
            ws.send_json({"type": "SET_VIDEOS", "selected_video_ids": ["v1"]})
            vids_frame = ws.receive_json()
            assert vids_frame["type"] == "VIDEOS_UPDATED"
            assert vids_frame["data"]["selected_video_ids"] == ["v1"]

            # 4. Test INTERRUPT control
            ws.send_json({"type": "INTERRUPT"})
            int_frame = ws.receive_json()
            assert int_frame["type"] == "TURN_INTERRUPTED"

            # 5. Test ADD_CONTEXT steering control
            ws.send_json({"type": "ADD_CONTEXT", "text": "Focus on the funny reaction"})
            ctx_frame = ws.receive_json()
            assert ctx_frame["type"] == "CONTEXT_ADDED"
            while True:
                f = ws.receive_json()
                if f["type"] == "TURN_COMPLETE":
                    break

            # 6. Invalid client message type returns ERROR frame without disconnecting
            ws.send_json({"type": "NOT_A_VALID_TYPE"})
            bad_frame = ws.receive_json()
            assert bad_frame["type"] == "ERROR"

