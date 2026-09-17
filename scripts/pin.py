#!/usr/bin/env python3
"""Stamp the launcher with a clean package commit already pushed to main."""
from pathlib import Path
import re
import subprocess
import tomllib


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def main():
    root = Path(__file__).resolve().parents[1]
    if git(root, "status", "--porcelain"):
        raise SystemExit("Refusing to pin a dirty working tree; commit and push first")
    if git(root, "branch", "--show-current") != "main":
        raise SystemExit("Stamp the release from main")
    revision = git(root, "rev-parse", "HEAD")
    remote = git(root, "ls-remote", "https://github.com/getcolors/context-sql.git", "refs/heads/main")
    if not remote or remote.split()[0] != revision:
        raise SystemExit("HEAD must equal the published context-sql main commit")
    with (root / "pyproject.toml").open("rb") as handle:
        config = tomllib.load(handle)
    blue_revision = config["tool"]["uv"]["sources"]["blue"]["rev"]
    if not re.fullmatch(r"[0-9a-f]{40}", blue_revision):
        raise SystemExit("Blue must have an exact Git revision")
    dependency = f"blue @ git+https://github.com/getcolors/blue.git@{blue_revision}"
    if dependency not in config["project"]["dependencies"]:
        raise SystemExit("Blue dependency and tool.uv.sources revisions disagree")
    launcher = root / "skills/package-context-sql-blue/blue"
    content = launcher.read_text()
    for key, value in (("package_revision", revision), ("blue_revision", blue_revision)):
        content, count = re.subn(rf"^{key}='[^']*'$", f"{key}='{value}'", content, flags=re.MULTILINE)
        if count != 1:
            raise SystemExit(f"Expected one {key} assignment in launcher")
    launcher.write_text(content)
    launcher.chmod(0o755)
    print(revision)


if __name__ == "__main__":
    main()
