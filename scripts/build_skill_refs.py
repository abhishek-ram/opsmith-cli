"""Regenerates the generated references of the Agent Skill.

Run it after changing a command, a flag, an exit code or the configuration schema:

    uv run python scripts/build_skill_refs.py            # write them
    uv run python scripts/build_skill_refs.py --check    # fail if they are stale

The work is in ``opsmith/cli/skill_refs.py`` rather than here, so that the freshness test imports
the generator instead of shelling out: a broken path in CI would otherwise make the check pass by
doing nothing.
"""

import argparse
import sys

from opsmith.cli.app import app
from opsmith.cli.skill_refs import SKILL_REFERENCES_DIR, stale, write


def main() -> int:
    """
    :return: The exit code: 1 when --check found a stale reference, 0 otherwise.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Report stale references instead of rewriting them.",
    )
    arguments = parser.parse_args()

    if arguments.check:
        out_of_date = stale(app)
        if out_of_date:
            print(
                "These skill references are out of date: "
                + ", ".join(out_of_date)
                + "\nRun: uv run python scripts/build_skill_refs.py",
                file=sys.stderr,
            )
            return 1
        return 0

    for path in write(app):
        print(f"wrote {path.relative_to(SKILL_REFERENCES_DIR.parent.parent.parent)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
