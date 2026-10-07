# agent/tools/_helpers.py
# Shared RAG & Security Verification Helpers for Agent Tools

# Imports
from dotenv import load_dotenv
from google import genai
from google.adk.tools import ToolContext
from google.cloud.firestore_v1.base_query import FieldFilter
from google.genai import types
from typing import Any
import os

# Custom Dependencies
from db import db
from models.misc import WordTimestamp

# Load ENV Vars & Constants
load_dotenv()
LFVIDEO_COLLECTION_NAME = os.environ["LFVIDEO_COLLECTION_NAME"]
LFVIDEO_SEGMENT_COLLECTION_NAME = os.environ["LFVIDEO_SEGMENT_COLLECTION_NAME"]
EMBEDDING_MODEL_NAME = os.environ["EMBEDDING_MODEL_NAME"]
EMBEDDING_DIMENSION = 1408

# Initialize GenAI Client
genai_client = genai.Client(
    vertexai=True,
    project=os.environ['GCP_PROJECT_ID'],
    location='global'
)

################################################################
# General MISC Helper Functions
################################################################

def parse_word_timestamps(raw_words: list[Any]) -> list[WordTimestamp]:
    """
    Normalizes stored word dicts in lf_video_segments into WordTimestamp models.
    """
    parsed: list[WordTimestamp] = []
    for w in raw_words or []:
        if isinstance(w, WordTimestamp):
            parsed.append(w)
        elif isinstance(w, dict):
            parsed.append(
                WordTimestamp(
                    word=str(w.get("word", "")),
                    start_sec=float(w.get("start_sec", w.get("start", 0.0))),
                    end_sec=float(w.get("end_sec", w.get("end", 0.0))),
                )
            )
    return parsed

################################################################
# RAG Helper Functions
################################################################

def embed_query_text(query_text: str) -> list[float]:
    """
    Embeds a search query into the 1408-d gemini-embedding-2 vector space.
    """
    response = genai_client.models.embed_content(
        model=EMBEDDING_MODEL_NAME,
        contents=query_text,
        config=types.EmbedContentConfig(
            output_dimensionality=EMBEDDING_DIMENSION,
            task_type="RETRIEVAL_QUERY",
        ),
    )
    if not response.embeddings or not response.embeddings[0].values:
        raise RuntimeError("Failed to generate query embedding from gemini-embedding-2.")
    return list(response.embeddings[0].values)


def get_allowed_user_videos(tool_context: ToolContext) -> dict[str, dict[str, Any]]:
    """
    Deterministically reads `uid` and `selected_video_ids` directly from ADK
    ToolContext (never from LLM arguments) and returns {video_id: video_dict}
    for all SUCCESSFUL videos the user owns within the active Tape Selector scope.
    """
    uid = tool_context.user_id
    pinned_ids: list[str] = tool_context.state.get("selected_video_ids") or []
    pinned_set = set(pinned_ids) if pinned_ids else None

    query = (
        db.collection(LFVIDEO_COLLECTION_NAME)
        .where(filter=FieldFilter("uid", "==", uid))
        .where(filter=FieldFilter("status", "==", "SUCCESSFUL"))
    )

    videos: dict[str, dict[str, Any]] = {}
    for snap in query.stream():
        if pinned_set is not None and snap.id not in pinned_set:
            continue
        data = snap.to_dict() or {}
        data["id"] = snap.id
        videos[snap.id] = data

    return videos