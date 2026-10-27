"""Make sure this PC has its own config file - used by the start_*.bat files (standard library only).

    python tools/local_config.py host [file]    IOC host file (default ioc_host/supervisor_config.yaml)
    python tools/local_config.py lab [file]     lab file      (default control/lab.yaml)

The live files are not in git (.gitignore), so a `git pull` never overwrites the real addresses of a
PC. The examples are in git: ioc_host/hosts/*.example.yaml and control/lab*.example.yaml - with
placeholder addresses, the repository is public. Real configs to share between the lab PCs belong in a
private folder with the same layout (control/, ioc_host/hosts/): FASTQ_CONFIG_EXAMPLES=<folder>[;<folder>]
(e.g. a NAS share or a private git repository), or ../FastQ-private next to this repository; their
examples are offered too.

The file exists: prints its path, exit 0. It does not: asks which example to start from (the one
whose name matches this computer is offered first), copies it, exit 2 = "edit it, then start again".
FASTQ_CONFIG_EXAMPLE=<example file> picks the example without asking (unattended installs).
"""
from __future__ import annotations

import os
import shutil
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KINDS = {
    "host": (ROOT / "ioc_host" / "supervisor_config.yaml", ROOT / "ioc_host" / "hosts", "*.example.yaml"),
    "lab": (ROOT / "control" / "lab.yaml", ROOT / "control", "lab*.example.yaml"),
}


def show(p: Path) -> str:
    try:
        return str(p.relative_to(ROOT))
    except ValueError:
        return str(p)


def roots() -> list[Path]:
    extra = [Path(d) for d in os.environ.get("FASTQ_CONFIG_EXAMPLES", "").split(os.pathsep) if d]
    return [ROOT, *extra, ROOT.parent / "FastQ-private"]


def examples(kind: str) -> list[Path]:
    """Example files (repository + private folders), the ones matching this computer's name first."""
    _, folder, pattern = KINDS[kind]
    pc = (os.environ.get("COMPUTERNAME") or socket.gethostname()).lower()

    def setup(p: Path) -> str:  # "lab.4dmag.example.yaml" -> "4dmag", "4dmag.example.yaml" -> "4dmag"
        return p.name[:-len(".example.yaml")].removeprefix("lab").strip(".").lower()

    rel = folder.relative_to(ROOT)
    found = [p for r in dict.fromkeys(roots()) if (r / rel).is_dir() for p in sorted((r / rel).glob(pattern))]
    return sorted(found, key=lambda p: not (setup(p) and (setup(p) in pc or pc in setup(p))))


def choose(kind: str, target: Path) -> Path | None:
    chosen = os.environ.get("FASTQ_CONFIG_EXAMPLE")
    if chosen:
        return Path(chosen) if Path(chosen).is_absolute() else ROOT / chosen
    options = examples(kind)
    if not options:
        return None
    print(f"{show(target)} does not exist yet - this PC's own copy, kept out of git.")
    print("Start from which example?")
    for i, p in enumerate(options, 1):
        print(f"  {i}. {show(p)}")
    if not sys.stdin.isatty():
        return None
    answer = input("number [1]: ").strip() or "1"
    try:
        return options[int(answer) - 1]
    except (ValueError, IndexError):
        return None


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in KINDS:
        print(__doc__)
        return 1
    kind = argv[0]
    target = Path(argv[1]) if len(argv) > 1 else KINDS[kind][0]
    target = target if target.is_absolute() else Path.cwd() / target
    if target.exists():
        print(target)
        return 0
    if target.resolve().parent != KINDS[kind][0].parent:  # an explicitly named file: no guessing
        print(f"{target} not found.")
        return 1
    example = choose(kind, target)
    if example is None or not example.exists():
        print(f"No example chosen - copy one of {show(KINDS[kind][1])}"
              f"{os.sep}{KINDS[kind][2]} to {show(target)} yourself.")
        return 1
    shutil.copyfile(example, target)
    print(f"\nCreated {show(target)} from {show(example)}.")
    print("Check / edit it for this PC (addresses, prefixes, data folders), then start again.")
    print("It is not in git: updates (git pull) leave it alone.")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
