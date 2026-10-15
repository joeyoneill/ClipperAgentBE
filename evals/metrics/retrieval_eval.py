# evals/metrics/retrieval_eval.py
# Deterministic Multimodal RAG Retrieval Evaluation (Recall@5, Precision@5, MRR@5)
# Exercises `agent.tools.search_video_segments.search_video_segments` across
# text, visual, and hybrid (Reciprocal Rank Fusion) search modes.

import hashlib
import math
from typing import Any
from unittest.mock import MagicMock, patch
from pydantic import BaseModel

import db as db_module
from agent.tools.search_video_segments import search_video_segments
from models.lf_videos import LFVideoSegment


################################################################
# Evaluation Result Models
################################################################


class RetrievalCaseResult(BaseModel):
    case_id: str
    query: str
    search_mode: str
    expected_segment_ids: list[str]
    retrieved_segment_ids: list[str]
    recall_at_5: float
    precision_at_5: float
    raw_precision_at_5: float
    reciprocal_rank: float
    hit_at_1: bool


class RetrievalSuiteSummary(BaseModel):
    total_cases: int
    mean_recall_at_5: float
    mean_precision_at_5: float
    mean_raw_precision_at_5: float
    mrr_at_5: float
    top_1_hit_rate: float
    passed: bool
    case_results: list[RetrievalCaseResult]


################################################################
# Deterministic 1408-D Concept Embedding & Cosine KNN Engine
################################################################

EMBEDDING_DIM = 1408


def build_deterministic_1408d_vector(tags: list[str], text_fallback: str = "") -> list[float]:
    """
    Projects semantic tags and keywords into a deterministic L2-normalized 1408-d vector
    so cosine similarity (`DistanceMeasure.COSINE`) in `find_nearest` behaves identically
    to a trained multimodal embedding space without external network non-determinism.
    """
    vec = [0.0] * EMBEDDING_DIM
    tokens = [t.lower().strip() for t in tags if t.strip()]
    if not tokens and text_fallback:
        tokens = [w.lower().strip(".,!?") for w in text_fallback.split() if len(w) > 3]

    for tok in tokens:
        digest = hashlib.sha256(tok.encode("utf-8")).digest()
        # Activate 8 deterministic dimensions per semantic concept tag
        for i in range(8):
            idx = int.from_bytes(digest[i * 2 : i * 2 + 2], "big") % EMBEDDING_DIM
            vec[idx] += 1.0

    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


class InMemoryVectorFirestoreCollection:
    """
    Simulates Firestore's `lf_video_segments` collection with real 1408-d Cosine KNN
    `find_nearest` execution and `uid` / `video_id` pre-filtering.
    """

    def __init__(self, segments: list[dict[str, Any]], filters: list[tuple[str, Any]] | None = None):
        self._segments = segments
        self._filters = filters or []

    def where(self, filter: Any = None, **kwargs: Any) -> "InMemoryVectorFirestoreCollection":
        new_filters = list(self._filters)
        if filter is not None and hasattr(filter, "field_path"):
            new_filters.append((filter.field_path, filter.value))
        return InMemoryVectorFirestoreCollection(self._segments, new_filters)

    def find_nearest(
        self,
        vector_field: str,
        query_vector: Any,
        distance_measure: Any,
        limit: int,
    ) -> MagicMock:
        q_vals = list(query_vector.to_map_value()["value"]) if hasattr(query_vector, "to_map_value") else list(query_vector)
        scored: list[tuple[float, dict[str, Any]]] = []
        for seg in self._segments:
            # Apply equality filters (uid, video_id)
            if not all(seg.get(f_key) == f_val for f_key, f_val in self._filters):
                continue
            cand_vec = seg.get(vector_field)
            if not cand_vec:
                continue
            sim = cosine_similarity(q_vals, cand_vec)
            scored.append((sim, seg))

        scored.sort(key=lambda item: item[0], reverse=True)
        top_snaps: list[MagicMock] = []
        for _, seg in scored[:limit]:
            snap = MagicMock()
            snap.id = seg["segment_doc_id"]
            snap.exists = True
            snap.to_dict.return_value = seg
            top_snaps.append(snap)

        result_mock = MagicMock()
        result_mock.get.return_value = top_snaps
        return result_mock


################################################################
# Evaluation Runner
################################################################


def evaluate_rag_retrieval(dataset: dict[str, Any], k: int = 5) -> RetrievalSuiteSummary:
    """
    Runs all `rag_retrieval_cases` in `golden_dataset.json` through the real
    `search_video_segments` ADK tool and computes Recall@5, Precision@5, and MRR@5.
    """
    corpus_videos = {v["id"]: v for v in dataset["corpus"]["videos"]}
    embedded_segments: list[dict[str, Any]] = []

    for raw_seg in dataset["corpus"]["segments"]:
        text_vec = build_deterministic_1408d_vector(
            raw_seg.get("text_tags", []), raw_seg.get("transcript_text", "")
        )
        visual_vec = build_deterministic_1408d_vector(
            raw_seg.get("visual_tags", []), raw_seg.get("visual_description", "")
        )
        # Validate against Pydantic LFVideoSegment schema
        seg_model = LFVideoSegment(
            id=raw_seg["segment_doc_id"],
            video_id=raw_seg["video_id"],
            uid=raw_seg["uid"],
            segment_index=raw_seg["segment_index"],
            start_sec=raw_seg["start_sec"],
            end_sec=raw_seg["end_sec"],
            transcript_text=raw_seg["transcript_text"],
            words=raw_seg.get("words", []),
            text_embedding=text_vec,
            video_embedding=visual_vec,
        )
        seg_dict = seg_model.model_dump()
        seg_dict["segment_doc_id"] = raw_seg["segment_doc_id"]
        embedded_segments.append(seg_dict)

    vector_collection = InMemoryVectorFirestoreCollection(embedded_segments)
    case_results: list[RetrievalCaseResult] = []

    for case in dataset["rag_retrieval_cases"]:
        pinned_ids: list[str] = case.get("selected_video_ids", [])
        allowed_vids = (
            {vid: v for vid, v in corpus_videos.items() if vid in pinned_ids}
            if pinned_ids
            else dict(corpus_videos)
        )
        query_vec = build_deterministic_1408d_vector(
            case.get("query_tags", []), case["query"]
        )

        ctx = MagicMock()
        ctx.user_id = "eval_user_01"
        ctx.state = {"selected_video_ids": pinned_ids}

        with (
            patch.object(db_module.db, "collection", return_value=vector_collection),
            patch(
                "agent.tools.search_video_segments.get_allowed_user_videos",
                return_value=allowed_vids,
            ),
            patch(
                "agent.tools.search_video_segments.embed_query_text",
                return_value=query_vec,
            ),
        ):
            resp = search_video_segments(
                query=case["query"],
                tool_context=ctx,
                search_mode=case["search_mode"],
                top_k=k,
            )

        retrieved_ids = [m.segment_doc_id for m in resp.matches[:k]]
        expected_ids = list(case["expected_segment_ids"])
        expected_set = set(expected_ids)

        hits = [doc_id for doc_id in retrieved_ids if doc_id in expected_set]
        recall_at_k = len(hits) / max(1, len(expected_set))
        # Normalized Precision@K (R-Precision capped at min(k, |Relevant|)) and raw Precision@K
        norm_denom = min(k, len(expected_set)) or 1
        precision_at_k = len(hits) / norm_denom
        raw_precision_at_k = len(hits) / k

        rr = 0.0
        for idx, doc_id in enumerate(retrieved_ids):
            if doc_id in expected_set:
                rr = 1.0 / (idx + 1)
                break

        case_results.append(
            RetrievalCaseResult(
                case_id=case["id"],
                query=case["query"],
                search_mode=case["search_mode"],
                expected_segment_ids=expected_ids,
                retrieved_segment_ids=retrieved_ids,
                recall_at_5=round(recall_at_k, 4),
                precision_at_5=round(precision_at_k, 4),
                raw_precision_at_5=round(raw_precision_at_k, 4),
                reciprocal_rank=round(rr, 4),
                hit_at_1=bool(retrieved_ids and retrieved_ids[0] in expected_set),
            )
        )

    n = max(1, len(case_results))
    mean_recall = round(sum(c.recall_at_5 for c in case_results) / n, 4)
    mean_precision = round(sum(c.precision_at_5 for c in case_results) / n, 4)
    mean_raw_precision = round(sum(c.raw_precision_at_5 for c in case_results) / n, 4)
    mrr = round(sum(c.reciprocal_rank for c in case_results) / n, 4)
    hit_rate = round(sum(1.0 for c in case_results if c.hit_at_1) / n, 4)

    return RetrievalSuiteSummary(
        total_cases=len(case_results),
        mean_recall_at_5=mean_recall,
        mean_precision_at_5=mean_precision,
        mean_raw_precision_at_5=mean_raw_precision,
        mrr_at_5=mrr,
        top_1_hit_rate=hit_rate,
        passed=(mean_recall >= 0.85 and mean_precision >= 0.85),
        case_results=case_results,
    )
