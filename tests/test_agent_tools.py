# tests/test_agent_tools.py
# Unit tests for ADK tools in agent/tools/ and agent configuration in agent/agent.py

import os
from unittest.mock import MagicMock, patch
import pytest

from agent.agent import (
    ClipEditorInput,
    _build_plugins,
    clip_editor_agent,
    clipping_app,
    orchestrator_agent,
)
from agent.tools.get_expanded_window import get_expanded_segment_window
from agent.tools.helpers import (
    embed_query_text,
    get_allowed_user_videos,
    parse_word_timestamps,
)
from agent.tools.list_available_videos import list_available_videos
from agent.tools.propose_clip_candidate import propose_clip_candidate
from agent.tools.search_video_segments import search_video_segments
from models.misc import WordTimestamp
from tests.conftest import make_doc_snap, make_tool_context, mock_db


################################################################
# 1. agent/tools/helpers.py Unit Tests
################################################################


def test_parse_word_timestamps_and_embed_query_text() -> None:
    raw = [
        WordTimestamp(word="one", start_sec=0.0, end_sec=0.5),
        {"word": "two", "start_sec": 0.6, "end_sec": 1.1},
        {"word": "three", "start": 1.2, "end": 1.8},
    ]
    parsed = parse_word_timestamps(raw)
    assert [w.word for w in parsed] == ["one", "two", "three"]
    assert parsed[2].start_sec == 1.2
    assert parsed[2].end_sec == 1.8

    mock_emb = MagicMock()
    mock_emb.values = [0.125] * 1408
    mock_resp = MagicMock()
    mock_resp.embeddings = [mock_emb]
    with patch("agent.tools.helpers.genai_client.models.embed_content", return_value=mock_resp):
        vec = embed_query_text("boss fight")
        assert len(vec) == 1408

    mock_empty = MagicMock(embeddings=[])
    with patch("agent.tools.helpers.genai_client.models.embed_content", return_value=mock_empty):
        with pytest.raises(RuntimeError, match="Failed to generate query embedding"):
            embed_query_text("fail")


def test_get_allowed_user_videos_respects_pinned_scope() -> None:
    snap1 = make_doc_snap(
        "v1", {"uid": "user_123", "filename": "one.mp4", "status": "SUCCESSFUL"}
    )
    snap2 = make_doc_snap(
        "v2", {"uid": "user_123", "filename": "two.mp4", "status": "SUCCESSFUL"}
    )
    mock_db.collection.return_value.where.return_value.where.return_value.stream.return_value = [
        snap1,
        snap2,
    ]

    # All vault tapes when selected_video_ids is empty
    ctx_all = make_tool_context("user_123", {"selected_video_ids": []})
    all_vids = get_allowed_user_videos(ctx_all)
    assert set(all_vids.keys()) == {"v1", "v2"}

    # Filtered when selected_video_ids pins ["v2"]
    ctx_pinned = make_tool_context("user_123", {"selected_video_ids": ["v2"]})
    pinned_vids = get_allowed_user_videos(ctx_pinned)
    assert set(pinned_vids.keys()) == {"v2"}


################################################################
# 2. agent/tools/list_available_videos.py Unit Tests
################################################################


def test_list_available_videos_tool() -> None:
    snap1 = make_doc_snap(
        "v1",
        {
            "uid": "user_123",
            "filename": "keynote.mp4",
            "duration_seconds": 600,
            "transcript": "A" * 450,
            "status": "SUCCESSFUL",
        },
    )
    mock_db.collection.return_value.where.return_value.where.return_value.stream.return_value = [
        snap1
    ]

    ctx = make_tool_context("user_123", {"selected_video_ids": ["v1"]})
    resp = list_available_videos(ctx)
    assert resp.scope == "PINNED_TAPES"
    assert resp.summary.video_count == 1
    assert resp.videos[0].video_id == "v1"
    assert len(resp.videos[0].transcript_preview) == 300


################################################################
# 3. agent/tools/search_video_segments.py Unit Tests
################################################################


def test_search_video_segments_hybrid_rrf_and_empty_scope() -> None:
    # Empty vault case
    mock_db.collection.return_value.where.return_value.where.return_value.stream.return_value = []
    ctx = make_tool_context("user_123", {})
    empty_resp = search_video_segments("find intro", ctx)
    assert empty_resp.summary.matches_found == 0
    assert empty_resp.summary.reason == "No completed videos in active scope."

    # Active vault with hybrid RRF fusion across text_embedding and video_embedding
    allowed_map = {"v1": {"id": "v1", "uid": "user_123", "filename": "stream.mp4"}}
    seg_a = make_doc_snap(
        "v1_seg_0001",
        {
            "video_id": "v1",
            "uid": "user_123",
            "segment_index": 1,
            "start_sec": 30.0,
            "end_sec": 60.0,
            "transcript_text": "Here is the final boss strategy.",
        },
    )
    seg_b = make_doc_snap(
        "v1_seg_0002",
        {
            "video_id": "v1",
            "uid": "user_123",
            "segment_index": 2,
            "start_sec": 60.0,
            "end_sec": 90.0,
            "transcript_text": "Visual fireworks on screen.",
        },
    )

    mock_vec_query = MagicMock()
    mock_vec_query.get.return_value = [seg_a, seg_b]
    mock_db.collection.return_value.where.return_value.find_nearest.return_value = (
        mock_vec_query
    )

    with (
        patch("agent.tools.search_video_segments.get_allowed_user_videos", return_value=allowed_map),
        patch("agent.tools.search_video_segments.embed_query_text", return_value=[0.1] * 1408),
    ):
        resp = search_video_segments(
            query="final boss strategy",
            tool_context=ctx,
            search_mode="hybrid",
            top_k=5,
        )

    assert resp.summary.matches_found == 2
    top_match = resp.matches[0]
    assert top_match.segment_doc_id == "v1_seg_0001"
    assert top_match.matched_on == ["transcript", "visual"]
    assert top_match.rrf_score == pytest.approx(round(2.0 / 61.0, 5))


################################################################
# 4. agent/tools/get_expanded_window.py Unit Tests
################################################################


def test_get_expanded_segment_window_tool() -> None:
    ctx = make_tool_context("user_123", {})
    allowed_map = {"v1": {"id": "v1", "uid": "user_123", "filename": "podcast.mp4"}}

    with patch("agent.tools.get_expanded_window.get_allowed_user_videos", return_value=allowed_map):
        unauth = get_expanded_segment_window("v_unauthorized", ctx, center_segment_index=2)
        assert unauth.error is not None

    seg_snaps = [
        make_doc_snap(
            f"v1_seg_{idx:04d}",
            {
                "uid": "user_123",
                "video_id": "v1",
                "segment_index": idx,
                "start_sec": idx * 30.0,
                "end_sec": (idx + 1) * 30.0,
                "transcript_text": f"Segment {idx} sentence.",
                "words": [
                    {
                        "word": f"Segment_{idx}",
                        "start_sec": idx * 30.0 + 1.0,
                        "end_sec": idx * 30.0 + 2.0,
                    }
                ],
            },
        )
        for idx in (1, 2, 3)
    ]
    mock_db.get_all.return_value = seg_snaps

    with patch("agent.tools.get_expanded_window.get_allowed_user_videos", return_value=allowed_map):
        res = get_expanded_segment_window(
            video_id="v1",
            tool_context=ctx,
            center_segment_index=2,
            pad_before=2,
            pad_after=2,
        )

    assert res.error is None
    assert res.window_start_sec == 30.0
    assert res.window_end_sec == 120.0
    assert res.summary.segments_loaded == 3
    assert res.summary.word_count == 3
    assert "[31.00-32.00] Segment_1" in (res.word_level_timeline or "")


################################################################
# 5. agent/tools/propose_clip_candidate.py & agent/agent.py Unit Tests
################################################################


def test_propose_clip_candidate_tool_and_agent_config() -> None:
    ctx = make_tool_context("user_123", {})
    bucket = os.environ["GCS_ROOT_BUCKET_NAME"]
    allowed_map = {
        "v1": {
            "id": "v1",
            "uid": "user_123",
            "filename": "stream.mp4",
            "gcs_uri": f"gs://{bucket}/user_123/lf_videos/v1/original.mp4",
        }
    }

    with patch("agent.tools.propose_clip_candidate.get_allowed_user_videos", return_value=allowed_map):
        bad = propose_clip_candidate(
            video_id="v1",
            title="Bad Clip",
            start_sec=50.0,
            end_sec=40.0,
            rationale="Invalid",
            tool_context=ctx,
        )
        assert bad.error is not None
        assert bad.candidate is None

        seg_snap = make_doc_snap(
            "v1_seg_0001",
            {
                "uid": "user_123",
                "video_id": "v1",
                "segment_index": 1,
                "start_sec": 30.0,
                "end_sec": 60.0,
                "transcript_text": "Ignored fallback text",
                "words": [
                    {"word": "Before", "start_sec": 31.0, "end_sec": 31.5},
                    {"word": "Hook", "start_sec": 35.0, "end_sec": 35.6},
                    {"word": "sentence", "start_sec": 35.8, "end_sec": 36.4},
                    {"word": "ends.", "start_sec": 36.5, "end_sec": 37.2},
                    {"word": "After", "start_sec": 52.0, "end_sec": 52.5},
                ],
            },
        )
        mock_db.get_all.return_value = [seg_snap]

        with patch(
            "agent.tools.propose_clip_candidate.generate_video_signed_read_url",
            return_value="https://signed-preview.example.com/v1.mp4",
        ):
            good = propose_clip_candidate(
                video_id="v1",
                title="  Clean Hook Clip  ",
                start_sec=35.0,
                end_sec=37.2,
                rationale="Starts cleanly on 'Hook' and ends on 'ends.'",
                tool_context=ctx,
            )

    assert good.error is None
    assert good.candidate is not None
    assert good.candidate.title == "Clean Hook Clip"
    assert good.candidate.duration_sec == 2.2
    assert good.candidate.transcript_text == "Hook sentence ends."
    assert len(good.candidate.words) == 3
    assert good.candidate.preview_url == "https://signed-preview.example.com/v1.mp4"

    # Verify ADK agent & plugin setup
    assert orchestrator_agent.name == "orchestrator_agent"
    assert clip_editor_agent.name == "clip_editor_agent"
    assert clip_editor_agent.input_schema is ClipEditorInput
    assert clipping_app.name == "clipping_studio"
    with patch.dict(os.environ, {"MODEL_ARMOR_TEMPLATE_NAME": ""}):
        assert _build_plugins() == []
