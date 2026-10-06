# CRUD DELETE Endpoints

# Imports
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException, Response, status
from google.cloud.firestore_v1.base_query import FieldFilter
import os

# Custom Dependencies
from db import db
from storage import gcs_client
from utils.auth import UserInfo, get_current_user
from utils.lf_videos import get_user_video_doc

# Init Router
router = APIRouter(tags=["LFVideo Delete"])

# Load ENV Vars
load_dotenv()
GCS_ROOT_BUCKET_NAME = os.environ['GCS_ROOT_BUCKET_NAME']
LFVIDEO_SEGMENT_COLLECTION_NAME = os.environ["LFVIDEO_SEGMENT_COLLECTION_NAME"]

################################################################
# DELETE: /api/lf_videos/{vid}
################################################################

@router.delete("/{vid}", status_code=status.HTTP_204_NO_CONTENT)
def delete_lf_video(
    vid: str,
    user: UserInfo = Depends(get_current_user)
) -> Response:
    # get & confirm ownership
    doc_ref, _ = get_user_video_doc(vid, user.uid)

    # delete blobs in GCS
    folder_prefix = f"{user.uid}/lf_videos/{vid}/"
    try:
        bucket = gcs_client.bucket(GCS_ROOT_BUCKET_NAME)
        blobs = list(bucket.list_blobs(prefix=folder_prefix))
        if blobs:
            bucket.delete_blobs(blobs)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to delete video files from GCS: {e}"
        )

    # Delete all associated video segments in Firestore (in batches of 400)
    try:
        seg_query = (
            db.collection(LFVIDEO_SEGMENT_COLLECTION_NAME)
            .where(filter=FieldFilter("video_id", "==", vid))
        )
        batch = db.batch()
        op_count = 0
        for seg_doc in seg_query.stream():
            batch.delete(seg_doc.reference)
            op_count += 1
            if op_count >= 400:
                batch.commit()
                batch = db.batch()
                op_count = 0
        if op_count > 0:
            batch.commit()
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete video segments from database: {e}"
        )

    # delete firestore record
    try:
        doc_ref.delete()
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete video record from database: {e}"
        )
    
    # return response
    return Response(status_code=status.HTTP_204_NO_CONTENT)