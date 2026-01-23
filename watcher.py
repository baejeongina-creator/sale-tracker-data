#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import csv
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Tuple, Dict, Any
from urllib.parse import urljoin

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(BASE_DIR, "brands.csv")
OUT_DIR = os.path.join(BASE_DIR, "outputs")
OUT_JSON = os.path.join(OUT_DIR, "sales.json")

DEFAULT_KEYWORDS = ["SALE", "세일", "할인", "OFF", "%", "UP TO", "EVENT", "프로모션", "특가"]

# ⚠️ 너무 공격적으로 true 되는 원인( LOGIN/SIGN IN ) 제거함
MEMBERS_ONLY_KEYWORDS = [
    "MEMBERS ONLY",
    "MEMBER ONLY",
    "회원전용",
    "회원 전용",
    "회원공개",
    "로그인 후",
    "로그인후",
]

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


@dataclass
class Brand:
    name: str
    country: str
    url: str
    sale_type_hint: Optional[str]
    keywords_extra: List[str]
    image: Optional[str] = None          # CSV에서 수동으로 박아넣는 이미지(최우선)
    image_page: Optional[str] = None     # "컨셉샷 뽑을 페이지" (보통 홈) - 없으면 url 사용


def fetch_html(url: str, timeout: int = 20) -> str:
    try:
        import requests  # type: ignore

        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
        r.raise_for_status()
        r.encoding = r.apparent_encoding or "utf-8"
        return r.text
    except Exception:
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


def normalize_text(html: str) -> str:
    t = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.I)
    t = re.sub(r"<style[\s\S]*?</style>", " ", t, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


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

    nums += [int(x) for x in re.findall(r"UP\s*TO\s*(\d{1,3})\s*%?", upper) if x.isdigit()]

    for _, n in re.findall(r"(최대|MAX)\s*(\d{1,3})\s*%?", upper):
        if n.isdigit():
            nums.append(int(n))

    for a, b in re.findall(r"(\d{1,3})\s*-\s*(\d{1,3})\s*%", upper):
        if a.isdigit():
            nums.append(int(a))
        if b.isdigit():
            nums.append(int(b))

    for n in re.findall(r"(\d{1,3})\s*%", upper):
        if n.isdigit():
            nums.append(int(n))

    nums = [n for n in nums if 1 <= n <= 95]
    return max(nums) if nums else None


def resolve_url(base: str, maybe: str) -> str:
    return urljoin(base, maybe)


def looks_like_logo(url: str) -> bool:
    u = (url or "").lower()
    bad = ["logo", "icon", "sprite", "favicon", "blank", "loading", "common", "gnb", "footer"]
    return any(x in u for x in bad)


def extract_auto_image(html: str, page_url: str) -> Optional[str]:
    # 1) og:image 우선
    m = re.search(
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        html,
        flags=re.I,
    )
    if m:
        u = resolve_url(page_url, m.group(1).strip())
        if u and not looks_like_logo(u):
            return u

    # 2) twitter:image
    m = re.search(
        r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
        html,
        flags=re.I,
    )
    if m:
        u = resolve_url(page_url, m.group(1).strip())
        if u and not looks_like_logo(u):
            return u

    # 3) fallback: img 중에 로고/아이콘 같은거 제외하고 첫번째 "사진스러운" 것
    imgs = re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', html, flags=re.I)
    for src in imgs[:120]:
        s = (src or "").strip()
        if not s:
            continue
        low = s.lower()
        if looks_like_logo(low):
            continue
        if not any(ext in low for ext in [".jpg", ".jpeg", ".png", ".webp"]):
            continue
        return resolve_url(page_url, s)

    return None


def load_brands_from_csv(path: str) -> List[Brand]:
    brands: List[Brand] = []
    if not os.path.exists(path):
        return brands

    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames or []

        # ✅ 필수 컬럼
        required = ["name", "country", "url", "sale_type_hint", "keywords_extra"]
        for rname in required:
            if rname not in fields:
                raise ValueError(f"brands.csv 헤더에 '{rname}' 컬럼이 필요해. 현재: {fields}")

        # ✅ 선택 컬럼(없어도 됨)
        has_image = "image" in fields
        has_image_page = "image_page" in fields

        for row in reader:
            name = (row.get("name") or "").strip()
            url = (row.get("url") or "").strip()
            if not name or not url:
                continue

            country = (row.get("country") or "").strip() or "KR"
            sale_type_hint = (row.get("sale_type_hint") or "").strip() or None
            kraw = (row.get("keywords_extra") or "").strip()
            extra = [x.strip() for x in kraw.split("|") if x.strip()] if kraw else []

            image = ((row.get("image") or "").strip() if has_image else "") or None
            image_page = ((row.get("image_page") or "").strip() if has_image_page else "") or None

            brands.append(
                Brand(
                    name=name,
                    country=country,
                    url=url,
                    sale_type_hint=sale_type_hint,
                    keywords_extra=extra,
                    image=image,
                    image_page=image_page,
                )
            )

    return brands


def load_brands() -> List[Brand]:
    brands = load_brands_from_csv(CSV_PATH)
    if brands:
        print(f"✅ Loaded {len(brands)} brands from brands.csv")
        return brands
    raise RuntimeError("brands.csv가 없거나 비어있음. (CSV 모드)")



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

            # ✅ 이미지: (1) CSV image가 있으면 그게 최우선
            #           (2) 없으면 image_page(없으면 url)에서 og:image/사진 자동 추출
            image_final = b.image
            if not image_final:
                img_page = b.image_page or b.url
                try:
                    img_html = fetch_html(img_page)
                    image_final = extract_auto_image(img_html, img_page)
                except Exception:
                    image_final = None

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
