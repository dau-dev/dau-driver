"""Every python block in the tutorials runs as written, in order.

The tutorials are the pages that promise to work without a board, against the
fake transport, so they are the ones run here; the how-to and reference pages
show fragments for a real device. A page is one session: its blocks share a
namespace, the way a reader typing them into one prompt would, and it runs in
a scratch directory. Where a page shows what a block prints (an untagged or
``text`` fence right after it, with at most a lead-in line such as
"**Output:**" between), the output must match. A block marked with an
``<!-- excerpt -->`` comment is skipped.
"""

from __future__ import annotations

import contextlib
import io
import re
from pathlib import Path

import pytest

DOCS = Path(__file__).resolve().parents[2] / "docs" / "src"
PAGES = sorted((DOCS / "tutorial").glob("*.md"))
FENCE = re.compile(r"(<!-- excerpt -->\s*)?```(\w*)\n(.*?)```", re.DOTALL)
LEAD_IN = re.compile(r"\s*(?:[^\n]*:[^\n]*\s*)?")


def _runs(page: Path) -> list[tuple[str, str | None]]:
    """The page's runnable python blocks, each with the output shown after it."""
    text = page.read_text(encoding="utf-8")
    fences = list(FENCE.finditer(text))
    runs = []
    for index, match in enumerate(fences):
        if match.group(2) != "python" or match.group(1):
            continue
        following = fences[index + 1] if index + 1 < len(fences) else None
        shown = None
        if (
            following is not None
            and following.group(2) in ("", "text")
            and not following.group(1)
            and LEAD_IN.fullmatch(text[match.end() : following.start()])
        ):
            shown = following.group(3)
        runs.append((match.group(3), shown))
    return runs


def test_the_tutorials_are_found_and_check_output() -> None:
    assert len(PAGES) == 2, PAGES
    assert all(any(shown is not None for _body, shown in _runs(page)) for page in PAGES)


@pytest.mark.parametrize("page", PAGES, ids=[f"{page.parent.name}/{page.stem}" for page in PAGES])
def test_the_page_runs_as_written(page: Path, tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    namespace: dict = {}
    for number, (body, shown) in enumerate(_runs(page), start=1):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            exec(compile(body, f"{page.name} block {number}", "exec"), namespace)  # noqa: S102 (the docs are ours)
        if shown is not None:
            assert out.getvalue().strip() == shown.strip(), f"{page.name} block {number} printed something other than the page shows"
