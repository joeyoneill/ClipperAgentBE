# evals/metrics/boundary_hygiene_eval.py
# Deterministic Clip Boundary Hygiene Evaluation
# Verifies clips proposed via `propose_clip_candidate` cut cleanly on natural
# word and sentence boundaries without mid-word or mid-sentence truncation.

import os
from typing import Any
from unittest.mock import MagicMock, patch
from pydantic import BaseModel

import db as db_module
from agent.tools.propose_clip_candidate import propose_clip_candidate
from models.misc import WordTimestamp


################################################################
# Evaluation Result Models
################################################################


class BoundaryHygieneCaseResult(BaseModel):
    case_id: str
    video_id: str
    start_sec: float
    end_sec: float
    duration_sec: float
    no_mid_word_truncation: bool
    word_boundary_aligned: bool
    clean_sentence_start: bool
    clean_sentence_end: bool
    duration_within_bounds: bool
    is_clean: bool
    expected_clean: bool
    matched_expectation: bool
    failure_reasons: list[str]


class BoundaryHygieneSuiteSummary(BaseModel):
    total_cases: int
    positive_cases: int
    negative_control_cases: int
    positive_clean_rate: float
    negative_rejection_rate: float
    overall_accuracy: float
    passed: bool
    case_results: list[BoundaryHygieneCaseResult]


################################################################
# Deterministic Word & Sentence Boundary Hygiene Inspector
################################################################

SENTENCE_END_PUNCTUATION = (".", "!", "?")
ALIGNMENT_TOLERANCE_SEC = 0.05
PAUSE_GAP_THRESHOLD_SEC = 0.30


def inspect_clip_boundary_hygiene(
    all_video_words: list[WordTimestamp],
    start_sec: float,
    end_sec: float,
    min_duration_sec: float = 15.0,
    max_duration_sec: float = 60.0,
) -> tuple[bool, dict[str, bool], list[str]]:
    """
    Deterministically checks 5 clip boundary hygiene invariants against the source
    video's word-level timestamp array:
      1. `no_mid_word_truncation`: Neither `start_sec` nor `end_sec` slices through the
         interior of a spoken word `(w.start_sec + eps < boundary < w.end_sec - eps)`.
      2. `word_boundary_aligned`: `start_sec` matches the first included word's `start_sec`
         (within 50ms) and `end_sec` matches the last included word's `end_sec` (within 50ms).
      3. `clean_sentence_start`: The first word of the clip is either the first word of the
         timeline, follows a word ending in (`.`, `!`, `?`), or follows a natural speech
         pause >= 0.30s.
      4. `clean_sentence_end`: The last word of the clip ends with terminal sentence
         punctuation (`.`, `!`, `?`).
      5. `duration_within_bounds`: `min_duration_sec <= (end_sec - start_sec) <= max_duration_sec`.
    """
    reasons: list[str] = []
    eps = ALIGNMENT_TOLERANCE_SEC

    # 1. Check mid-word truncation across all words in the video timeline
    mid_word_cut = False
    for w in all_video_words:
        if (w.start_sec + eps) < start_sec < (w.end_sec - eps):
            mid_word_cut = True
            reasons.append(
                f"start_sec ({start_sec:.2f}s) truncates mid-word '{w.word}' [{w.start_sec:.2f}s-{w.end_sec:.2f}s]."
            )
        if (w.start_sec + eps) < end_sec < (w.end_sec - eps):
            mid_word_cut = True
            reasons.append(
                f"end_sec ({end_sec:.2f}s) truncates mid-word '{w.word}' [{w.start_sec:.2f}s-{w.end_sec:.2f}s]."
            )
    no_mid_word_truncation = not mid_word_cut

    # Locate words inside [start_sec - eps, end_sec + eps]
    included_indices = [
        idx
        for idx, w in enumerate(all_video_words)
        if w.end_sec >= (start_sec - eps) and w.start_sec <= (end_sec + eps)
    ]

    if not included_indices:
        return (
            False,
            {
                "no_mid_word_truncation": no_mid_word_truncation,
                "word_boundary_aligned": False,
                "clean_sentence_start": False,
                "clean_sentence_end": False,
                "duration_within_bounds": False,
            },
            reasons + ["No spoken words found inside clip window."],
        )

    first_idx = included_indices[0]
    last_idx = included_indices[-1]
    first_word = all_video_words[first_idx]
    last_word = all_video_words[last_idx]

    # 2. Exact word boundary alignment
    start_aligned = abs(first_word.start_sec - start_sec) <= eps
    end_aligned = abs(last_word.end_sec - end_sec) <= eps
    word_boundary_aligned = start_aligned and end_aligned
    if not start_aligned:
        reasons.append(
            f"start_sec ({start_sec:.2f}s) not aligned to first word '{first_word.word}' start ({first_word.start_sec:.2f}s)."
        )
    if not end_aligned:
        reasons.append(
            f"end_sec ({end_sec:.2f}s) not aligned to last word '{last_word.word}' end ({last_word.end_sec:.2f}s)."
        )

    # 3. Clean sentence start boundary
    if first_idx == 0:
        clean_sentence_start = True
    else:
        prev_word = all_video_words[first_idx - 1]
        prev_ends_sentence = prev_word.word.strip().endswith(SENTENCE_END_PUNCTUATION)
        pause_gap = first_word.start_sec - prev_word.end_sec
        clean_sentence_start = prev_ends_sentence or (pause_gap >= PAUSE_GAP_THRESHOLD_SEC)
    if not clean_sentence_start:
        reasons.append(
            f"First word '{first_word.word}' starts mid-sentence after '{all_video_words[first_idx - 1].word}'."
        )

    # 4. Clean sentence end boundary
    clean_sentence_end = last_word.word.strip().endswith(SENTENCE_END_PUNCTUATION)
    if not clean_sentence_end:
        reasons.append(
            f"Last word '{last_word.word}' does not end on terminal sentence punctuation (., !, ?)."
        )

    # 5. Duration bounds check
    duration = round(end_sec - start_sec, 2)
    duration_within_bounds = min_duration_sec <= duration <= max_duration_sec
    if not duration_within_bounds:
        reasons.append(
            f"Clip duration ({duration:.2f}s) outside target range [{min_duration_sec:.1f}s, {max_duration_sec:.1f}s]."
        )

    checks = {
        "no_mid_word_truncation": no_mid_word_truncation,
        "word_boundary_aligned": word_boundary_aligned,
        "clean_sentence_start": clean_sentence_start,
        "clean_sentence_end": clean_sentence_end,
        "duration_within_bounds": duration_within_bounds,
    }
    is_clean = all(checks.values())
    return is_clean, checks, reasons


################################################################
# Evaluation Runner
################################################################


def evaluate_boundary_hygiene(dataset: dict[str, Any]) -> BoundaryHygieneSuiteSummary:
    """
    Executes `propose_clip_candidate` for every case in `boundary_hygiene_cases`
    and verifies deterministic word & sentence boundary hygiene.
    """
    bucket_name = os.environ["GCS_ROOT_BUCKET_NAME"]
    corpus_videos = {
        v["id"]: {
            **v,
            "gcs_uri": v.get("gcs_uri") or f"gs://{bucket_name}/{v['blob_path']}",
        }
        for v in dataset["corpus"]["videos"]
    }
    segments_by_doc_id = {
        s["segment_doc_id"]: s for s in dataset["corpus"]["segments"]
    }
    words_by_video: dict[str, list[WordTimestamp]] = {}
    for seg in dataset["corpus"]["segments"]:
        vid = seg["video_id"]
        words_by_video.setdefault(vid, []).extend(
            WordTimestamp.model_validate(w) for w in seg.get("words", [])
        )
    for vid in words_by_video:
        words_by_video[vid].sort(key=lambda w: w.start_sec)

    def fake_get_all(doc_refs: list[Any]) -> list[MagicMock]:
        snaps: list[MagicMock] = []
        for ref in doc_refs:
            doc_id = getattr(ref, "id", "")
            seg_data = segments_by_doc_id.get(doc_id)
            snap = MagicMock()
            snap.id = doc_id
            snap.exists = seg_data is not None
            snap.to_dict.return_value = seg_data
            snaps.append(snap)
        return snaps

    def fake_collection(name: str) -> MagicMock:
        col = MagicMock()
        col.document.side_effect = lambda doc_id: MagicMock(id=doc_id)
        return col

    ctx = MagicMock()
    ctx.user_id = "eval_user_01"
    ctx.state = {"selected_video_ids": []}

    results: list[BoundaryHygieneCaseResult] = []
    for case in dataset["boundary_hygiene_cases"]:
        vid = case["video_id"]
        with (
            patch.object(db_module.db, "collection", side_effect=fake_collection),
            patch.object(db_module.db, "get_all", side_effect=fake_get_all),
            patch(
                "agent.tools.propose_clip_candidate.get_allowed_user_videos",
                return_value=corpus_videos,
            ),
            patch(
                "agent.tools.propose_clip_candidate.generate_video_signed_read_url",
                return_value=f"https://signed.example.com/{vid}.mp4",
            ),
        ):
            prop_resp = propose_clip_candidate(
                video_id=vid,
                title=case["title"],
                start_sec=case["start_sec"],
                end_sec=case["end_sec"],
                rationale=case["rationale"],
                tool_context=ctx,
            )

        assert prop_resp.candidate is not None
        cand = prop_resp.candidate
        is_clean, checks, reasons = inspect_clip_boundary_hygiene(
            all_video_words=words_by_video[vid],
            start_sec=cand.start_sec,
            end_sec=cand.end_sec,
            min_duration_sec=case.get("min_duration_sec", 15.0),
            max_duration_sec=case.get("max_duration_sec", 60.0),
        )
        expected_clean = bool(case["expected_clean"])
        results.append(
            BoundaryHygieneCaseResult(
                case_id=case["id"],
                video_id=vid,
                start_sec=cand.start_sec,
                end_sec=cand.end_sec,
                duration_sec=cand.duration_sec,
                no_mid_word_truncation=checks["no_mid_word_truncation"],
                word_boundary_aligned=checks["word_boundary_aligned"],
                clean_sentence_start=checks["clean_sentence_start"],
                clean_sentence_end=checks["clean_sentence_end"],
                duration_within_bounds=checks["duration_within_bounds"],
                is_clean=is_clean,
                expected_clean=expected_clean,
                matched_expectation=(is_clean == expected_clean),
                failure_reasons=reasons,
            )
        )

    pos_cases = [r for r in results if r.expected_clean]
    neg_cases = [r for r in results if not r.expected_clean]

    pos_rate = (
        round(sum(1.0 for r in pos_cases if r.is_clean) / len(pos_cases), 4)
        if pos_cases
        else 1.0
    )
    neg_rate = (
        round(sum(1.0 for r in neg_cases if not r.is_clean) / len(neg_cases), 4)
        if neg_cases
        else 1.0
    )
    overall_acc = round(
        sum(1.0 for r in results if r.matched_expectation) / max(1, len(results)), 4
    )

    return BoundaryHygieneSuiteSummary(
        total_cases=len(results),
        positive_cases=len(pos_cases),
        negative_control_cases=len(neg_cases),
        positive_clean_rate=pos_rate,
        negative_rejection_rate=neg_rate,
        overall_accuracy=overall_acc,
        passed=(pos_rate == 1.0 and neg_rate == 1.0),
        case_results=results,
    )
