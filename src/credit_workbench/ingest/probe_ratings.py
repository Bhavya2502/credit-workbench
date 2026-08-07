"""Tracker H2 — feasibility probe for NRSRO rating histories.

Rule 17g-7(b) obliges every nationally recognised rating agency to publish its full
rating action history since June 2007 as XBRL, on its own website. That makes the data
free, but unlike the SEC bulk files there is no single endpoint: ten agencies, ten
sites, ten shapes, and links that move.

This probe establishes what is actually reachable before any loader is written — the
SEC's own index of agency disclosure pages, then each agency page, looking for the
XBRL or ZIP the rule requires.
"""
from __future__ import annotations

import re

import httpx

from credit_workbench.common.config import sec_user_agent

SEC_INDEX = ("https://www.sec.gov/about/divisions-offices/office-credit-ratings/"
             "disclosure-of-credit-rating-histories")

# Agency disclosure pages as published by the SEC. Probed directly because the SEC
# index page is the authority on where each one lives.
AGENCY_PAGES = {
    "S&P Global Ratings": "https://disclosure.spglobal.com/ratings/en/regulatory/ratings-history",
    "Moody's": "https://ratings.moodys.com/sec-17g-7b",
    "Fitch": "https://www.fitchratings.com/regulatory",
    "Morningstar DBRS": "https://ratingagency.morningstar.com/mcr/regulatory/Credit-Ratings-History",
    "KBRA": "https://www.kbra.com/regulatory/17g-7b",
    "AM Best": "https://web.ambest.com/ratings-services/rule-17g-7b",
    "Egan-Jones": "https://www.egan-jones.com/regulatory",
    "HR Ratings": "https://www.hrratings.com/regulation/",
    "Japan Credit Rating Agency": "https://www.jcr.co.jp/en/",
}

LINK_RE = re.compile(r'href="([^"]+\.(?:zip|xml|xbrl|xlsx|csv))"', re.IGNORECASE)


def main() -> None:
    headers = {"User-Agent": sec_user_agent(),
               "Accept": "text/html,application/xhtml+xml"}
    with httpx.Client(headers=headers, timeout=60, follow_redirects=True) as client:
        print("### SEC index of agency disclosure pages")
        try:
            resp = client.get(SEC_INDEX)
            print(f"  HTTP {resp.status_code}, {len(resp.text):,} bytes")
            hrefs = re.findall(r'href="(https?://[^"]+)"', resp.text)
            external = sorted({h for h in hrefs
                               if "sec.gov" not in h and len(h) < 120})
            print(f"  {len(external)} external links; those that look like agencies:")
            for h in external:
                if any(k in h.lower() for k in
                       ("rating", "moody", "fitch", "kbra", "ambest", "egan",
                        "morningstar", "spglobal", "jcr", "hrratings", "demotech")):
                    print(f"    {h}")
        except Exception as exc:  # noqa: BLE001
            print(f"  (failed: {exc})")

        print("\n### Agency pages — reachability and downloadable files")
        for name, url in AGENCY_PAGES.items():
            try:
                resp = client.get(url)
                files = sorted({f if f.startswith("http") else "(relative) " + f
                                for f in LINK_RE.findall(resp.text)})[:4]
                print(f"  [{resp.status_code}] {name}")
                for f in files:
                    print(f"        {f[:110]}")
                if not files:
                    print("        (no direct file links in the HTML — likely behind "
                          "a form, login, or JavaScript)")
            except Exception as exc:  # noqa: BLE001
                print(f"  [ERR] {name}: {type(exc).__name__} {exc}")


if __name__ == "__main__":
    main()
