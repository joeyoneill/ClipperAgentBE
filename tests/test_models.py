# tests/test_models.py
# Unit tests for all Pydantic models across models/misc.py, models/lf_videos.py,
# models/sf_videos.py, models/chat.py, and agent/agent.py

from datetime import datetime, timezone
from typing import Literal
import pytest
from pydantic import ValidationError

from agent.agent import ClipEditorInput
from models.chat import (
    AgentTraceStep,
    ChatMessageModel,
    ChatRequest,
    ChatSessionDetail,
    ChatSessionSummary,
    ClipCandidate,
    CreateChatSessionRequest,
    UpdateChatSessionRequest,
    WSClientMessage,
    WSServerMessage,
)
from models.lf_videos import (
    LFUploadFailedRequest,
    LFUploadRequest,
    LFUploadResponse,
    LFVideo,
    LFVideoSegment,
)
from models.misc import WordTimestamp
from models.sf_videos import (
    DownloadSFVideoRequest,
    DownloadSFVideoResponse,
    SaveClipRequest,
    SFVideo,
    SFVideoResponse,
    UpdateSFVideoRequest,
)


################################################################
# models/misc.py Tests
################################################################


def test_word_timestamp_valid_and_coercion() -> None:
    wt = WordTimestamp.model_validate({"word": "hello", "start_sec": "1.25", "end_sec": 2})
    assert wt.word == "hello"
    assert wt.start_sec == 1.25
    assert wt.end_sec == 2.0


def test_word_timestamp_missing_required() -> None:
    with pytest.raises(ValidationError):
        WordTimestamp.model_validate({"word": "hello", "start_sec": 1.0})


################################################################
# models/lf_videos.py Tests
################################################################


def test_lf_video_valid_states_and_defaults() -> None:
    valid_statuses: tuple[
        Literal["PENDING", "UPLOADING", "PROCESSING", "SUCCESSFUL", "FAILED"], ...
    ] = ("PENDING", "UPLOADING", "PROCESSING", "SUCCESSFUL", "FAILED")
    for status_val in valid_statuses:
        vid = LFVideo(
            uid="u1",
            filename="stream.mp4",
            gcs_uri="gs://bucket/u1/lf_videos/v1/original.mp4",
            status=status_val,
        )
        assert vid.status == status_val
        assert vid.is_complete is False
        assert vid.id is None
        assert isinstance(vid.created_at, datetime)
        assert vid.created_at.tzinfo == timezone.utc


def test_lf_video_invalid_status_rejected() -> None:
    with pytest.raises(ValidationError):
        LFVideo.model_validate(
            {
                "uid": "u1",
                "filename": "stream.mp4",
                "gcs_uri": "gs://bucket/path.mp4",
                "status": "INVALID_STATUS",
            }
        )


def test_lf_upload_request_and_response() -> None:
    req = LFUploadRequest(
        filename="gameplay.mp4",
        content_type="video/mp4",
        file_size_bytes=1048576,
        duration_seconds=300,
    )
    assert req.filename == "gameplay.mp4"
    assert req.file_size_bytes == 1048576

    resp = LFUploadResponse(
        video_id="vid_1",
        upload_url="https://storage.googleapis.com/upload/session",
        gcs_uri="gs://b/u/lf_videos/vid_1/original.mp4",
        blob_path="u/lf_videos/vid_1/original.mp4",
    )
    assert resp.video_id == "vid_1"


def test_lf_upload_failed_request_default() -> None:
    req = LFUploadFailedRequest()
    assert req.error_msg == "Upload failed or was aborted by client."
    custom = LFUploadFailedRequest(error_msg="Network disconnected")
    assert custom.error_msg == "Network disconnected"


def test_lf_video_segment_model() -> None:
    vec = [0.01] * 1408
    seg = LFVideoSegment(
        id="v1_seg_0000",
        video_id="v1",
        uid="u1",
        segment_index=0,
        start_sec=0.0,
        end_sec=30.0,
        transcript_text="Welcome back to the stream.",
        words=[WordTimestamp(word="Welcome", start_sec=0.1, end_sec=0.5)],
        text_embedding=vec,
        video_embedding=vec,
    )
    assert seg.segment_index == 0
    assert len(seg.words) == 1
    assert len(seg.text_embedding or []) == 1408


################################################################
# models/sf_videos.py Tests
################################################################


def test_sf_video_and_response_models() -> None:
    sf = SFVideo(
        id="sf_1",
        uid="u1",
        source_video_id="v1",
        source_filename="vod.mp4",
        title="Boss Fight Highlight",
        start_sec=10.0,
        end_sec=35.0,
        duration_seconds=25.0,
        transcript_text="We finally beat the boss!",
    )
    assert sf.status == "PENDING_CUT"
    assert sf.rendered_variants == {}

    sf_resp = SFVideoResponse(
        **sf.model_dump(),
        preview_url="https://signed.example.com/vod.mp4",
    )
    assert sf_resp.preview_url == "https://signed.example.com/vod.mp4"


def test_save_update_and_download_sf_video_models() -> None:
    save_req = SaveClipRequest(
        candidate_id="cand_1",
        video_id="v1",
        session_id="sess_1",
        title="Epic Clutch",
        start_sec=12.0,
        end_sec=40.0,
        transcript_text="Look at that clutch play.",
        rationale="Strong opening hook and clean resolution.",
    )
    assert save_req.candidate_id == "cand_1"

    upd_req = UpdateSFVideoRequest(title="New Title", transcript_text="Updated text")
    assert upd_req.words is None

    dl_default = DownloadSFVideoRequest()
    assert dl_default.aspect_ratio == "original"
    assert dl_default.resolution == "1080p"
    assert dl_default.subtitles is False

    dl_custom = DownloadSFVideoRequest(
        aspect_ratio="9:16_crop",
        resolution="720p",
        subtitles=True,
    )
    assert dl_custom.aspect_ratio == "9:16_crop"

    with pytest.raises(ValidationError):
        DownloadSFVideoRequest.model_validate({"aspect_ratio": "4:3_invalid"})

    with pytest.raises(ValidationError):
        DownloadSFVideoRequest.model_validate({"resolution": "4k_invalid"})

    dl_resp = DownloadSFVideoResponse(
        download_url="https://signed-dl.example.com/clip.mp4",
        filename="Epic_Clutch_9:16_crop_720p_cc.mp4",
        variant_key="9:16_crop__720p__subs_on",
        cached=True,
    )
    assert dl_resp.cached is True


################################################################
# models/chat.py & agent/agent.py Tests
################################################################


def test_chat_and_websocket_models() -> None:
    cand = ClipCandidate(
        id="c1",
        title="Funny Intro",
        video_id="v1",
        video_filename="podcast.mp4",
        start_sec=5.0,
        end_sec=25.0,
        duration_sec=20.0,
        transcript_text="Hello everyone and welcome.",
        rationale="Great intro hook.",
    )
    step = AgentTraceStep(
        id="s1",
        step_type="TOOL_CALL",
        title="CALLING search_video_segments",
        tool_name="search_video_segments",
        tool_args={"query": "intro"},
    )
    msg = ChatMessageModel(
        id="m1",
        role="model",
        text="Here is your clip.",
        trace=[step],
        clip_candidates=[cand],
    )
    detail = ChatSessionDetail(
        id="sess_1",
        uid="u1",
        title="Podcast Clips",
        selected_video_ids=["v1"],
        messages=[msg],
    )
    assert isinstance(detail, ChatSessionSummary)
    assert len(detail.messages) == 1
    assert detail.messages[0].clip_candidates[0].title == "Funny Intro"

    chat_req = ChatRequest(user_id="u1", session_id="s1", query="hi")
    create_req = CreateChatSessionRequest(title="My Log", selected_video_ids=["v1"])
    update_req = UpdateChatSessionRequest(title="Renamed Log")
    assert chat_req.query == "hi"
    assert create_req.selected_video_ids == ["v1"]
    assert update_req.title == "Renamed Log"

    ws_in = WSClientMessage(type="USER_MESSAGE", text="Find a funny clip")
    assert ws_in.type == "USER_MESSAGE"

    with pytest.raises(ValidationError):
        WSClientMessage.model_validate({"type": "UNKNOWN_FRAME"})

    ws_out = WSServerMessage(type="TURN_COMPLETE", data={"session_id": "s1"})
    assert ws_out.type == "TURN_COMPLETE"

    sub_in = ClipEditorInput(
        video_id="v1",
        center_segment_index=3,
        clip_goal="Cut a 30s highlight about the tournament win.",
    )
    assert sub_in.center_segment_index == 3
