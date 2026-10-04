#!/usr/bin/env python3
"""Score the generated corpus and report what is weak.

    python3 tools/quality_report.py            # summary per page kind
    python3 tools/quality_report.py --worst 20 # the worst individual pages
    python3 tools/quality_report.py --repeat   # sentences shared across many pages

This never deletes or blocks anything. It exists so the corpus can be judged rather
than assumed good, and so a data source that starts returning empty tables shows up as
a fall in scores instead of silently producing thin pages.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.quality import repetition_report, score_html, summarise  # noqa: E402


def kind_of(name: str) -> str:
    """Classify by the real filename shapes the sources produce. Guessing prefixes
    classified 974 pages as "other", which made every per-kind number useless."""
    if name.startswith("hub-"):
        return "hub"
    if "average-monthly-temperature-rainfall" in name:
        return "climate"
    if "country-data" in name:
        return "country"
    if "price-by-month" in name:
        return "crypto"
    return "holiday"


def related_count(html: str) -> int:
    """Anchors inside the related-pages section. The section carries hashed utility
    classes, but the <section class="related"> wrapper is stable."""
    section = re.search(r'<section class="related">(.*?)</section>', html, re.S | re.I)
    if not section:
        return 0
    return len(re.findall(r"<a\b", section.group(1), re.I))


def main() -> int:
    ap = argparse.ArgumentParser(description="Report pSEOare page quality")
    ap.add_argument("--output", default="output", help="rendered site directory")
    ap.add_argument("--worst", type=int, default=0, help="list the N worst pages")
    ap.add_argument("--repeat", action="store_true", help="report repeated sentences")
    args = ap.parse_args()

    root = Path(args.output)
    files = sorted(root.glob("*.html"))
    if not files:
        print(f"no html under {root}", file=sys.stderr)
        return 2

    scores = []
    prose: dict[str, str] = {}
    for path in files:
        html = path.read_text(encoding="utf-8", errors="replace")
        score = score_html(path.name, kind_of(path.name), html, related_count(html))
        scores.append(score)
        prose[path.name] = score.prose

    summary = summarise(scores)
    print(f"pages {summary['pages']}  ok {summary['ok']}  failing {summary['failing']}"
          f"  median words {summary['median_words']}")
    print(f"{'kind':10} {'pages':>6} {'failing':>8} {'med words':>10}  top problem")
    for kind, row in summary["by_kind"].items():
        print(f"{kind:10} {row['pages']:6} {row['failing']:8} {row['median_words']:10}"
              f"  {row['top_problem']}")

    if args.worst:
        print(f"\nworst {args.worst}:")
        for score in sorted(scores, key=lambda s: len(s.problems))[: args.worst]:
            print(f"  {score.path}: {'; '.join(score.problems)}")

    if args.repeat:
        print("\nmost repeated sentences:")
        for sentence, count in repetition_report(prose):
            print(f"  {count:5}  {sentence[:110]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())