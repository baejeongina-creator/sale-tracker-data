import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Tuple, Optional, Dict, Any

import requests
import yaml
from bs4 import BeautifulSoup

OUT_DIR = Path("outputs")
OUT_DIR.mkdir(exist_ok=True)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

COMMON_SALE_KEYWORDS = [
    "SALE", "SEASON OFF", "OFF", "CLEARANCE", "OUTLET", "DISCOUNT",
    "PROMOTION", "EVENT", "UP TO", "FINAL SALE",
    "세일", "시즌오프", "클리어런스", "아울렛", "할인", "프로모션", "이벤트",
    "최대", "특가", "타임세일", "쿠폰", "기획전",
    "%", "원",
]

SALE_TYPE_RULES = [
    ("refurb", ["REFURB", "리퍼브", "B-GRADE", "B GRADE", "B급", "리퍼", "REWORK", "RE:"]),
    ("clearance", ["CLEARANCE", "클리어런스", "OUTLET", "아울렛", "FINAL SALE", "파이널"]),
    ("season_off", ["SEASON OFF", "시즌오프"]),
    ("members_only", ["MEMBERS ONLY", "회원전용", "회원 전용", "회원공개", "회원 공개", "로그인 후", "로그인"]),
    ("sale", ["SALE", "세일", "할인"]),
]

MEMBERS_PATTERNS = [
    r"MEMBERS\s*ONLY",
    r"회원\s*전용",
    r"회원\s*공개",
    r"회원만",
    r"로그인\s*후",
]

UPTO_PATTERNS = [
    r"UP\s*TO\s*(\d{1,2})\s*%",
    r"최대\s*(\d{1,2})\s*%",
    r"(\d{1,2})\s*%\s*OFF",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_brands() -> List[Dict[str, Any]]:
    with open("config.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    brands = cfg.get("brands", [])
    if not isinstance(brands, list):
        raise ValueError("config.yaml 형식 오류: brands는 리스트여야 함")
    return brands


def fetch_html(url: str) -> str:
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=25)
    r.raise_for_status()
    return r.text


def extract_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator=" ", strip=True)
    return re.sub(r"\s+", " ", text)


def detect_sale(text: str, keywords: List[str]) -> Tuple[bool, Optional[str]]:
    upper = text.upper()
    for k in keywords:
        if str(k).upper() in upper:
            return True, str(k)
    return False, None


def detect_members_only(text: str) -> bool:
    for pat in MEMBERS_PATTERNS:
        if re.search(pat, text, flags=re.IGNORECASE):
            return True
    return False


def detect_sale_type(text: str) -> Optional[str]:
    upper = text.upper()
    for sale_type, keys in SALE_TYPE_RULES:
        for k in keys:
            if k.upper() in upper:
                return sale_type
    return None


def extract_up_to_discount(text: str) -> Optional[int]:
    nums = []

    # 1) 확실한 패턴 우선
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

    # 2) fallback: 그냥 '숫자%' 전부 긁기 (10%~50% 탭 대응)
    percent_hits = []
    for m in re.finditer(r"(?<!\d)(\d{1,2})\s*%", text):
        try:
            v = int(m.group(1))
            if 5 <= v <= 95:
                percent_hits.append(v)
        except Exception:
            pass

    return max(percent_hits) if percent_hits else None



def keywords_from_brand(b: Dict[str, Any]) -> List[str]:
    """
    구형 config 지원:
      signals:
        - type: "keyword"
          any: [...]
    신형 config 지원:
      extra_keywords: [...]
    공통 키워드는 항상 포함
    """
    kw = list(COMMON_SALE_KEYWORDS)

    # 구형: signals
    if isinstance(b.get("signals"), list) and b.get("signals"):
        first = b["signals"][0]
        any_list = first.get("any") if isinstance(first, dict) else None
        if isinstance(any_list, list):
            kw.extend([str(x) for x in any_list])

    # 신형: extra_keywords
    extra = b.get("extra_keywords", [])
    if isinstance(extra, list):
        kw.extend([str(x) for x in extra])

    # 중복 제거(순서 유지)
    seen = set()
    uniq = []
    for x in kw:
        if x not in seen:
            uniq.append(x)
            seen.add(x)
    return uniq


def main() -> None:
    brands = load_brands()
    results = []

    for b in brands:
        name = b.get("name")
        url = b.get("url")

        if not name or not url:
            results.append({
                "brand": name or "(missing name)",
                "url": url or "(missing url)",
                "status": "error",
                "sale_type": None,
                "matched_keyword": None,
                "members_only": None,
                "max_discount_hint": None,
                "checked_at": now_iso(),
                "error": "config missing name or url",
            })
            continue

        print("CHECKING:", name)

        try:
            html = fetch_html(url)
            text = extract_text(html)

            kw = keywords_from_brand(b)
            is_sale, matched_kw = detect_sale(text, kw)
            status = "sale" if is_sale else "no_sale"

            members_only = detect_members_only(text)
            auto_type = detect_sale_type(text)

            hint = b.get("sale_type_hint")
            if status == "sale" and hint:
                sale_type = hint
            else:
                sale_type = auto_type if status == "sale" else None

            max_discount_hint = extract_up_to_discount(text)

            results.append({
                "brand": name,
                "country": "KR",
                "url": url,
                "status": status,
                "sale_type": sale_type,
                "matched_keyword": matched_kw,
                "members_only": bool(members_only),
                "max_discount_hint": max_discount_hint,
                "checked_at": now_iso(),
            })

            time.sleep(1)

        except Exception as e:
            print("[ERROR] {}: {}".format(name, e))
            results.append({
                "brand": name,
                "country": "KR",
                "url": url,
                "status": "error",
                "sale_type": None,
                "matched_keyword": None,
                "members_only": None,
                "max_discount_hint": None,
                "checked_at": now_iso(),
                "error": str(e),
            })

    (OUT_DIR / "sales.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("✅ Done. outputs/sales.json 생성됨")


if __name__ == "__main__":
    main()

