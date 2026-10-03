from pathlib import Path
from types import SimpleNamespace

from app.agent_evaluation_dataset import AgentEvaluationCase, load_agent_evaluation_dataset
from app.evaluation import aggregate_metrics, score_case


def test_versioned_agent_dataset_contains_all_legacy_scenarios():
    root = Path(__file__).resolve().parents[1] / "evaluation" / "agent" / "v1"
    dataset = load_agent_evaluation_dataset(root)

    assert dataset.version == "v1"
    assert len(dataset.cases) == 32
    assert {case.id for case in dataset.cases} >= {"D01", "I01", "P01", "C01", "E01", "M01", "M02"}
    assert all(case.expected["min_evidence"] >= 1 for case in dataset.cases)


def test_automatic_agent_score_requires_evidence_artifact_and_routing():
    case = AgentEvaluationCase(
        id="fixture",
        scenario="baseline",
        category="测试",
        goal="测试自动评分",
        rubric="覆盖评分约束",
        expected={
            "valid_statuses": ["completed"],
            "min_evidence": 1,
            "require_artifact": True,
            "forbidden_tools": ["forbidden_tool"],
            "required_subtask_roles": ["channel"],
            "max_subtasks": 1,
        },
    )
    task = SimpleNamespace(
        status="completed",
        state={
            "answer": "已完成",
            "observations": [{"tool": "query_metrics", "status": "success", "evidence_id": "ev_1"}],
            "subtasks": {"subtask-1": {"role": "channel"}},
        },
    )

    passed = score_case(case, task, [SimpleNamespace(id="ar_1")])
    failed = score_case(case, task, [])
    metrics = aggregate_metrics(
        [
            {"status": "completed", "expected": case.expected, "automatic": passed, "usage": {}},
            {"status": "completed", "expected": case.expected, "automatic": failed, "usage": {}},
        ]
    )

    assert passed["passed"] is True
    assert failed["checks"]["artifact"] is False
    assert metrics["deterministic_pass_rate"] == 0.5
    assert metrics["artifact_success_rate"] == 0.5
