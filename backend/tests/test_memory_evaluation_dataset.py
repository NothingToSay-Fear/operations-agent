from pathlib import Path

from app.memory_evaluation import _metrics
from app.memory_evaluation_dataset import load_memory_evaluation_dataset


def test_versioned_memory_dataset_has_fifty_cases_across_memory_capabilities():
    root = Path(__file__).resolve().parents[1] / "evaluation" / "memory" / "v1"
    dataset = load_memory_evaluation_dataset(root)

    assert dataset.version == "v2"
    assert dataset.history_top_k == 4
    assert len(dataset.cases) == 50
    assert {case.kind for case in dataset.cases} == {"context", "history", "summary", "lifecycle"}


def test_memory_metrics_count_missing_required_context_as_failure():
    metrics = _metrics(
        [
            {
                "kind": "context",
                "constraint_results": {"dimensions": False},
                "required_memory_ids": ["memory-a"],
                "selected_memory_ids": [],
                "leaked_memory_ids": [],
                "forbidden_constraint_terms": [],
            },
            {"kind": "history", "anchor_hit": True, "cross_task_leak": False},
            {"kind": "lifecycle", "success": True},
        ]
    )

    assert metrics["context_required_recall"] == 0.0
    assert metrics["long_term_memory_recall"] == 0.0
