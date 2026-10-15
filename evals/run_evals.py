# evals/run_evals.py
# CLI Runner & Scorecard Generator for Phase 4 AI Evaluation Suite (evals/)
# Usage: uv run python -m evals.run_evals

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from dotenv import load_dotenv

# Strictly load environment configuration from .env (no hardcoded fallback defaults)
load_dotenv()

with (
    patch(
        "google.cloud.firestore.Client",
        return_value=MagicMock(_database=os.environ["FIRESTORE_DB_NAME"]),
    ),
    patch("google.cloud.firestore.AsyncClient", return_value=MagicMock()),
    patch("google.cloud.storage.Client", return_value=MagicMock()),
    patch("google.cloud.run_v2.JobsClient", return_value=MagicMock()),
    patch("google.genai.Client", return_value=MagicMock()),
    patch(
        "google.cloud.secretmanager.SecretManagerServiceClient",
        side_effect=RuntimeError("Offline"),
    ),
    patch("google.cloud.trace_v2.TraceServiceClient", return_value=MagicMock()),
):
    from evals.metrics.boundary_hygiene_eval import evaluate_boundary_hygiene
    from evals.metrics.guardrail_eval import evaluate_guardrails
    from evals.metrics.retrieval_eval import evaluate_rag_retrieval
    from evals.metrics.rubric_judge import evaluate_rubric_judge

EVALS_DIR = Path(__file__).resolve().parent
GOLDEN_DATASET_PATH = EVALS_DIR / "golden_dataset.json"
REPORT_OUTPUT_PATH = EVALS_DIR / "latest_eval_report.json"


def load_golden_dataset(path: Path = GOLDEN_DATASET_PATH) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def run_all_evaluations(write_report: bool = True) -> dict[str, Any]:
    dataset = load_golden_dataset()

    retrieval_summary = evaluate_rag_retrieval(dataset, k=5)
    boundary_summary = evaluate_boundary_hygiene(dataset)
    guardrail_summary = evaluate_guardrails(dataset)
    rubric_summary = evaluate_rubric_judge(dataset)

    all_passed = (
        retrieval_summary.passed
        and boundary_summary.passed
        and guardrail_summary.passed
        and rubric_summary.passed
    )

    report: dict[str, Any] = {
        "dataset_version": dataset.get("version", "1.0.0"),
        "all_north_star_metrics_passed": all_passed,
        "north_star_scorecard": {
            "rag_recall_at_5": retrieval_summary.mean_recall_at_5,
            "rag_precision_at_5": retrieval_summary.mean_precision_at_5,
            "rag_mrr_at_5": retrieval_summary.mrr_at_5,
            "boundary_hygiene_clean_rate": boundary_summary.positive_clean_rate,
            "boundary_defect_rejection_rate": boundary_summary.negative_rejection_rate,
            "guardrail_layer1_block_rate": guardrail_summary.layer_1_block_rate,
            "guardrail_layer2_model_armor_block_rate": guardrail_summary.layer_2_block_rate,
            "guardrail_zero_tool_call_rate": guardrail_summary.zero_tool_call_compliance_rate,
            "rubric_faithfulness_mean_score": rubric_summary.faithfulness_mean_score,
            "rubric_faithfulness_pass_rate": rubric_summary.faithfulness_pass_rate,
            "rubric_clip_rationale_mean_score": rubric_summary.rationale_mean_score,
            "rubric_clip_rationale_pass_rate": rubric_summary.rationale_pass_rate,
        },
        "suites": {
            "rag_retrieval": retrieval_summary.model_dump(mode="json"),
            "boundary_hygiene": boundary_summary.model_dump(mode="json"),
            "guardrail_security": guardrail_summary.model_dump(mode="json"),
            "rubric_judge": rubric_summary.model_dump(mode="json"),
        },
    }

    if write_report:
        with open(REPORT_OUTPUT_PATH, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)

    return report


def print_scorecard(report: dict[str, Any]) -> None:
    sc = report["north_star_scorecard"]
    status_str = "PASSED" if report["all_north_star_metrics_passed"] else "FAILED"
    print("=" * 72)
    print(f" CLIP_AGENT_OS — AI EVALUATION NORTH STAR SCORECARD [{status_str}]")
    print("=" * 72)
    print(f" 1. Multimodal RAG Recall@5               : {sc['rag_recall_at_5'] * 100:6.1f}%  (Target >= 85.0%)")
    print(f" 2. Multimodal RAG Precision@5            : {sc['rag_precision_at_5'] * 100:6.1f}%  (Target >= 85.0%)")
    print(f" 3. Multimodal RAG MRR@5                  : {sc['rag_mrr_at_5']:6.2f}   (Target >=  0.85)")
    print(f" 4. Clip Boundary Hygiene Pass Rate       : {sc['boundary_hygiene_clean_rate'] * 100:6.1f}%  (Target == 100.0%)")
    print(f" 5. Mid-Word/Sentence Defect Catch Rate   : {sc['boundary_defect_rejection_rate'] * 100:6.1f}%  (Target == 100.0%)")
    print(f" 6. Guardrail Layer 1 (Off-Topic) Block   : {sc['guardrail_layer1_block_rate'] * 100:6.1f}%  (Target == 100.0%)")
    print(f" 7. Guardrail Layer 2 (Model Armor) Block : {sc['guardrail_layer2_model_armor_block_rate'] * 100:6.1f}%  (Target == 100.0%)")
    print(f" 8. Guardrail 0-Tool-Call Compliance      : {sc['guardrail_zero_tool_call_rate'] * 100:6.1f}%  (Target == 100.0%)")
    print(f" 9. Rubric Judge — Answer Faithfulness    : {sc['rubric_faithfulness_mean_score']:4.2f}/5.0 ({sc['rubric_faithfulness_pass_rate'] * 100:.0f}% pass)")
    print(f"10. Rubric Judge — Clip Rationale Quality : {sc['rubric_clip_rationale_mean_score']:4.2f}/5.0 ({sc['rubric_clip_rationale_pass_rate'] * 100:.0f}% pass)")
    print("=" * 72)


if __name__ == "__main__":
    eval_report = run_all_evaluations(write_report=True)
    print_scorecard(eval_report)
    if not eval_report["all_north_star_metrics_passed"]:
        raise SystemExit(1)
