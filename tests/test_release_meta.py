"""版番号のそろい方。リリース（.github/workflows/release.yml）は pyproject.toml の version でタグを付け、
CHANGELOG.md の同じ版の節をリリースノートにする。どれかがずれると、違う版のタグや空のノートができる。"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

import tenugui

ROOT = Path(__file__).resolve().parent.parent


def _version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


def test_package_version_matches_pyproject():
    assert tenugui.__version__ == _version()


def test_changelog_has_a_section_for_this_version():
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    heads = re.findall(r"^## \[([^\]]+)\]", text, flags=re.M)
    assert heads and heads[0] == _version(), f"CHANGELOG.md の先頭の節 {heads[:1]} が版 {_version()} と違います"
    body = text.split(f"## [{_version()}]", 1)[1].split("\n## [", 1)[0]
    assert body.strip(), "CHANGELOG.md の節が空です（リリースノートが空になります）"


def test_agent_reference_names_this_version():
    head = (ROOT / "AGENT_REFERENCE.md").read_text(encoding="utf-8").splitlines()[:5]
    assert any(f"バージョン {_version()}" in line for line in head)
