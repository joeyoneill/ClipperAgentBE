# tests/test_routes_lf_videos.py
# Integration tests for main.py & routes/lf_videos/ (upload, read, delete) with mocked Firestore & GCS.

import os
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

from tests.conftest import make_doc_snap, mock_db, mock_gcs

BUCKET = os.environ["GCS_ROOT_BUCKET_NAME"]


################################################################
# General Routes (`main.py`)
################################################################


def test_root_redirect_and_health_check(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}

    redir = client.get("/", follow_redirects=False)
    assert redir.status_code in (302, 307)
    assert redir.headers["location"] == "/docs"


def test_unauthenticated_request_rejected(unauth_client: TestClient) -> None:
    resp = unauth_client.get("/api/lf_videos/list")
    assert resp.status_code in (401, 403)


################################################################
# POST /api/lf_videos/upload/init
################################################################


def test_init_lf_video_upload_success_and_errors(client: TestClient) -> None:
    mock_doc_ref = MagicMock()
    mock_doc_ref.id = "vid_new_1"
    mock_db.collection.return_value.document.return_value = mock_doc_ref

    mock_blob = MagicMock()
    mock_blob.create_resumable_upload_session.return_value = (
        "https://storage.googleapis.com/upload/resumable/session_123"
    )
    mock_gcs.bucket.return_value.blob.return_value = mock_blob

    resp = client.post(
        "/api/lf_videos/upload/init",
        json={
            "filename": "tournament.mp4",
            "content_type": "video/mp4",
            "file_size_bytes": 5000000,
            "duration_seconds": 120,
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["video_id"] == "vid_new_1"
    assert body["upload_url"].startswith("https://storage.googleapis.com/")
    assert body["blob_path"] == "user_123/lf_videos/vid_new_1/original.mp4"
    mock_doc_ref.set.assert_called_once()

    # Unsupported MIME type -> 415
    bad_mime = client.post(
        "/api/lf_videos/upload/init",
        json={"filename": "notes.txt", "content_type": "text/plain"},
    )
    assert bad_mime.status_code == 415

    # GCS resumable session failure -> 502 and rolls back Firestore doc
    mock_blob.create_resumable_upload_session.side_effect = RuntimeError("GCS unavailable")
    gcs_fail = client.post(
        "/api/lf_videos/upload/init",
        json={"filename": "tournament.mp4", "content_type": "video/mp4"},
    )
    assert gcs_fail.status_code == 502
    mock_doc_ref.delete.assert_called_once()


################################################################
# POST /api/lf_videos/upload/{vid}/complete & /{vid}/failed
################################################################


def test_completed_upload_success_and_failure_branches(client: TestClient) -> None:
    vid_data = {
        "uid": "user_123",
        "filename": "stream.mp4",
        "gcs_uri": f"gs://{BUCKET}/user_123/lf_videos/v1/original.mp4",
        "status": "PENDING",
        "is_complete": False,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    mock_doc_ref = MagicMock()
    mock_doc_ref.get.return_value = make_doc_snap("v1", vid_data)
    mock_db.collection.return_value.document.return_value = mock_doc_ref

    mock_blob = MagicMock()
    mock_blob.exists.return_value = True
    mock_blob.size = 999999
    mock_gcs.bucket.return_value.blob.return_value = mock_blob

    with patch("routes.lf_videos.upload.trigger_video_processor_job") as mock_trigger:
        resp = client.post("/api/lf_videos/upload/v1/complete")
        assert resp.status_code == 200
        assert resp.json()["status"] == "PROCESSING"
        mock_trigger.assert_called_once_with(vid="v1", uid="user_123")

    # Missing blob in GCS -> 400
    mock_blob.exists.return_value = False
    resp_missing = client.post("/api/lf_videos/upload/v1/complete")
    assert resp_missing.status_code == 400

    # Cloud Run job trigger error -> 500 and marks status FAILED
    mock_blob.exists.return_value = True
    with patch(
        "routes.lf_videos.upload.trigger_video_processor_job",
        side_effect=RuntimeError("Cloud Run quota exceeded"),
    ):
        resp_job_err = client.post("/api/lf_videos/upload/v1/complete")
        assert resp_job_err.status_code == 500


def test_failed_upload_success_and_409_conflict(client: TestClient) -> None:
    vid_data = {
        "uid": "user_123",
        "filename": "stream.mp4",
        "gcs_uri": f"gs://{BUCKET}/user_123/lf_videos/v1/original.mp4",
        "status": "PENDING",
        "is_complete": False,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    mock_doc_ref = MagicMock()
    mock_doc_ref.get.return_value = make_doc_snap("v1", vid_data)
    mock_db.collection.return_value.document.return_value = mock_doc_ref

    resp = client.post(
        "/api/lf_videos/upload/v1/failed",
        json={"error_msg": "Client lost connection"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "FAILED"
    assert resp.json()["error_msg"] == "Client lost connection"

    # Already completed -> 409 Conflict
    mock_doc_ref.get.return_value = make_doc_snap(
        "v1", {**vid_data, "status": "SUCCESSFUL", "is_complete": True}
    )
    conflict = client.post("/api/lf_videos/upload/v1/failed", json={})
    assert conflict.status_code == 409


################################################################
# GET /api/lf_videos/list, GET /api/lf_videos/{vid}, DELETE /api/lf_videos/{vid}
################################################################


def test_list_get_and_delete_lf_videos(client: TestClient) -> None:
    t1 = datetime(2026, 10, 1, tzinfo=timezone.utc)
    t2 = datetime(2026, 10, 10, tzinfo=timezone.utc)
    snap1 = make_doc_snap(
        "v_old",
        {
            "uid": "user_123",
            "filename": "older.mp4",
            "gcs_uri": f"gs://{BUCKET}/user_123/lf_videos/v_old/original.mp4",
            "status": "SUCCESSFUL",
            "is_complete": True,
            "created_at": t1,
            "updated_at": t1,
        },
    )
    snap2 = make_doc_snap(
        "v_new",
        {
            "uid": "user_123",
            "filename": "newer.mp4",
            "gcs_uri": f"gs://{BUCKET}/user_123/lf_videos/v_new/original.mp4",
            "status": "SUCCESSFUL",
            "is_complete": True,
            "created_at": t2,
            "updated_at": t2,
        },
    )
    mock_db.collection.return_value.where.return_value.stream.return_value = [
        snap1,
        snap2,
    ]

    list_resp = client.get("/api/lf_videos/list")
    assert list_resp.status_code == 200
    items = list_resp.json()
    assert [v["id"] for v in items] == ["v_new", "v_old"]

    # GET single video
    mock_doc_ref = MagicMock()
    mock_doc_ref.get.return_value = snap2
    mock_db.collection.return_value.document.return_value = mock_doc_ref

    get_resp = client.get("/api/lf_videos/v_new")
    assert get_resp.status_code == 200
    assert get_resp.json()["filename"] == "newer.mp4"

    # DELETE cascading cleanup (GCS blobs + Firestore segments batch + parent doc)
    mock_bucket = MagicMock()
    mock_bucket.list_blobs.return_value = [MagicMock(), MagicMock()]
    mock_gcs.bucket.return_value = mock_bucket

    seg_snaps = [make_doc_snap(f"v_new_seg_{i:04d}", {}) for i in range(3)]
    mock_db.collection.return_value.where.return_value.stream.return_value = seg_snaps
    mock_batch = MagicMock()
    mock_db.batch.return_value = mock_batch

    del_resp = client.delete("/api/lf_videos/v_new")
    assert del_resp.status_code == 204
    mock_bucket.delete_blobs.assert_called_once()
    assert mock_batch.delete.call_count == 3
    mock_batch.commit.assert_called_once()
    mock_doc_ref.delete.assert_called_once()
