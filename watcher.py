#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import csv
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "outputs"
OUTPUT_DIR.mkdir(exist_ok=True)
OUT_JSON = OUTPUT_DIR / "sales.json"

DEFAULT_KEYWORDS = [
    "SALE", "세일", "SEASON", "SEASON OFF", "OFF", "할인", "%", "CLEARANCE", "클리어런스",
    "REFURB", "리퍼브", "B-GRADE", "B GRADE", "아울렛", "OUTLET", "EVENT", "UP TO", "최대", "회원", "MEMBERS"
]

UPTO_PATTERNS = [
    r"UP\s*TO\s*(\d{1,2})\s*%",
    r"최대\s*(\d{1,2})\s*%",
    r"(\d{1,2})\s*%\s*OFF",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Safari/537.36"
}

TIMEOUT = 25


@dataclass
class Brand:
    name: str
    url: str
    country: str = "KR"  # "KR" or "GL" etc.
    sale_type_hint: str = ""
    keywords_extra: str = ""  # pipe-separated regex tokens


def load_brands_csv(path: Path) -> List[Brand]:
    if not path.exists():
        raise FileNotFoundError(f"brands.csv not found: {path}")

    brands: List[Brand] = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = (row.get("name") or "").strip()
            url = (row.get("url") or "").strip()
            if not name or not url:
                continue
            brands.append(
                Brand(
                    name=name,
                    url=url,
                    country=(row.get("country") or "KR").strip() or "KR",
                    sale_type_hint=(row.get("sale_type_hint") or "").strip(),
                    keywords_extra=(row.get("keywords_extra") or "").strip(),
                )
            )
    return brands


def fetch_text(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    html = r.text
    soup = BeautifulSoup(html, "html.parser")

    # remove scripts/styles
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    text = soup.get_text(" ", strip=True)
    return text


def detect_sale(text: str, keywords: List[str]) -> Tuple[bool, Optional[str]]:
    # return (is_sale, matched_keyword)
    for kw in keywords:
        if not kw:
            continue
        if kw in text or kw.lower() in text.lower():
            return True, kw
    return False, None


def extract_up_to_discount(text: str) -> Optional[int]:
    nums: List[int] = []

    for pat in UPTO_PATTERNS:
        for m in re.finditer(pat, text, flags=re.IGNORECASE):
            try:
                nums.append(int(m.group(1)))
            except Exception:
                pass

    if nums:
        v = max(nums)
        if 5 <= v <= 95:
            return v

    # fallback: any "NN%" occurrences (10/20/30/40/50 tabs etc.)
    hits: List[int] = []
    for m in re.finditer(r"(?<!\d)(\d{1,2})\s*%", text):
        try:
            v = int(m.group(1))
            if 5 <= v <= 95:
                hits.append(v)
        except Exception:
            pass

    return max(hits) if hits else None


def infer_members_only(text: str) -> bool:
    return any(k in text.lower() for k in ["members only", "회원", "회원공개", "로그인", "login"])


def main() -> None:
    brands = load_brands_csv(ROOT / "brands.csv")
    now = datetime.now(timezone.utc).isoformat()

    out: List[Dict[str, Any]] = []

    for b in brands:
        # keywords = default + extra (split by |)
        extra = [x.strip() for x in b.keywords_extra.split("|") if x.strip()] if b.keywords_extra else []
        keywords = list(dict.fromkeys(DEFAULT_KEYWORDS + extra))  # unique preserve order

        try:
            text = fetch_text(b.url)
            is_sale, matched = detect_sale(text, keywords)
            members = infer_members_only(text)
            max_disc = extract_up_to_discount(text)
        except Exception as e:
            out.append(
                {
                    "brand": b.name,
                    "url": b.url,
                    "country": b.country,
                    "status": "error",
                    "error": str(e),
                    "sale_type": b.sale_type_hint or None,
                    "matched_keyword": None,
                    "members_only": False,
                    "max_discount_hint": None,
                    "checked_at": now,
                }
            )
            continue

        out.append(
            {
                "brand": b.name,
                "url": b.url,
                "country": b.country,
                "status": "sale" if is_sale else "no_sale",
                "sale_type": b.sale_type_hint or None,
                "matched_keyword": matched,
                "members_only": bool(members),
                "max_discount_hint": max_disc,
                "checked_at": now,
            }
        )

    with OUT_JSON.open("w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print("✅ Done. outputs/sales.json 생성됨")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
