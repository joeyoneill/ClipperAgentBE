# tests/test_utils.py
# Unit tests for utils/auth.py, utils/lf_videos.py, utils/chat_sessions.py,
# utils/secret_manager.py, and utils/observability.py.

import json
import logging
import os
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from fastapi import HTTPException, WebSocketException
from fastapi.security import HTTPAuthorizationCredentials
from google.adk.events import Event, EventActions
from google.adk.sessions.session import Session
from google.genai.types import Content, FunctionCall, FunctionResponse, Part

from models.chat import ClipCandidate
from tests.conftest import make_doc_snap, mock_db, mock_gcs
from utils.auth import (
    clear_allowlist_cache,
    get_current_user,
    get_ws_current_user,
    verify_firebase_jwt,
)
from utils.chat_sessions import (
    get_verified_adk_session,
    session_to_chat_detail,
    session_to_chat_summary,
    unwrap_tool_response,
    update_session_state_fields,
    validate_owned_video_ids,
)
from utils.lf_videos import (
    generate_video_signed_read_url,
    get_user_video_doc,
    trigger_video_processor_job,
)
from utils.observability import (
    CloudLoggingJsonFormatter,
    get_tracer,
    log_analytics_event,
    setup_observability,
)
from utils.secret_manager import get_secret, load_secrets_into_env


################################################################
# 1. utils/auth.py Tests
################################################################


def test_verify_firebase_jwt_and_http_ws_dependencies() -> None:
    with patch(
        "utils.auth.id_token.verify_firebase_token",
        return_value={"sub": "user_abc", "email": "abc@example.com"},
    ):
        u = verify_firebase_jwt("valid_token")
        assert u.uid == "user_abc"
        assert u.email == "abc@example.com"

        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="valid_token")
        u2 = get_current_user(creds)
        assert u2.uid == "user_abc"

    # Missing sub/user_id raises ValueError / 401
    with patch("utils.auth.id_token.verify_firebase_token", return_value={"email": "x"}):
        with pytest.raises(ValueError, match="missing user identifier"):
            verify_firebase_jwt("bad_claims")

        with pytest.raises(HTTPException) as exc:
            get_current_user(
                HTTPAuthorizationCredentials(scheme="Bearer", credentials="bad_claims")
            )
        assert exc.value.status_code == 401

    # Firestore allowlist enforcement: approved email passes, unapproved gets 403
    clear_allowlist_cache()
    allow_doc_ref = MagicMock()
    allow_doc_ref.get.return_value = make_doc_snap(
        "allowlist",
        {"enabled": True, "emails": ["approved@example.com"]},
        exists=True,
    )
    mock_db.collection.return_value.document.return_value = allow_doc_ref

    with patch(
        "utils.auth.id_token.verify_firebase_token",
        return_value={"sub": "u_ok", "email": "APPROVED@example.com"},
    ):
        u_ok = get_current_user(
            HTTPAuthorizationCredentials(scheme="Bearer", credentials="ok_token")
        )
        assert u_ok.uid == "u_ok"

    with patch(
        "utils.auth.id_token.verify_firebase_token",
        return_value={"sub": "u_blocked", "email": "stranger@example.com"},
    ):
        with pytest.raises(HTTPException) as exc_forbidden:
            get_current_user(
                HTTPAuthorizationCredentials(scheme="Bearer", credentials="blocked_token")
            )
        assert exc_forbidden.value.status_code == 403
    clear_allowlist_cache()


@pytest.mark.asyncio
async def test_get_ws_current_user() -> None:
    ws = MagicMock()
    with pytest.raises(WebSocketException) as exc:
        await get_ws_current_user(ws, token=None)
    assert exc.value.code == 1008

    with patch(
        "utils.auth.id_token.verify_firebase_token",
        side_effect=RuntimeError("Expired JWT"),
    ):
        with pytest.raises(WebSocketException) as exc2:
            await get_ws_current_user(ws, token="expired")
        assert exc2.value.code == 1008

    with patch(
        "utils.auth.id_token.verify_firebase_token",
        return_value={"user_id": "ws_user"},
    ):
        u = await get_ws_current_user(ws, token="good_jwt")
        assert u.uid == "ws_user"


################################################################
# 2. utils/lf_videos.py Tests
################################################################


def test_get_user_video_doc_and_trigger_job() -> None:
    doc_ref = MagicMock()
    doc_ref.get.return_value = make_doc_snap("v1", {"uid": "user_123", "status": "SUCCESSFUL"})
    mock_db.collection.return_value.document.return_value = doc_ref

    ref, data = get_user_video_doc("v1", "user_123")
    assert ref is doc_ref
    assert data["status"] == "SUCCESSFUL"

    # Wrong user -> 403
    with pytest.raises(HTTPException) as exc:
        get_user_video_doc("v1", "intruder")
    assert exc.value.status_code == 403

    # Missing -> 404
    doc_ref.get.return_value = make_doc_snap("v1", None, exists=False)
    with pytest.raises(HTTPException) as exc:
        get_user_video_doc("v1", "user_123")
    assert exc.value.status_code == 404

    # DB Exception -> 500
    doc_ref.get.side_effect = RuntimeError("Firestore error")
    with pytest.raises(HTTPException) as exc:
        get_user_video_doc("v1", "user_123")
    assert exc.value.status_code == 500

    with patch("utils.lf_videos.run_jobs_client.run_job") as mock_run:
        trigger_video_processor_job("v1", "user_123")
        mock_run.assert_called_once()


def test_generate_video_signed_read_url_both_paths() -> None:
    with pytest.raises(ValueError, match="Invalid GCS URI"):
        generate_video_signed_read_url("gs://wrong-bucket/v.mp4")

    mock_blob = MagicMock()
    mock_blob.generate_signed_url.return_value = "https://signed-read.example.com/v.mp4"
    mock_gcs.bucket.return_value.blob.return_value = mock_blob
    bucket = os.environ["GCS_ROOT_BUCKET_NAME"]
    uri = f"gs://{bucket}/user_123/lf_videos/v1/original.mp4"

    # Key-signing credentials path
    creds_key = MagicMock()
    creds_key.sign_bytes = MagicMock()
    with patch("utils.lf_videos.google_auth_default", return_value=(creds_key, "proj")):
        assert generate_video_signed_read_url(uri) == "https://signed-read.example.com/v.mp4"

    # ADC IAM token-refresh path
    creds_adc = MagicMock(spec=["refresh", "token", "service_account_email"])
    creds_adc.token = "tok_123"
    creds_adc.service_account_email = None
    with patch("utils.lf_videos.google_auth_default", return_value=(creds_adc, "proj")):
        assert generate_video_signed_read_url(uri) == "https://signed-read.example.com/v.mp4"


################################################################
# 3. utils/chat_sessions.py Tests
################################################################


def test_unwrap_tool_response_and_session_converters() -> None:
    assert unwrap_tool_response(None) == {}
    cand = ClipCandidate(
        id="c1",
        title="Clip 1",
        video_id="v1",
        video_filename="stream.mp4",
        start_sec=10.0,
        end_sec=30.0,
        duration_sec=20.0,
        transcript_text="Hello world.",
        rationale="Good hook.",
    )
    assert unwrap_tool_response({"result": cand})["id"] == "c1"
    assert unwrap_tool_response({"result": {"k": "v"}}) == {"k": "v"}
    assert unwrap_tool_response({"direct": 1}) == {"direct": 1}

    events = [
        Event(
            id="ev_u1",
            invocation_id="inv1",
            author="user",
            content=Content(role="user", parts=[Part(text="Cut a clip from stream.mp4")]),
            timestamp=1700000000.0,
        ),
        Event(
            id="ev_m1",
            invocation_id="inv1",
            author="orchestrator_agent",
            content=Content(
                role="model",
                parts=[
                    Part(text="Thinking part 1. ", thought=True),
                    Part(text="Thinking part 2.", thought=True),
                    Part(
                        function_call=FunctionCall(
                            id="fc_1",
                            name="propose_clip_candidate",
                            args={"video_id": "v1"},
                        )
                    ),
                ],
            ),
            timestamp=1700000001.0,
        ),
        Event(
            id="ev_m2",
            invocation_id="inv1",
            author="orchestrator_agent",
            content=Content(
                role="model",
                parts=[
                    Part(
                        function_response=FunctionResponse(
                            id="fc_1",
                            name="propose_clip_candidate",
                            response={
                                "summary": {"title": "Clip 1"},
                                "candidate": cand.model_dump(mode="json"),
                            },
                        )
                    ),
                    Part(text="Here is the proposed clip!"),
                ],
            ),
            timestamp=1700000002.0,
        ),
        Event(
            id="ev_abort",
            invocation_id="inv2",
            author="orchestrator_agent",
            error_code="INVOCATION_ABORTED",
            actions=EventActions(state_delta={"last_turn_interrupted": True}),
            timestamp=1700000003.0,
        ),
    ]

    session = Session(
        id="sess_1",
        app_name="clipping_studio",
        user_id="user_123",
        state={
            "title": "My Session",
            "selected_video_ids": ["v1"],
            "created_at": "2026-10-15T00:00:00+00:00",
        },
        events=events,
        last_update_time=1700000005.0,
    )

    summary = session_to_chat_summary(session)
    assert summary.title == "My Session"
    assert summary.selected_video_ids == ["v1"]

    bucket = os.environ["GCS_ROOT_BUCKET_NAME"]
    mock_db.get_all.return_value = [
        make_doc_snap(
            "v1",
            {
                "uid": "user_123",
                "gcs_uri": f"gs://{bucket}/user_123/lf_videos/v1/original.mp4",
            },
        )
    ]
    with patch(
        "utils.chat_sessions.generate_video_signed_read_url",
        return_value="https://signed-preview.example.com/v1.mp4",
    ):
        detail = session_to_chat_detail(session)

    assert len(detail.messages) == 2
    assert detail.messages[0].role == "user"
    model_msg = detail.messages[1]
    assert model_msg.role == "model"
    assert model_msg.interrupted is True
    assert len(model_msg.trace) == 3
    assert model_msg.trace[0].content == "Thinking part 1. Thinking part 2."
    assert len(model_msg.clip_candidates) == 1
    assert (
        model_msg.clip_candidates[0].preview_url
        == "https://signed-preview.example.com/v1.mp4"
    )


@pytest.mark.asyncio
async def test_session_helpers_and_validate_owned_video_ids() -> None:
    session = Session(
        id="sess_1",
        app_name="clipping_studio",
        user_id="user_123",
        state={},
        events=[],
    )
    with patch(
        "utils.chat_sessions.session_service.append_event", new_callable=AsyncMock
    ) as mock_append:
        await update_session_state_fields(session, {"title": "Updated"})
        mock_append.assert_called_once()

    with patch(
        "utils.chat_sessions.session_service.get_session",
        new_callable=AsyncMock,
        return_value=session,
    ):
        res = await get_verified_adk_session("sess_1", "user_123", include_events=False)
        assert res.id == "sess_1"

    with patch(
        "utils.chat_sessions.session_service.get_session",
        new_callable=AsyncMock,
        return_value=None,
    ):
        with pytest.raises(HTTPException) as exc:
            await get_verified_adk_session("missing", "user_123")
        assert exc.value.status_code == 404

    assert validate_owned_video_ids([], "user_123") == []

    mock_db.get_all.return_value = [
        make_doc_snap("v1", {"uid": "user_123", "status": "SUCCESSFUL"})
    ]
    assert validate_owned_video_ids(["v1", "v1"], "user_123") == ["v1"]

    # Non-existent video -> 404
    mock_db.get_all.return_value = [make_doc_snap("v1", None, exists=False)]
    with pytest.raises(HTTPException) as exc:
        validate_owned_video_ids(["v1"], "user_123")
    assert exc.value.status_code == 404

    # Wrong owner -> 403
    mock_db.get_all.return_value = [
        make_doc_snap("v1", {"uid": "other", "status": "SUCCESSFUL"})
    ]
    with pytest.raises(HTTPException) as exc:
        validate_owned_video_ids(["v1"], "user_123")
    assert exc.value.status_code == 403

    # Not SUCCESSFUL -> 400
    mock_db.get_all.return_value = [
        make_doc_snap("v1", {"uid": "user_123", "status": "PROCESSING"})
    ]
    with pytest.raises(HTTPException) as exc:
        validate_owned_video_ids(["v1"], "user_123")
    assert exc.value.status_code == 400


################################################################
# 4. utils/secret_manager.py & utils/observability.py Tests
################################################################


def test_secret_manager_and_observability() -> None:
    get_secret.cache_clear()
    mock_sm_client = MagicMock()
    mock_sm_client.access_secret_version.return_value.payload.data = b"secret-from-gcp\n"
    with patch(
        "utils.secret_manager.secretmanager.SecretManagerServiceClient",
        return_value=mock_sm_client,
    ):
        val = get_secret("MY_TEST_SECRET")
        assert val == "secret-from-gcp"

    get_secret.cache_clear()
    with patch(
        "utils.secret_manager.secretmanager.SecretManagerServiceClient",
        side_effect=RuntimeError("Offline"),
    ):
        os.environ["FALLBACK_KEY"] = "env-value"
        assert get_secret("FALLBACK_KEY") == "env-value"
        load_secrets_into_env(["FALLBACK_KEY"])
        assert os.environ["FALLBACK_KEY"] == "env-value"

    # CloudLoggingJsonFormatter + log_analytics_event
    formatter = CloudLoggingJsonFormatter(project_id=os.environ["GCP_PROJECT_ID"])
    record = logging.LogRecord(
        name="clipper.analytics",
        level=logging.INFO,
        pathname=__file__,
        lineno=10,
        msg="Test message",
        args=(),
        exc_info=None,
    )
    setattr(record, "telemetry", {"event_type": "SF_EXPORT_VARIANT", "cached": True})
    formatted = json.loads(formatter.format(record))
    assert formatted["severity"] == "INFO"
    assert formatted["event_type"] == "SF_EXPORT_VARIANT"
    assert formatted["telemetry"]["cached"] is True

    log_analytics_event("TEST_METRIC", latency_ms=42.5)
    tracer = get_tracer("test.tracer")
    assert tracer is not None

    # Test setup_observability with JSON_LOGS=true and mocked CloudTraceSpanExporter
    import utils.observability as obs_mod

    obs_mod._initialized = False
    with (
        patch.dict(os.environ, {"JSON_LOGS": "true", "ENABLE_CLOUD_TRACE": "true"}),
        patch("utils.observability.TraceServiceClient", return_value=MagicMock()),
        patch("utils.observability.CloudTraceSpanExporter", return_value=MagicMock()),
        patch("utils.observability.BatchSpanProcessor", return_value=MagicMock()),
        patch("utils.observability.get_aggregated_resources", return_value=MagicMock()),
    ):
        setup_observability("test-service")
        assert obs_mod._initialized is True
