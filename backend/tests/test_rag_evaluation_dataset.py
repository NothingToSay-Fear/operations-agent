from pathlib import Path

from app.document_parser import parse_document
from app.rag_evaluation import _aggregate_metrics
from app.rag_evaluation_dataset import load_rag_evaluation_dataset


def test_versioned_rag_dataset_covers_long_documents_and_all_supported_formats():
    root = Path(__file__).resolve().parents[1] / "evaluation" / "rag" / "v1"
    dataset = load_rag_evaluation_dataset(root)

    assert dataset.version == "v5"
    assert dataset.top_k == 5
    assert len(dataset.documents) == 60
    assert len(dataset.cases) == 110
    assert {source.path.suffix for source in dataset.documents} == {".md", ".txt", ".csv", ".pdf", ".docx"}
    assert sum(source.path.stat().st_size for source in dataset.documents) > 500 * 1024
    assert any(case.expects_answer for case in dataset.cases)
    assert any(not case.expects_answer for case in dataset.cases)
    assert all(case.expected_anchors for case in dataset.cases if case.expects_answer)
    parsed = [parse_document(source.path.name, source.path.read_bytes()) for source in dataset.documents]
    contents = {source.id: document.content for source, document in zip(dataset.documents, parsed, strict=True)}
    assert all(len(document.content) >= 1000 for document in parsed)
    assert sum(len(document.chunks) for document in parsed) >= 400
    assert any(chunk.content_type == "table" for document in parsed for chunk in document.chunks)
    assert any(chunk.page_start is not None for document in parsed for chunk in document.chunks)
    for case in dataset.cases:
        if case.expects_answer:
            expected_content = "\n".join(contents[identifier] for identifier in case.expected_document_ids)
            assert all(anchor in expected_content for anchor in case.expected_anchors)


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
