from pathlib import Path

from app.rag_evaluation import _aggregate_metrics
from app.rag_evaluation_dataset import load_rag_evaluation_dataset


def test_versioned_rag_dataset_has_positive_and_no_answer_cases():
    root = Path(__file__).resolve().parents[1] / "evaluation" / "rag" / "v1"
    dataset = load_rag_evaluation_dataset(root)

    assert dataset.version == "v1"
    assert dataset.top_k == 5
    assert any(source.path.suffix == ".csv" for source in dataset.documents)
    assert any(case.expects_answer for case in dataset.cases)
    assert any(not case.expects_answer for case in dataset.cases)
    assert all(case.expected_anchors for case in dataset.cases if case.expects_answer)


def test_rag_metrics_include_ts_rank_cd_anchor_recall():
    metrics = _aggregate_metrics(
        [
            {
                "expected_document_ids": ["promotion"],
                "rank": 1,
                "ts_rank_cd_rank": 1,
                "anchor_matches": {"毛利率不得低于百分之二十": True},
                "ts_rank_cd_anchor_matches": {"毛利率不得低于百分之二十": False},
                "no_answer_false_positive": None,
                "ts_rank_cd_no_answer_false_positive": None,
                "elapsed_ms": 10,
            },
            {
                "expected_document_ids": [],
                "rank": None,
                "ts_rank_cd_rank": None,
                "anchor_matches": {},
                "ts_rank_cd_anchor_matches": {},
                "no_answer_false_positive": False,
                "ts_rank_cd_no_answer_false_positive": True,
                "elapsed_ms": 20,
            },
        ]
    )

    assert metrics["anchor_recall_at_5"] == 1.0
    assert metrics["ts_rank_cd_anchor_recall_at_5"] == 0.0
