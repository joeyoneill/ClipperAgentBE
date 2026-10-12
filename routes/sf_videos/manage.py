# routes/sf_videos/manage.py
# Short-Form Video (Reel) List, Rename, Delete, and On-Demand Download Endpoints

# Imports
from dotenv import load_dotenv
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Response,
    status,
)
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
import os

# Custom Dependencies
from db import db
from models.misc import WordTimestamp
from models.sf_videos import (
    DownloadSFVideoRequest,
    DownloadSFVideoResponse,
    SFVideoResponse,
    UpdateSFVideoRequest,
)
from storage import gcs_client
from utils.auth import UserInfo, get_current_user
from utils.lf_videos import generate_video_signed_read_url, get_user_video_doc
from utils.misc import get_utc_now
from utils.sf_videos import (
    gcs_blob_exists,
    generate_signed_download_url,
    get_user_sf_video_doc,
    realign_transcript_words,
    render_sf_video_variant,
    sanitize_download_filename,
    validate_manual_words
)

# Init Router
router = APIRouter(tags=["SFVideos (Reel)"])

# Load Env Vars
load_dotenv()
GCS_ROOT_BUCKET_NAME = os.environ["GCS_ROOT_BUCKET_NAME"]
LFVIDEO_COLLECTION_NAME = os.environ["LFVIDEO_COLLECTION_NAME"]
SFVIDEO_COLLECTION_NAME = os.environ["SFVIDEO_COLLECTION_NAME"]


################################################################
# GET: /api/sf_videos/list
################################################################

@router.get("/list", response_model=list[SFVideoResponse], status_code=status.HTTP_200_OK)
def list_user_sf_videos(
    user: UserInfo = Depends(get_current_user),
) -> list[SFVideoResponse]:
    """
    list a users sf videos

    1. Queries `sf_videos` for all saved clips belonging to `user.uid` (newest first).
    2. Batch-fetches parent `lfvideos` documents in 1 Firestore RPC (`db.get_all`).
    3. Attaches a signed `preview_url` to each clip so the Reel UI can play `#t={start_sec},{end_sec}`.
    """
    try:
        query = (
            db.collection(SFVIDEO_COLLECTION_NAME)
            .where(filter=FieldFilter("uid", "==", user.uid))
            .order_by("created_at", direction=firestore.Query.DESCENDING)
        )
        snaps = list(query.stream())
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list saved Reel clips: {e}",
        )

    if not snaps:
        return []

    raw_clips: list[tuple[str, dict]] = []
    parent_video_ids: set[str] = set()
    for snap in snaps:
        data = snap.to_dict() or {}
        raw_clips.append((snap.id, data))
        if data.get("source_video_id"):
            parent_video_ids.add(data["source_video_id"])

    # Batch-fetch parent LFVideo docs in 1 RPC to mint preview URLs
    signed_preview_map: dict[str, str] = {}
    if parent_video_ids:
        lf_refs = [
            db.collection(LFVIDEO_COLLECTION_NAME).document(vid)
            for vid in parent_video_ids
        ]
        for lf_snap in db.get_all(lf_refs):
            if not lf_snap.exists:
                continue
            lf_data = lf_snap.to_dict() or {}
            if lf_data.get("uid") == user.uid and lf_data.get("gcs_uri"):
                try:
                    signed_preview_map[lf_snap.id] = generate_video_signed_read_url(
                        lf_data["gcs_uri"]
                    )
                except Exception:
                    pass

    results: list[SFVideoResponse] = []
    for clip_id, clip_data in raw_clips:
        source_vid = clip_data.get("source_video_id", "")
        results.append(
            SFVideoResponse(
                id=clip_id,
                preview_url=signed_preview_map.get(source_vid),
                **clip_data,
            )
        )

    return results


################################################################
# PATCH: /api/sf_videos/{sf_id}
################################################################

@router.patch("/{sf_id}", response_model=SFVideoResponse)
async def update_sf_video_title(
    sf_id: str,
    payload: UpdateSFVideoRequest,
    user: UserInfo = Depends(get_current_user),
):
    """
    Updates a saved short-form clip's title, simple transcript_text, or manual words[] list.
    Invalidates cached `*__subs_on` MP4 variants whenever subtitles are modified.
    """
    doc_ref, data = get_user_sf_video_doc(sf_id=sf_id, uid=user.uid)

    updates: dict = {}
    start_sec = float(data.get("start_sec", 0.0))
    end_sec = float(data.get("end_sec", 0.0))
    subtitles_modified = False

    if payload.title is not None:
        new_title = payload.title.strip()
        if not new_title:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Clip title cannot be empty.",
            )
        updates["title"] = new_title

    # Mode B: Advanced Word-by-Word + Timestamp Editing
    # Default to advanced if advanced & simple both not None at same time
    if payload.words is not None:
        try:
            validated_words = validate_manual_words(
                words=payload.words,
                clip_start_sec=start_sec,
                clip_end_sec=end_sec,
            )
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(e),
            )
        updates["words"] = [w.model_dump() for w in validated_words]
        updates["transcript_text"] = " ".join(w.word for w in validated_words)
        subtitles_modified = True

    # Mode A: Simple Paragraph Text Editing
    elif payload.transcript_text is not None:
        new_transcript = payload.transcript_text.strip()
        if not new_transcript:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Transcript text cannot be empty.",
            )
        raw_words = data.get("words") or []
        existing_words = [
            w if isinstance(w, WordTimestamp) else WordTimestamp.model_validate(w)
            for w in raw_words
        ]
        try:
            realigned_words = realign_transcript_words(
                existing_words=existing_words,
                new_transcript_text=new_transcript,
                clip_start_sec=start_sec,
                clip_end_sec=end_sec,
            )
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(e),
            )
        updates["words"] = [w.model_dump() for w in realigned_words]
        updates["transcript_text"] = " ".join(w.word for w in realigned_words)
        subtitles_modified = True

    if not updates:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Must provide `title`, `transcript_text`, or `words` to update.",
        )

    # Clear cached *__subs_on variants from GCS & Firestore if subtitles changed
    if subtitles_modified:
        rendered_variants = dict(data.get("rendered_variants") or {})
        subs_keys = [k for k in rendered_variants if k.endswith("__subs_on")]
        if subs_keys:
            bucket = gcs_client.bucket(GCS_ROOT_BUCKET_NAME)
            prefix = f"gs://{GCS_ROOT_BUCKET_NAME}/"
            for k in subs_keys:
                uri = rendered_variants.pop(k, "")
                if uri.startswith(prefix):
                    try:
                        bucket.blob(uri[len(prefix) :]).delete()
                    except Exception:
                        pass
            updates["rendered_variants"] = rendered_variants

    updates["updated_at"] = get_utc_now()

    try:
        doc_ref.update(updates)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update clip in Firestore: {e}",
        )

    data.update(updates)

    preview_url: str | None = None
    source_vid = data.get("source_video_id")
    if source_vid:
        lf_snap = db.collection(LFVIDEO_COLLECTION_NAME).document(source_vid).get()
        if lf_snap.exists:
            lf_data = lf_snap.to_dict() or {}
            if lf_data.get("uid") == user.uid and lf_data.get("gcs_uri"):
                try:
                    preview_url = generate_video_signed_read_url(lf_data["gcs_uri"])
                except Exception:
                    pass

    return SFVideoResponse(id=sf_id, preview_url=preview_url, **data)

################################################################
# DELETE: /api/sf_videos/{sf_id}
################################################################

@router.delete("/{sf_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_sf_video(
    sf_id: str,
    user: UserInfo = Depends(get_current_user),
) -> Response:
    """
    Deletes a SF Video by ID

    1. Verifies the clip exists in `sf_videos` and belongs to `user.uid`.
    2. Deletes any cached rendered MP4 variant blobs in GCS under `<uid>/sf_videos/<sf_id>/`.
    3. Deletes the `sf_videos` Firestore document.
    """
    doc_ref, _ = get_user_sf_video_doc(sf_id, user.uid)

    # Delete any cached rendered MP4 blobs for this clip in GCS
    try:
        bucket = gcs_client.bucket(GCS_ROOT_BUCKET_NAME)
        prefix = f"{user.uid}/sf_videos/{sf_id}/"
        blobs = list(bucket.list_blobs(prefix=prefix))
        for b in blobs:
            b.delete()
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to clean up cached clip files in GCS: {e}",
        )

    # Delete Firestore record
    try:
        doc_ref.delete()
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete clip record from database: {e}",
        )

    return Response(status_code=status.HTTP_204_NO_CONTENT)


################################################################
# POST: /api/sf_videos/{sf_id}/download
################################################################

@router.post(
    "/{sf_id}/download",
    response_model=DownloadSFVideoResponse,
    status_code=status.HTTP_200_OK,
)
async def download_sf_video(
    sf_id: str,
    payload: DownloadSFVideoRequest,
    user: UserInfo = Depends(get_current_user),
) -> DownloadSFVideoResponse:
    """
    Returns Download URL for requested SF Video with options

    1. Verifies clip ownership in `sf_videos`.
    2. Computes deterministic `variant_key` from `(aspect_ratio, resolution, subtitles)`.
    3. If the variant is already cached in GCS, returns a signed attachment download URL immediately.
    4. Otherwise, renders the variant via async FFmpeg, caches it in GCS + Firestore, and returns the download URL.
    """
    doc_ref, sf_data = get_user_sf_video_doc(sf_id, user.uid)

    subs_tag = "subs_on" if payload.subtitles else "subs_off"
    variant_key = f"{payload.aspect_ratio}__{payload.resolution}__{subs_tag}"

    file_suffix = f"{payload.aspect_ratio}_{payload.resolution}{'_cc' if payload.subtitles else ''}"
    download_filename = sanitize_download_filename(
        sf_data.get("title", "clip"),
        suffix=file_suffix,
    )

    # Fast Path: Check if this exact variant is already cached in GCS
    rendered_variants: dict[str, str] = dict(sf_data.get("rendered_variants") or {})
    cached_gcs_uri = rendered_variants.get(variant_key)
    if cached_gcs_uri and gcs_blob_exists(cached_gcs_uri):
        try:
            signed_url = generate_signed_download_url(
                gcs_uri=cached_gcs_uri,
                download_filename=download_filename,
            )
            return DownloadSFVideoResponse(
                download_url=signed_url,
                filename=download_filename,
                variant_key=variant_key,
                cached=True,
            )
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Failed to sign cached download URL: {e}",
            )

    # Render Path: Verify parent LFVideo still exists in GCS & Firestore
    source_video_id = sf_data.get("source_video_id")
    if not source_video_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Clip is missing its source_video_id reference.",
        )

    _, lf_data = get_user_video_doc(source_video_id, user.uid)
    source_gcs_uri = lf_data.get("gcs_uri")
    if not source_gcs_uri:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Parent long-form video GCS file not found.",
        )

    # Parse word-level timestamps for optional yellow-karaoke subtitle burn-in
    raw_words = sf_data.get("words") or []
    words = [
        w if isinstance(w, WordTimestamp) else WordTimestamp.model_validate(w)
        for w in raw_words
    ]

    # Run non-blocking FFmpeg render + GCS upload
    try:
        rendered_gcs_uri = await render_sf_video_variant(
            uid=user.uid,
            sf_id=sf_id,
            source_gcs_uri=source_gcs_uri,
            start_sec=float(sf_data["start_sec"]),
            end_sec=float(sf_data["end_sec"]),
            words=words,
            aspect_ratio=payload.aspect_ratio,
            resolution=payload.resolution,
            subtitles=payload.subtitles,
            variant_key=variant_key,
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to render video clip variant: {e}",
        )

    # Persist the newly cached variant URI onto the SFVideo Firestore document
    rendered_variants[variant_key] = rendered_gcs_uri
    try:
        doc_ref.update(
            {
                "rendered_variants": rendered_variants,
                "status": "SUCCESSFUL",
                "is_complete": True,
                "gcs_uri": rendered_gcs_uri,
                "updated_at": get_utc_now(),
            }
        )
    except Exception:
        # Non-fatal if cache metadata update fails; we still return the download URL
        pass

    try:
        signed_url = generate_signed_download_url(
            gcs_uri=rendered_gcs_uri,
            download_filename=download_filename,
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to generate signed download URL: {e}",
        )

    return DownloadSFVideoResponse(
        download_url=signed_url,
        filename=download_filename,
        variant_key=variant_key,
        cached=False,
    )