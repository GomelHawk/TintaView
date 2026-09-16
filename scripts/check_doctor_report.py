"""Assert that a `tintaview doctor --json` document describes a healthy install.

Used by CI's install smoke jobs (see `.github/workflows/ci.yml`), which run the real
installer on a clean runner and then ask `doctor` whether the result works. It lives here,
as a file both the shell and the PowerShell job can call, rather than as an inline snippet
in each — the two would drift, and one of them would drift into passing vacuously.

The exit code alone is not enough to assert on: a report that never ran the checks at all
would also exit 0 on the day someone breaks the runner.
"""

from __future__ import annotations

import json
import sys

#: Every section a working install must have reported on. `ENGINE` and `STATS` are
#: deliberately absent: a CI runner has no lighting hardware and no signed-in agent, so
#: both are expected to warn.
REQUIRED_SECTIONS = {"ENVIRONMENT", "CONFIG", "DAEMON", "HOOK SCRIPT", "AGENT HOOKS"}


def main(path: str) -> int:
    with open(path, encoding="utf-8") as fh:
        report = json.load(fh)

    problems = []
    failed = [c for c in report.get("checks", []) if c.get("level") == "fail"]
    for check in failed:
        problems.append(f"FAIL {check['section']}: {check['message']}")

    sections = {c.get("section") for c in report.get("checks", [])}
    missing = REQUIRED_SECTIONS - sections
    if missing:
        problems.append(f"doctor never reported on: {', '.join(sorted(missing))}")
    if report.get("ok") is not True:
        problems.append(f"report says ok={report.get('ok')!r}")

    if problems:
        print("\n".join(problems), file=sys.stderr)
        print(json.dumps(report, indent=2), file=sys.stderr)
        return 1
    print(f"doctor: {len(report['checks'])} checks, {report['warns']} warning(s), none failed")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: check_doctor_report.py <doctor.json>", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
