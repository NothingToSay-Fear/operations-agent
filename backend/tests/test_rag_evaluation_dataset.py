from pathlib import Path

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
