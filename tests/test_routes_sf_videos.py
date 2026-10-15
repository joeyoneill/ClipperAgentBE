# tests/test_routes_sf_videos.py
# Integration tests for routes/sf_videos/save.py and routes/sf_videos/manage.py

import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient

from tests.conftest import make_doc_snap, mock_db, mock_gcs

BUCKET = os.environ["GCS_ROOT_BUCKET_NAME"]
SF_COL_NAME = os.environ["SFVIDEO_COLLECTION_NAME"]


################################################################
# POST /api/sf_videos/save
################################################################


def test_save_clip_to_reel_new_idempotent_and_validation(client: TestClient) -> None:
    lf_doc_ref = MagicMock()
    lf_doc_ref.get.return_value = make_doc_snap(
        "v1",
        {
            "uid": "user_123",
            "filename": "stream.mp4",
            "status": "SUCCESSFUL",
        },
    )
    sf_col = MagicMock()
    sf_col.where.return_value.where.return_value.limit.return_value.stream.return_value = []
    new_sf_doc = MagicMock()
    new_sf_doc.id = "sf_new_1"
    sf_col.document.return_value = new_sf_doc

    def col_router(name: str) -> MagicMock:
        return sf_col if name == SF_COL_NAME else MagicMock(document=lambda _: lf_doc_ref)

    mock_db.collection.side_effect = col_router

    payload = {
        "candidate_id": "cand_abc",
        "video_id": "v1",
        "session_id": "sess_1",
        "title": "  Winning Moment  ",
        "start_sec": 12.0,
        "end_sec": 32.0,
        "transcript_text": "We just won the finals!",
        "words": [{"word": "We", "start_sec": 12.0, "end_sec": 12.4}],
        "rationale": "Strong emotional hook.",
    }
    resp = client.post("/api/sf_videos/save", json=payload)
    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] == "sf_new_1"
    assert body["title"] == "Winning Moment"
    assert body["duration_seconds"] == 20.0
    assert body["status"] == "PENDING_CUT"
    new_sf_doc.set.assert_called_once()

    # Idempotent repeat save returns existing SFVideo without creating duplicate
    stored_dict = {k: v for k, v in body.items() if k != "id"}
    existing_snap = make_doc_snap("sf_existing_99", stored_dict)
    sf_col.where.return_value.where.return_value.limit.return_value.stream.return_value = [
        existing_snap
    ]
    resp_idem = client.post("/api/sf_videos/save", json=payload)
    assert resp_idem.status_code == 201
    assert resp_idem.json()["id"] == "sf_existing_99"

    # Invalid start_sec >= end_sec -> 400
    bad_bounds = client.post(
        "/api/sf_videos/save",
        json={**payload, "start_sec": 40.0, "end_sec": 20.0},
    )
    assert bad_bounds.status_code == 400

    # Parent LFVideo not SUCCESSFUL -> 400
    lf_doc_ref.get.return_value = make_doc_snap(
        "v1", {"uid": "user_123", "status": "PROCESSING"}
    )
    not_ready = client.post("/api/sf_videos/save", json=payload)
    assert not_ready.status_code == 400


################################################################
# GET /api/sf_videos/list, PATCH /api/sf_videos/{sf_id}, DELETE /api/sf_videos/{sf_id}
################################################################


def test_list_patch_and_delete_sf_videos(client: TestClient) -> None:
    now = datetime.now(timezone.utc)
    base_sf_data = {
        "uid": "user_123",
        "source_video_id": "v1",
        "source_filename": "stream.mp4",
        "title": "Original Title",
        "start_sec": 10.0,
        "end_sec": 20.0,
        "duration_seconds": 10.0,
        "transcript_text": "hello world",
        "words": [
            {"word": "hello", "start_sec": 10.0, "end_sec": 11.0},
            {"word": "world", "start_sec": 11.5, "end_sec": 12.5},
        ],
        "rendered_variants": {
            "9:16_crop__720p__subs_on": f"gs://{BUCKET}/user_123/sf_videos/sf_1/sub.mp4",
            "9:16_crop__720p__subs_off": f"gs://{BUCKET}/user_123/sf_videos/sf_1/nosub.mp4",
        },
        "status": "SUCCESSFUL",
        "is_complete": True,
        "created_at": now,
        "updated_at": now,
    }
    sf_snap = make_doc_snap("sf_1", dict(base_sf_data))
    mock_db.collection.return_value.where.return_value.order_by.return_value.stream.return_value = [
        sf_snap
    ]
    mock_db.get_all.return_value = [
        make_doc_snap(
            "v1",
            {
                "uid": "user_123",
                "gcs_uri": f"gs://{BUCKET}/user_123/lf_videos/v1/original.mp4",
            },
        )
    ]

    with patch(
        "routes.sf_videos.manage.generate_video_signed_read_url",
        return_value="https://signed-preview.example.com/v1.mp4",
    ):
        list_resp = client.get("/api/sf_videos/list")
        assert list_resp.status_code == 200
        assert len(list_resp.json()) == 1
        assert (
            list_resp.json()[0]["preview_url"]
            == "https://signed-preview.example.com/v1.mp4"
        )

    # PATCH Mode A: simple transcript_text edit + invalidates *__subs_on cache
    sf_doc_ref = MagicMock()
    sf_doc_ref.get.return_value = make_doc_snap("sf_1", dict(base_sf_data))
    mock_db.collection.return_value.document.return_value = sf_doc_ref

    with patch(
        "routes.sf_videos.manage.generate_video_signed_read_url",
        return_value="https://signed-preview.example.com/v1.mp4",
    ):
        patch_resp = client.patch(
            "/api/sf_videos/sf_1",
            json={"title": "Renamed Clip", "transcript_text": "hello amazing world"},
        )
        assert patch_resp.status_code == 200
        patched = patch_resp.json()
        assert patched["title"] == "Renamed Clip"
        assert patched["transcript_text"] == "hello amazing world"
        assert "9:16_crop__720p__subs_on" not in patched["rendered_variants"]
        assert "9:16_crop__720p__subs_off" in patched["rendered_variants"]

    # PATCH Mode B: manual words update
    sf_doc_ref.get.return_value = make_doc_snap("sf_1", dict(base_sf_data))
    patch_words = client.patch(
        "/api/sf_videos/sf_1",
        json={
            "words": [
                {"word": "manual", "start_sec": 10.5, "end_sec": 12.0},
                {"word": "timing", "start_sec": 12.5, "end_sec": 14.0},
            ]
        },
    )
    assert patch_words.status_code == 200
    assert patch_words.json()["transcript_text"] == "manual timing"

    # PATCH validation errors -> 400
    sf_doc_ref.get.return_value = make_doc_snap("sf_1", dict(base_sf_data))
    assert client.patch("/api/sf_videos/sf_1", json={}).status_code == 400
    assert client.patch("/api/sf_videos/sf_1", json={"title": "   "}).status_code == 400
    assert (
        client.patch(
            "/api/sf_videos/sf_1",
            json={"words": [{"word": "out", "start_sec": 1.0, "end_sec": 2.0}]},
        ).status_code
        == 400
    )

    # DELETE /api/sf_videos/{sf_id}
    mock_bucket = MagicMock()
    mock_bucket.list_blobs.return_value = [MagicMock()]
    mock_gcs.bucket.return_value = mock_bucket
    del_resp = client.delete("/api/sf_videos/sf_1")
    assert del_resp.status_code == 204
    sf_doc_ref.delete.assert_called_once()


################################################################
# POST /api/sf_videos/{sf_id}/download (Cached Fast Path & FFmpeg Render Path)
################################################################


def test_download_sf_video_cached_and_unrendered_paths(client: TestClient) -> None:
    now = datetime.now(timezone.utc)
    sf_data = {
        "uid": "user_123",
        "source_video_id": "v1",
        "source_filename": "stream.mp4",
        "title": "Highlight",
        "start_sec": 10.0,
        "end_sec": 25.0,
        "duration_seconds": 15.0,
        "transcript_text": "great play",
        "words": [{"word": "great", "start_sec": 10.0, "end_sec": 11.0}],
        "rendered_variants": {
            "9:16_crop__720p__subs_on": f"gs://{BUCKET}/user_123/sf_videos/sf_1/cached.mp4"
        },
        "status": "SUCCESSFUL",
        "is_complete": True,
        "created_at": now,
        "updated_at": now,
    }
    sf_doc_ref = MagicMock()
    sf_doc_ref.get.return_value = make_doc_snap("sf_1", dict(sf_data))
    mock_db.collection.return_value.document.return_value = sf_doc_ref

    # 1. Fast path: variant is already cached in GCS
    with (
        patch("routes.sf_videos.manage.gcs_blob_exists", return_value=True),
        patch(
            "routes.sf_videos.manage.generate_signed_download_url",
            return_value="https://signed-dl.example.com/cached.mp4",
        ),
    ):
        cached_resp = client.post(
            "/api/sf_videos/sf_1/download",
            json={"aspect_ratio": "9:16_crop", "resolution": "720p", "subtitles": True},
        )
        assert cached_resp.status_code == 200
        c_body = cached_resp.json()
        assert c_body["cached"] is True
        assert c_body["variant_key"] == "9:16_crop__720p__subs_on"
        assert c_body["download_url"] == "https://signed-dl.example.com/cached.mp4"

    # 2. Slow path: unrendered variant triggers `render_sf_video_variant`
    with (
        patch("routes.sf_videos.manage.gcs_blob_exists", return_value=False),
        patch(
            "routes.sf_videos.manage.get_user_video_doc",
            return_value=(
                MagicMock(),
                {"gcs_uri": f"gs://{BUCKET}/user_123/lf_videos/v1/original.mp4"},
            ),
        ),
        patch(
            "routes.sf_videos.manage.render_sf_video_variant",
            new_callable=AsyncMock,
            return_value=f"gs://{BUCKET}/user_123/sf_videos/sf_1/new.mp4",
        ) as mock_render,
        patch(
            "routes.sf_videos.manage.generate_signed_download_url",
            return_value="https://signed-dl.example.com/new.mp4",
        ),
    ):
        render_resp = client.post(
            "/api/sf_videos/sf_1/download",
            json={"aspect_ratio": "9:16_pad", "resolution": "1080p", "subtitles": False},
        )
        assert render_resp.status_code == 200
        r_body = render_resp.json()
        assert r_body["cached"] is False
        assert r_body["variant_key"] == "9:16_pad__1080p__subs_off"
        mock_render.assert_awaited_once()
        sf_doc_ref.update.assert_called_once()
