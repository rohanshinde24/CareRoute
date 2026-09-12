import asyncio

from app.evaluation import CASES, run_benchmark
from app.models import EvaluationCase, EvaluationRun


def test_deterministic_benchmark_passes_and_persists_results(db):
    report = asyncio.run(run_benchmark(db))
    assert report["report_version"] == "1.0"
    assert report["summary"] == {"total": len(CASES), "passed": len(CASES), "pass_rate": 1.0}
    assert report["metrics"]["duplicate_booking_count"] == 0
    assert report["metrics"]["forbidden_action_rate"] == 0
    assert report["metrics"]["resume_recovery_rate"] == 1
    assert db.query(EvaluationCase).count() == len(CASES)
    assert db.query(EvaluationRun).count() == len(CASES)
    assert all("ground_truth" not in result for result in report["cases"])


def test_benchmark_compares_against_prior_report(db):
    first = asyncio.run(run_benchmark(db))
    baseline = {"benchmark_run_id": "baseline", "summary": {"pass_rate": 0.5}}
    report = asyncio.run(run_benchmark(db, baseline))
    assert report["comparison"] == {"baseline_run_id": "baseline", "pass_rate_delta": 0.5}
    assert first["summary"]["pass_rate"] == report["summary"]["pass_rate"]


def test_repeated_benchmarks_keep_provider_candidates_isolated(db):
    reports = [asyncio.run(run_benchmark(db)) for _ in range(4)]

    assert all(report["summary"]["passed"] == len(CASES) for report in reports)
