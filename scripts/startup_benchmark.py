#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class StartupRunResult:
    run_no: int
    warmup: bool
    duration_seconds: float
    return_code: int
    timed_out: bool
    output_tail: str


class StartupBenchmarkService:
    def __init__(
        self,
        *,
        python_executable: str,
        target_script: Path,
        working_directory: Path,
        runs: int,
        warmups: int,
        timeout_seconds: float,
        autoquit_ms: int,
        no_style: bool,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        self._python_executable = python_executable
        self._target_script = target_script
        self._working_directory = working_directory
        self._runs = max(1, int(runs))
        self._warmups = max(0, int(warmups))
        self._timeout_seconds = max(1.0, float(timeout_seconds))
        self._autoquit_ms = max(200, int(autoquit_ms))
        self._no_style = bool(no_style)
        self._extra_env = dict(extra_env or {})

    def _build_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.setdefault("AI_IDE_AUTOQUIT_MS", str(self._autoquit_ms))
        if self._no_style:
            env.setdefault("AI_IDE_NO_STYLE", "1")
        for env_name, env_value in self._extra_env.items():
            if str(env_name).strip() and env_value is not None:
                env[str(env_name)] = str(env_value)
        return env

    @staticmethod
    def _tail_output(raw_output: str, max_lines: int = 14) -> str:
        line_list = raw_output.splitlines()
        if not line_list:
            return ""
        return "\n".join(line_list[-max_lines:])

    def _run_process(self, *, run_no: int, warmup: bool, env: dict[str, str]) -> StartupRunResult:
        started_at = time.perf_counter()
        timed_out = False
        return_code = -1
        output_text = ""
        try:
            completed = subprocess.run(
                [self._python_executable, str(self._target_script)],
                cwd=str(self._working_directory),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=self._timeout_seconds,
            )
            return_code = int(completed.returncode)
            output_text = str(completed.stdout or "")
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            output_text = str(exc.stdout or "")
            return_code = -9

        ended_at = time.perf_counter()
        return StartupRunResult(
            run_no=run_no,
            warmup=warmup,
            duration_seconds=(ended_at - started_at),
            return_code=return_code,
            timed_out=timed_out,
            output_tail=self._tail_output(output_text),
        )

    def benchmark_startup(self) -> list[StartupRunResult]:
        env = self._build_env()
        result_list: list[StartupRunResult] = []
        total_runs = self._warmups + self._runs
        for run_index in range(total_runs):
            is_warmup = run_index < self._warmups
            result_list.append(
                self._run_process(
                    run_no=run_index + 1,
                    warmup=is_warmup,
                    env=env,
                ),
            )
        return result_list


class StartupBenchmarkReportService:
    @staticmethod
    def _percentile(duration_list: list[float], p: float) -> float:
        if not duration_list:
            return 0.0
        sorted_values = sorted(duration_list)
        rank = int(math.ceil((p / 100.0) * len(sorted_values))) - 1
        rank = min(max(rank, 0), len(sorted_values) - 1)
        return sorted_values[rank]

    def build_report(self, result_list: list[StartupRunResult]) -> dict[str, Any]:
        measured_results = [result for result in result_list if not result.warmup]
        successful_results = [
            result
            for result in measured_results
            if result.return_code == 0 and not result.timed_out
        ]
        failed_results = [result for result in measured_results if result not in successful_results]

        durations = [result.duration_seconds for result in successful_results]
        report: dict[str, Any] = {
            "total_runs": len(measured_results),
            "successful_runs": len(successful_results),
            "failed_runs": len(failed_results),
            "timed_out_runs": sum(1 for result in failed_results if result.timed_out),
            "durations_seconds": durations,
        }

        if durations:
            report.update(
                {
                    "min_seconds": min(durations),
                    "max_seconds": max(durations),
                    "mean_seconds": statistics.fmean(durations),
                    "median_seconds": statistics.median(durations),
                    "p95_seconds": self._percentile(durations, 95.0),
                },
            )

        if failed_results:
            report["failed_details"] = [
                {
                    "run_no": result.run_no,
                    "return_code": result.return_code,
                    "timed_out": result.timed_out,
                    "duration_seconds": result.duration_seconds,
                    "output_tail": result.output_tail,
                }
                for result in failed_results
            ]

        return report


class StartupBenchmarkCompareService:
    _METRIC_KEY_LIST: tuple[str, ...] = (
        "min_seconds",
        "max_seconds",
        "mean_seconds",
        "median_seconds",
        "p95_seconds",
    )

    @staticmethod
    def _load_numeric_metric(report: dict[str, Any], metric_key: str) -> float | None:
        metric_value = report.get(metric_key)
        if isinstance(metric_value, (int, float)):
            return float(metric_value)
        return None

    def _build_metric_delta(
        self,
        *,
        report_a: dict[str, Any],
        report_b: dict[str, Any],
        metric_key: str,
    ) -> dict[str, float] | None:
        metric_a = self._load_numeric_metric(report_a, metric_key)
        metric_b = self._load_numeric_metric(report_b, metric_key)
        if metric_a is None or metric_b is None:
            return None
        delta_seconds = metric_b - metric_a
        delta_percent = ((delta_seconds / metric_a) * 100.0) if metric_a != 0 else 0.0
        return {
            "from_seconds": metric_a,
            "to_seconds": metric_b,
            "delta_seconds": delta_seconds,
            "delta_percent": delta_percent,
        }

    def build_compare_report(
        self,
        *,
        label_a: str,
        report_a: dict[str, Any],
        label_b: str,
        report_b: dict[str, Any],
    ) -> dict[str, Any]:
        metric_delta_map: dict[str, dict[str, float]] = {}
        for metric_key in self._METRIC_KEY_LIST:
            metric_delta = self._build_metric_delta(
                report_a=report_a,
                report_b=report_b,
                metric_key=metric_key,
            )
            if isinstance(metric_delta, dict):
                metric_delta_map[metric_key] = metric_delta

        faster_profile_by_median = "unknown"
        median_a = self._load_numeric_metric(report_a, "median_seconds")
        median_b = self._load_numeric_metric(report_b, "median_seconds")
        if median_a is not None and median_b is not None:
            if median_a < median_b:
                faster_profile_by_median = label_a
            elif median_b < median_a:
                faster_profile_by_median = label_b
            else:
                faster_profile_by_median = "tie"

        return {
            "mode": "compare",
            "profile_a": {
                "label": label_a,
                "report": report_a,
            },
            "profile_b": {
                "label": label_b,
                "report": report_b,
            },
            "delta": {
                "metrics": metric_delta_map,
                "faster_profile_by_median": faster_profile_by_median,
            },
        }


def _resolve_default_target_script() -> Path:
    repo_root = Path(__file__).resolve().parents[1]
    return (repo_root / "ALDE" / "alde" / "ai_ide_v1756.py").resolve()


def _parse_env_overrides(env_item_list: list[str]) -> dict[str, str]:
    env_map: dict[str, str] = {}
    for env_item in env_item_list:
        if "=" not in env_item:
            continue
        env_name, env_value = env_item.split("=", 1)
        normalized_name = env_name.strip()
        if not normalized_name:
            continue
        env_map[normalized_name] = env_value
    return env_map


def _load_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark startup latency for ai_ide_v1756.py with median and p95 output.",
    )
    parser.add_argument(
        "--python-executable",
        default=sys.executable,
        help="Python interpreter used to launch the target script.",
    )
    parser.add_argument(
        "--target-script",
        default=str(_resolve_default_target_script()),
        help="Absolute or relative path to the startup target script.",
    )
    parser.add_argument(
        "--cwd",
        default=str(Path(__file__).resolve().parents[1]),
        help="Working directory used for subprocess startup runs.",
    )
    parser.add_argument("--runs", type=int, default=5, help="Number of measured runs.")
    parser.add_argument("--warmups", type=int, default=1, help="Number of warmup runs not included in stats.")
    parser.add_argument("--timeout-seconds", type=float, default=30.0, help="Timeout for one startup run.")
    parser.add_argument("--autoquit-ms", type=int, default=2500, help="Set AI_IDE_AUTOQUIT_MS for benchmark runs.")
    parser.add_argument(
        "--no-style",
        action="store_true",
        help="Set AI_IDE_NO_STYLE=1 for clean autoquit behavior during startup measurement.",
    )
    parser.add_argument(
        "--env",
        action="append",
        default=[],
        help="Additional environment overrides in KEY=VALUE form.",
    )
    parser.add_argument(
        "--json-only",
        action="store_true",
        help="Print only JSON report without per-run lines.",
    )
    parser.add_argument(
        "--compare",
        action="store_true",
        help="Run two benchmark profiles and print an A/B comparison report.",
    )
    parser.add_argument(
        "--label-a",
        default="A",
        help="Display label for compare profile A.",
    )
    parser.add_argument(
        "--label-b",
        default="B",
        help="Display label for compare profile B.",
    )
    parser.add_argument(
        "--env-a",
        action="append",
        default=[],
        help="Profile A environment overrides in KEY=VALUE form.",
    )
    parser.add_argument(
        "--env-b",
        action="append",
        default=[],
        help="Profile B environment overrides in KEY=VALUE form.",
    )
    return parser.parse_args()


def _print_run_summary(result: StartupRunResult, *, label: str | None = None) -> None:
    run_kind = "warmup" if result.warmup else "measure"
    status = "ok"
    if result.timed_out:
        status = "timeout"
    elif result.return_code != 0:
        status = f"rc={result.return_code}"
    label_prefix = f"[{label}] " if label else ""
    print(
        f"{label_prefix}run={result.run_no:02d} kind={run_kind:7s} "
        f"duration={result.duration_seconds:.3f}s status={status}",
    )


def _run_profile(
    *,
    profile_label: str,
    args: argparse.Namespace,
    target_script: Path,
    working_directory: Path,
    env_overrides: dict[str, str],
    report_service: StartupBenchmarkReportService,
) -> tuple[list[StartupRunResult], dict[str, Any]]:
    benchmark_service = StartupBenchmarkService(
        python_executable=str(args.python_executable),
        target_script=target_script,
        working_directory=working_directory,
        runs=int(args.runs),
        warmups=int(args.warmups),
        timeout_seconds=float(args.timeout_seconds),
        autoquit_ms=int(args.autoquit_ms),
        no_style=bool(args.no_style),
        extra_env=env_overrides,
    )
    result_list = benchmark_service.benchmark_startup()
    report = report_service.build_report(result_list)
    if not args.json_only:
        print(f"=== Startup Benchmark Runs [{profile_label}] ===")
        for result in result_list:
            _print_run_summary(result, label=profile_label)
        print(f"=== Startup Benchmark Summary [{profile_label}] ===")
    return result_list, report


def main() -> int:
    args = _load_args()
    target_script = Path(str(args.target_script)).expanduser().resolve()
    working_directory = Path(str(args.cwd)).expanduser().resolve()

    if not target_script.exists():
        print(f"[error] target script not found: {target_script}")
        return 2

    report_service = StartupBenchmarkReportService()
    shared_env_overrides = _parse_env_overrides(list(args.env or []))

    if not bool(args.compare):
        _result_list, report = _run_profile(
            profile_label="single",
            args=args,
            target_script=target_script,
            working_directory=working_directory,
            env_overrides=shared_env_overrides,
            report_service=report_service,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if int(report.get("failed_runs", 0)) == 0 else 1

    compare_service = StartupBenchmarkCompareService()
    label_a = str(args.label_a or "A").strip() or "A"
    label_b = str(args.label_b or "B").strip() or "B"
    profile_a_env = {
        **shared_env_overrides,
        **_parse_env_overrides(list(args.env_a or [])),
    }
    profile_b_env = {
        **shared_env_overrides,
        **_parse_env_overrides(list(args.env_b or [])),
    }

    _results_a, report_a = _run_profile(
        profile_label=label_a,
        args=args,
        target_script=target_script,
        working_directory=working_directory,
        env_overrides=profile_a_env,
        report_service=report_service,
    )
    _results_b, report_b = _run_profile(
        profile_label=label_b,
        args=args,
        target_script=target_script,
        working_directory=working_directory,
        env_overrides=profile_b_env,
        report_service=report_service,
    )
    compare_report = compare_service.build_compare_report(
        label_a=label_a,
        report_a=report_a,
        label_b=label_b,
        report_b=report_b,
    )
    print(json.dumps(compare_report, ensure_ascii=False, indent=2))

    failed_run_count = int(report_a.get("failed_runs", 0)) + int(report_b.get("failed_runs", 0))
    return 0 if failed_run_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
