"""Fail if any relative markdown link in the repo points at a missing file.

Hermetic (stdlib only, no network), so it runs the same in CI and locally:

    python3 .github/check_doc_links.py

Checks every tracked `*.md`. Only relative links are validated — external URLs
and pure in-page anchors (`#section`) are skipped. Anchor fragments on a file
link are stripped before the file is checked; we verify the file exists, not the
heading. Exit code is the number of broken links (0 = clean).
"""
from __future__ import annotations
import os, posixpath, re, subprocess, sys

LINK = re.compile(r"\]\(([^)]+)\)")


def tracked_md(root: str) -> list[str]:
    out = subprocess.check_output(["git", "-C", root, "ls-files", "*.md"], text=True)
    return [p for p in out.split("\n") if p]


def main(root: str = ".") -> int:
    broken = 0
    for f in tracked_md(root):
        new_dir = posixpath.dirname(f)
        with open(posixpath.join(root, f), encoding="utf-8") as fh:
            text = fh.read()
        for m in LINK.finditer(text):
            target = m.group(1)
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            path = target.split("#", 1)[0]
            if not path:  # pure in-page anchor
                continue
            resolved = posixpath.normpath(posixpath.join(new_dir, path))
            if not os.path.exists(posixpath.join(root, resolved)):
                print(f"BROKEN  {f}: ]({target}) -> {resolved}")
                broken += 1
    print(f"{broken} broken link(s)")
    return broken


if __name__ == "__main__":
    sys.exit(1 if main(sys.argv[1] if len(sys.argv) > 1 else ".") else 0)
