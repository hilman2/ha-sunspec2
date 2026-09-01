"""Bring the embedded SunSpec model definitions up to date with sunspec/models.

Usage: python sync_models.py <clone of https://github.com/sunspec/models> --out <dir>

Replaces custom_components/sunspec2/pysunspec2/models/json/ with the clone's
json/ directory. When any definition changed, records the clone's commit in
models/UPSTREAM and bumps the patch version in const.py and manifest.json.
When nothing changed, the tree is left as it was.

Writes into <dir>, for the workflow that opens the pull request:

    changed          "true" or "false"
    commit_msg.txt   subject and body for the commit
    pr_body.md       what changed, and the upstream commits behind it

Run from any directory; paths are taken relative to this file. Needs git,
and uses gh for the upstream log when it is available and authenticated.
"""

import argparse
import pathlib
import re
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "custom_components" / "sunspec2"
JSON_DIR = PACKAGE / "pysunspec2" / "models" / "json"
POINTER = JSON_DIR.parent / "UPSTREAM"
VERSION_FILES = (PACKAGE / "const.py", PACKAGE / "manifest.json")
UPSTREAM_REPO = "sunspec/models"


def git(*args, cwd=ROOT):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def recorded_commit():
    """The upstream commit the embedded definitions came from, or None."""
    if not POINTER.is_file():
        return None
    for line in POINTER.read_text(encoding="utf-8").splitlines():
        if line.startswith("commit "):
            return line.split()[1]
    return None


def replace_definitions(clone):
    for old in JSON_DIR.glob("*.json"):
        old.unlink()
    for new in sorted((clone / "json").glob("*.json")):
        shutil.copyfile(new, JSON_DIR / new.name)


def changed_files():
    """(status, name) per definition that differs from the index."""
    out = git("status", "--porcelain", "--", str(JSON_DIR))
    changes = []
    for line in out.splitlines():
        status, path = line[:2].strip(), line[3:]
        changes.append((status, pathlib.Path(path).name))
    return changes


def bump_patch_version():
    """0.31.0 becomes 0.31.1 in both files hassfest compares."""
    pattern = re.compile(r'(VERSION = "|"version": ")(\d+)\.(\d+)\.(\d+)(")')
    new_version = None
    for path in VERSION_FILES:
        text = path.read_text(encoding="utf-8")
        match = pattern.search(text)
        if match is None:
            sys.exit(f"no version in {path}")
        major, minor, patch = match.group(2), match.group(3), int(match.group(4))
        new_version = f"{major}.{minor}.{patch + 1}"
        text = (
            text[: match.start()]
            + f"{match.group(1)}{new_version}{match.group(5)}"
            + text[match.end() :]
        )
        path.write_text(text, encoding="utf-8")
    return new_version


def upstream_log(old, new):
    """Subjects of the upstream commits between the two, oldest first.

    Goes through the GitHub API rather than the clone, which is shallow.
    Empty when gh is missing or cannot reach the API; the pull request
    then names the two commits and nothing in between.
    """
    if old is None:
        return []
    try:
        out = subprocess.run(
            [
                "gh",
                "api",
                f"repos/{UPSTREAM_REPO}/compare/{old}...{new}",
                "--jq",
                '.commits[].commit.message | split("\n")[0]',
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return []
    return [s for s in out.splitlines() if s.strip() and not s.startswith("Merge ")]


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("clone", type=pathlib.Path)
    parser.add_argument("--out", type=pathlib.Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    old = recorded_commit()
    new = git("rev-parse", "HEAD", cwd=args.clone)
    new_date = git("log", "-1", "--format=%cs", cwd=args.clone)

    replace_definitions(args.clone)
    changes = changed_files()
    if not changes:
        (args.out / "changed").write_text("false\n", encoding="utf-8")
        print(f"embedded definitions already match {UPSTREAM_REPO} {new[:7]}")
        return

    POINTER.write_text(
        f"source https://github.com/{UPSTREAM_REPO}\ncommit {new}\ndate {new_date}\n",
        encoding="utf-8",
    )
    version = bump_patch_version()
    log = upstream_log(old, new)

    words = {"M": "changed", "A": "added", "D": "removed", "??": "added"}
    listing = "\n".join(f"- {name}: {words.get(status, status)}" for status, name in changes)
    subject = (
        f"chore: v{version} - SunSpec model definitions from {UPSTREAM_REPO} {new[:7]} ({new_date})"
    )
    since = f"since {old[:7]}" if old else "first recorded commit"
    log_block = "\n".join(f"- {s}" for s in log) if log else "(not available)"

    (args.out / "commit_msg.txt").write_text(
        f"{subject}\n\n"
        f"{len(changes)} definition(s) differ from the embedded copy. Upstream\n"
        f"commits {since}:\n\n{log_block}\n\nFiles:\n\n{listing}\n",
        encoding="utf-8",
    )
    (args.out / "pr_body.md").write_text(
        f"TL;DR: {len(changes)} SunSpec model definition(s) changed upstream "
        f"{since}. Opened by the models workflow; version bumped to {version}.\n\n"
        f"Upstream commits:\n\n{log_block}\n\nFiles:\n\n{listing}\n\n"
        "CI was dispatched on this branch by the workflow, because a push made "
        "with the workflow token starts no pull_request run on its own.\n",
        encoding="utf-8",
    )
    (args.out / "changed").write_text("true\n", encoding="utf-8")
    print(f"{len(changes)} definition(s) changed, version {version}, upstream {new[:7]}")


if __name__ == "__main__":
    main()
