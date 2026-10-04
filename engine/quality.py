"""Per-page quality scoring: generate, evaluate, publish.

Publishing used to be unconditional -- whatever the sources produced went to disk and
into the sitemap. That is how a corpus reaches thousands of pages while nobody has ever
checked whether any of them are worth indexing.

This module scores each rendered page and reports the result. It deliberately does not
block publishing: a gate that rejected pages would shrink a corpus of thousands on the
strength of a threshold nobody has validated against real search data, which is a far
worse failure than publishing a mediocre page. The scores exist to be *read* -- a
distribution that collapses tells you a data source has degraded, and a page that scores
badly tells you which template to fix.

Run standalone to score the existing corpus:

    python3 tools/quality_report.py
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

# A page must clear all of these to be considered healthy. They are deliberately
# modest: the point is to catch a regression that empties a section, not to certify
# quality against Google's judgement.
MIN_WORDS = 250
MIN_FACTS = 4
MIN_RELATED = 3

_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'’\-]*")


@dataclass
class PageScore:
    path: str
    kind: str
    words: int = 0
    facts: int = 0
    related: int = 0
    has_table: bool = False
    has_schema: bool = False
    has_canonical: bool = False
    has_description: bool = False
    prose: str = ""
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def visible_words(html: str) -> int:
    """Words a reader would see: scripts, styles and markup removed."""
    text = re.sub(r"<script\b.*?</script>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<style\b.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return len(_WORD_RE.findall(text))


def extract_prose(html: str) -> str:
    """The main body copy, used for cross-page repetition checks.

    Ad zones are stripped first. The house ad is deliberately identical on every page, so
    leaving it in made the single most-repeated "sentence" on the site a sentence the
    writer never wrote."""
    body = re.sub(r"<aside\b.*?</aside>", " ", html, flags=re.S | re.I)
    body = re.sub(r"<script\b.*?</script>", " ", body, flags=re.S | re.I)
    body = re.sub(r"<style\b.*?</style>", " ", body, flags=re.S | re.I)
    body = re.sub(r"<[^>]+>", " ", body)
    return " ".join(body.split())


def score_html(path: str, kind: str, html: str, related: int = 0) -> PageScore:
    """Score one rendered page. Everything here is read from the output HTML, so it
    measures what a crawler and a reader actually get rather than what the build
    intended to produce."""
    score = PageScore(path=path, kind=kind)
    score.words = visible_words(html)
    score.related = related
    score.has_table = "<table" in html.lower()
    score.has_schema = "application/ld+json" in html.lower()
    score.has_canonical = 'rel="canonical"' in html.lower()
    desc = re.search(r'<meta[^>]+name=["\']description["\'][^>]*content=["\'](.+?)["\']', html, re.I | re.S)
    score.has_description = bool(desc and len(desc.group(1).strip()) > 40)
    score.prose = extract_prose(html)

    # Facts render as <div><strong>Label</strong><br>Value</div> inside the "Key figures"
    # section, not as a definition list. Counting <dt> found none and made every page look
    # factless.
    score.facts = len(re.findall(r"<strong>[^<]{2,60}</strong><br>", html, re.I))

    # Hubs are navigation, not articles: a short hub is a correctly built hub.
    if score.words < MIN_WORDS and kind != "hub":
        score.problems.append(f"thin: {score.words} words (<{MIN_WORDS})")
    # A hub carries no dataset, so it has no key-figures block to count.
    if score.facts < MIN_FACTS and kind != "hub":
        score.problems.append(f"few facts: {score.facts} (<{MIN_FACTS})")
    if score.related < MIN_RELATED:
        score.problems.append(f"few related links: {score.related} (<{MIN_RELATED})")
    # A holiday page states one date, so it has no table to render, and a hub is a list
    # of links by design. Requiring a table of them would flag two healthy kinds.
    if not score.has_table and kind not in ("holiday", "hub"):
        score.problems.append("no data table")
    if not score.has_schema:
        score.problems.append("no JSON-LD")
    if not score.has_canonical:
        score.problems.append("no canonical")
    if not score.has_description:
        score.problems.append("missing or short meta description")
    return score


def repetition_report(prose_by_path: dict[str, str]) -> list[tuple[str, int]]:
    """Sentences shared by many different pages.

    This is the check that catches the failure mode fixed on 2026-10-04, when roughly
    2,600 climate pages drew their sentences from a fixed pool. A sentence appearing on
    dozens of unrelated URLs is boilerplate; the number returned is how many pages share
    the most repeated sentence.
    """
    counter: Counter[str] = Counter()
    for prose in prose_by_path.values():
        for sentence in re.split(r"(?<=[.!?])\s+", prose):
            cleaned = sentence.strip()
            # Short fragments overlap legitimately; only long sentences are evidence.
            if len(cleaned) >= 60:
                counter[cleaned] += 1
    return counter.most_common(10)


def summarise(scores: list[PageScore]) -> dict[str, Any]:
    """Aggregate scores into the numbers worth reporting per run."""
    by_kind: dict[str, list[PageScore]] = {}
    for score in scores:
        by_kind.setdefault(score.kind, []).append(score)
    return {
        "pages": len(scores),
        "ok": sum(1 for s in scores if s.ok),
        "failing": sum(1 for s in scores if not s.ok),
        "median_words": sorted(s.words for s in scores)[len(scores) // 2] if scores else 0,
        "by_kind": {
            kind: {
                "pages": len(rows),
                "failing": sum(1 for r in rows if not r.ok),
                "median_words": sorted(r.words for r in rows)[len(rows) // 2],
                "top_problem": Counter(
                    p.split(":")[0] for r in rows for p in r.problems
                ).most_common(1)[0][0]
                if any(r.problems for r in rows)
                else "",
            }
            for kind, rows in sorted(by_kind.items())
        },
    }