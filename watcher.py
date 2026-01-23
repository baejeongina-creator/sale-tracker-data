#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import csv
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

try:
    import yaml  # pip install pyyaml
except Exception:
    yaml = None

ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "outputs"
OUTPUT_DIR.mkdir(exist_ok=True)
OUT_JSON = OUTPUT_DIR / "sales.json"

TIMEOUT = 25
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Safari/537.36"
}

DEFAULT_KEYWORDS = [
    "SALE", "세일", "SEASON", "SEASON OFF", "OFF", "할인", "%", "원",
    "CLEARANCE", "클리어런스", "OUTLET", "아울렛",
    "REFURB", "리퍼브", "B-GRADE", "B GRADE", "B급",
    "UP TO", "최대", "EVENT", "회원", "회원전용", "회원공개", "MEMBERS ONLY"
]

UPTO_PATTERNS = [
    r"UP\s*TO\s*(\d{1,2})\s*%",
    r"최대\s*(\d{1,2})\s*%",
    r"(\d{1,2})\s*%\s*OFF",
]

MEMBERS_ONLY_PATTERNS = [
    r"members\s*only",
    r"member\s*only",
    r"회원\s*전용",
    r"회원\s*공개",
    r"회원가",
    r"회원\s*한정",
    r"로그인\s*후\s*공개",
    r"로그인\s*후\s*확인",
]



def fetch_text(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return soup.get_text(" ", strip=True)


def detect_sale(text: str, keywords: List[str]) -> Tuple[bool, Optional[str]]:
    low = text.lower()
    for kw in keywords:
        if not kw:
            continue
        if kw.lower() in low:
            return True, kw
    return False, None


def extract_up_to_discount(text: str) -> Optional[int]:
    nums: List[int] = []

    # strong patterns
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

    # fallback: any NN%
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
    low = text.lower()
    for pat in MEMBERS_ONLY_PATTERNS:
        if re.search(pat, low, flags=re.IGNORECASE):
            return True
    return False



def load_from_config_yaml(path: Path) -> Optional[List[Dict[str, Any]]]:
    """
    Reads your YAML like:
    brands:
      - name: ...
        url: ...
        signals:
          - type: keyword
            any: [...]
        sale_type_hint: ...
    """
    if not path.exists() or yaml is None:
        return None

    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    brands = cfg.get("brands")
    if not isinstance(brands, list) or not brands:
        return None

    normalized: List[Dict[str, Any]] = []
    for b in brands:
        if not isinstance(b, dict):
            continue
        name = (b.get("name") or "").strip()
        url = (b.get("url") or "").strip()
        if not name or not url:
            continue

        # gather keywords from signals
        keywords: List[str] = []
        signals = b.get("signals") or []
        if isinstance(signals, list):
            for s in signals:
                if not isinstance(s, dict):
                    continue
                if (s.get("type") or "").lower() == "keyword":
                    any_list = s.get("any") or []
                    if isinstance(any_list, list):
                        keywords.extend([str(x) for x in any_list if str(x).strip()])

        if not keywords:
            keywords = DEFAULT_KEYWORDS[:]  # fallback

        normalized.append({
            "name": name,
            "url": url,
            "country": (b.get("country") or "KR"),
            "sale_type_hint": (b.get("sale_type_hint") or ""),
            "keywords": keywords
        })
    return normalized


def load_from_brands_csv(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    out: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = (row.get("name") or "").strip()
            url = (row.get("url") or "").strip()
            if not name or not url:
                continue
            country = (row.get("country") or "KR").strip() or "KR"
            sale_type_hint = (row.get("sale_type_hint") or "").strip()
            extra = (row.get("keywords_extra") or "").strip()

            keywords = DEFAULT_KEYWORDS[:]
            if extra:
                keywords.extend([x.strip() for x in extra.split("|") if x.strip()])

            out.append({
                "name": name,
                "url": url,
                "country": country,
                "sale_type_hint": sale_type_hint,
                "keywords": keywords
            })
    return out


def main() -> None:
    now = datetime.now(timezone.utc).isoformat()

    # ✅ config.yaml 우선 (네가 붙여넣은 거 먹게)
    brands = load_from_config_yaml(ROOT / "config.yaml")
    if brands is None:
        # 없으면 csv fallback
        brands = load_from_brands_csv(ROOT / "brands.csv")

    if not brands:
        raise RuntimeError("No brands found. Add brands to config.yaml (brands:) or brands.csv")

    results: List[Dict[str, Any]] = []

    for b in brands:
        name = b["name"]
        url = b["url"]
        country = (b.get("country") or "KR").strip() or "KR"
        sale_type_hint = b.get("sale_type_hint") or None
        keywords = b.get("keywords") or DEFAULT_KEYWORDS

        try:
            text = fetch_text(url)
            is_sale, matched = detect_sale(text, keywords)
            members = infer_members_only(text)
            max_disc = extract_up_to_discount(text)
        except Exception as e:
            results.append({
                "brand": name,
                "url": url,
                "country": country,
                "status": "error",
                "error": str(e),
                "sale_type": sale_type_hint,
                "matched_keyword": None,
                "members_only": False,
                "max_discount_hint": None,
                "checked_at": now,
            })
            continue

        results.append({
            "brand": name,
            "url": url,
            "country": country,
            "status": "sale" if is_sale else "no_sale",
            "sale_type": sale_type_hint,
            "matched_keyword": matched,
            "members_only": bool(members),
            "max_discount_hint": max_disc,
            "checked_at": now,
        })

    OUT_JSON.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print("✅ Done. outputs/sales.json 생성됨")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
