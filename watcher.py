#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import csv
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Tuple, Dict, Any

# -----------------------------
# Paths
# -----------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(BASE_DIR, "brands.csv")
YAML_PATH = os.path.join(BASE_DIR, "config.yaml")
OUT_DIR = os.path.join(BASE_DIR, "outputs")
OUT_JSON = os.path.join(OUT_DIR, "sales.json")

# -----------------------------
# Defaults
# -----------------------------
DEFAULT_KEYWORDS = ["SALE", "세일", "할인", "OFF", "%", "UP TO", "EVENT", "프로모션", "특가"]

# "회원 전용" 감지용 키워드 (오탐 줄이려고 강한 신호만)
MEMBERS_ONLY_KEYWORDS = [
    "MEMBERS ONLY",
    "MEMBER ONLY",
    "회원전용",
    "회원 전용",
    "회원공개",
    "LOGIN",
    "SIGN IN",
]

# sale_type 자동 추론용 키워드
SALE_TYPE_RULES = [
    ("clearance", ["CLEARANCE", "클리어런스", "FINAL SALE", "OUTLET", "아울렛"]),
    ("refurb", ["REFURB", "리퍼브", "B-GRADE", "B GRADE", "B급", "리퍼"]),
    ("season_off", ["SEASON OFF", "SEASON-OFF", "SEASONOFF", "시즌오프", "시즌 오프"]),
    ("members_only", ["MEMBERS ONLY", "회원전용", "회원 전용", "회원공개"]),
]

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# -----------------------------
# Data model
# -----------------------------
@dataclass
class Brand:
    name: str
    country: str
    url: str
    sale_type_hint: Optional[str]
    keywords_extra: List[str]
    image: Optional[str]  # ✅ CSV 수동 이미지 (없으면 자동 추출)


# -----------------------------
# Fetch helpers (requests -> urllib fallback)
# -----------------------------
def fetch_html(url: str, timeout: int = 20) -> str:
    try:
        import requests  # type: ignore

        headers = {"User-Agent": USER_AGENT}
        r = requests.get(url, headers=headers, timeout=timeout)
        r.raise_for_status()
        r.encoding = r.apparent_encoding or "utf-8"
        return r.text
    except Exception:
        # urllib fallback
        import urllib.request

        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()

        for enc in ("utf-8", "euc-kr", "cp949", "latin-1"):
            try:
                return data.decode(enc)
            except Exception:
                pass
        return data.decode("utf-8", errors="ignore")


# -----------------------------
# Parsing / detection
# -----------------------------
def normalize_text(html: str) -> str:
    text = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def detect_sale(text: str, keywords: List[str]) -> Tuple[bool, Optional[str]]:
    upper = text.upper()
    for kw in keywords:
        if not kw:
            continue
        if re.search(r"[A-Z]", kw):
            if kw.upper() in upper:
                return True, kw
        else:
            if kw in text:
                return True, kw
    return False, None


def detect_members_only(text: str) -> bool:
    upper = text.upper()
    for kw in MEMBERS_ONLY_KEYWORDS:
        if re.search(r"[A-Z]", kw):
            if kw.upper() in upper:
                return True
        else:
            if kw in text:
                return True
    return False


def infer_sale_type(text: str, hint: Optional[str]) -> Optional[str]:
    if hint:
        return hint
    upper = text.upper()
    for sale_type, kws in SALE_TYPE_RULES:
        for kw in kws:
            if re.search(r"[A-Z]", kw):
                if kw.upper() in upper:
                    return sale_type
            else:
                if kw in text:
                    return sale_type
    return None


def extract_max_discount(text: str) -> Optional[int]:
    upper = text.upper()

    nums: List[int] = []

    m = re.findall(r"UP\s*TO\s*(\d{1,3})\s*%?", upper)
    nums += [int(x) for x in m if x.isdigit()]

    m2 = re.findall(r"(최대|MAX)\s*(\d{1,3})\s*%?", upper)
    for _, n in m2:
        if n.isdigit():
            nums.append(int(n))

    m3 = re.findall(r"(\d{1,3})\s*-\s*(\d{1,3})\s*%", upper)
    for a, b in m3:
        if a.isdigit():
            nums.append(int(a))
        if b.isdigit():
            nums.append(int(b))

    m4 = re.findall(r"(\d{1,3})\s*%", upper)
    for n in m4:
        if n.isdigit():
            nums.append(int(n))

    nums = [n for n in nums if 1 <= n <= 95]
    return max(nums) if nums else None


# -----------------------------
# Auto image extraction (og:image etc.)
# -----------------------------
def resolve_url(base: str, maybe: str) -> str:
    try:
        from urllib.parse import urljoin
        return urljoin(base, maybe)
    except Exception:
        return maybe


def extract_auto_image(html: str, page_url: str) -> Optional[str]:
    # og:image
    m = re.search(
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        html,
        flags=re.I,
    )
    if m:
        return resolve_url(page_url, m.group(1).strip())

    # twitter:image
    m = re.search(
        r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
        html,
        flags=re.I,
    )
    if m:
        return resolve_url(page_url, m.group(1).strip())

    # fallback: 첫 번째 일반 이미지
    imgs = re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', html, flags=re.I)
    for src in imgs[:60]:
        s = src.lower()
        if any(x in s for x in ["logo", "icon", "sprite", "blank", "loading", "common"]):
            continue
        if not any(ext in s for ext in [".jpg", ".jpeg", ".png", ".webp"]):
            continue
        return resolve_url(page_url, src.strip())

    return None


# -----------------------------
# Load brands from CSV / YAML
# -----------------------------
def load_brands_from_csv(path: str) -> List[Brand]:
    brands: List[Brand] = []
    if not os.path.exists(path):
        return brands

    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)

        # ✅ image 컬럼까지 포함 (비어있어도 OK)
        required = ["name", "country", "url", "sale_type_hint", "keywords_extra", "image"]
        for rname in required:
            if rname not in (reader.fieldnames or []):
                raise ValueError(
                    f"brands.csv 헤더에 '{rname}' 컬럼이 필요해. 현재: {reader.fieldnames}"
                )

        for row in reader:
            name = (row.get("name") or "").strip()
            country = (row.get("country") or "").strip() or "KR"
            url = (row.get("url") or "").strip()
            sale_type_hint = (row.get("sale_type_hint") or "").strip() or None
            keywords_extra_raw = (row.get("keywords_extra") or "").strip()
            image = (row.get("image") or "").strip() or None

            if not name:
                continue
            if not url:
                continue

            extra: List[str] = []
            if keywords_extra_raw:
                extra = [x.strip() for x in keywords_extra_raw.split("|") if x.strip()]

            brands.append(
                Brand(
                    name=name,
                    country=country,
                    url=url,
                    sale_type_hint=sale_type_hint,
                    keywords_extra=extra,
                    image=image,
                )
            )

    return brands


def load_brands_from_yaml(path: str) -> List[Brand]:
    brands: List[Brand] = []
    if not os.path.exists(path):
        return brands

    try:
        import yaml  # type: ignore
    except Exception:
        print("⚠️ config.yaml 읽으려면 PyYAML이 필요해. (pip install pyyaml)")
        return brands

    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    raw = cfg.get("brands") or []
    for b in raw:
        name = (b.get("name") or "").strip()
        url = (b.get("url") or "").strip()
        country = (b.get("country") or "KR").strip()
        sale_type_hint = (b.get("sale_type_hint") or None)

        keywords_extra: List[str] = []
        signals = b.get("signals") or []
        if signals and isinstance(signals, list):
            first = signals[0] if signals else {}
            any_list = first.get("any") if isinstance(first, dict) else None
            if isinstance(any_list, list):
                keywords_extra = [str(x).strip() for x in any_list if str(x).strip()]

        if name and url:
            brands.append(
                Brand(
                    name=name,
                    country=country,
                    url=url,
                    sale_type_hint=sale_type_hint,
                    keywords_extra=keywords_extra,
                    image=None,  # yaml엔 이미지 없음(원하면 나중에 추가 가능)
                )
            )

    return brands


def load_brands() -> List[Brand]:
    brands = load_brands_from_csv(CSV_PATH)
    if brands:
        print(f"✅ Loaded {len(brands)} brands from brands.csv")
        return brands

    brands = load_brands_from_yaml(YAML_PATH)
    if brands:
        print(f"✅ Loaded {len(brands)} brands from config.yaml")
        return brands

    raise RuntimeError("brands.csv도 없고 config.yaml도 없음. 둘 중 하나는 있어야 해.")


# -----------------------------
# Main
# -----------------------------
def main() -> None:
    brands = load_brands()

    results: List[Dict[str, Any]] = []
    checked_at = datetime.now(timezone.utc).isoformat()

    for b in brands:
        print(f"CHECKING: {b.name}")
        try:
            html = fetch_html(b.url)
            text = normalize_text(html)

            keywords = DEFAULT_KEYWORDS + (b.keywords_extra or [])
            is_sale, matched = detect_sale(text, keywords)

            members_only = detect_members_only(text)

            sale_type = infer_sale_type(text, b.sale_type_hint)
            if members_only and not sale_type:
                sale_type = "members_only"

            max_discount = extract_max_discount(text)

            # ✅ 이미지: CSV 수동 우선, 없으면 자동 추출
            auto_image = extract_auto_image(html, b.url)
            image_final = b.image or auto_image

            results.append(
                {
                    "brand": b.name,
                    "url": b.url,
                    "country": b.country,
                    "status": "sale" if is_sale else "no_sale",
                    "sale_type": sale_type,
                    "matched_keyword": matched,
                    "members_only": bool(members_only),
                    "max_discount_hint": max_discount,
                    "checked_at": checked_at,
                    "image": image_final,
                }
            )

        except Exception as e:
            results.append(
                {
                    "brand": b.name,
                    "url": b.url,
                    "country": b.country,
                    "status": "error",
                    "error": str(e),
                    "sale_type": b.sale_type_hint,
                    "matched_keyword": None,
                    "members_only": False,
                    "max_discount_hint": None,
                    "checked_at": checked_at,
                    "image": None,
                }
            )

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print(f"✅ Done. {OUT_JSON} 생성됨")


if __name__ == "__main__":
    main()
