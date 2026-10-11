"""JSON finite-grid reconstruction: estimated states and feasible bounds."""

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from crocodile.core.reconstruction import (
    LinearObservation,
    TradeExecutionBound,
    WindowProblem,
    solve_window,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--asof-ns", type=int, required=True)
    parser.add_argument("--mode", choices=["causal", "retrospective"], default="causal")
    parser.add_argument("--revision-id")
    parser.add_argument("--max-seconds", type=float, default=1)
    parser.add_argument("--max-iterations", type=int, default=2000)
    args = parser.parse_args()
    raw = json.loads(args.input.read_text(encoding="utf-8"))
    problem = WindowProblem(
        tuple(raw["times_ns"]),
        tuple(raw["durations_ns"]),
        tuple(tuple(row) for row in raw["prior"]),
        tuple(
            LinearObservation(
                tuple(tuple(pair) for pair in o["coefficients"]),
                o["quantity"],
                o["observed_at_ns"],
                o["available_at_ns"],
                o["name"],
                o.get("tolerance", 0),
            )
            for o in raw.get("observations", [])
        ),
        {int(k): v for k, v in raw.get("fixed", {}).items()},
        tuple(raw.get("forbidden", [])),
        tuple(raw["upper"]) if raw.get("upper") is not None else None,
        tuple(TradeExecutionBound(**x) for x in raw.get("executions", [])),
    )
    result = solve_window(
        problem,
        asof_ns=args.asof_ns,
        mode=args.mode,
        revision_id=args.revision_id,
        max_seconds=args.max_seconds,
        max_iterations=args.max_iterations,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(asdict(result), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
    )
    print(
        json.dumps(
            {"status": result.status, "residual": result.residual, "provenance": result.provenance}
        )
    )


if __name__ == "__main__":
    main()
