"""상품 축 태그 · 비선호 대조 어휘 — 태그 글자와 상품을 **같은 대표 이름**으로 맞춘다(DR-043 다음 단계, 10-16).

글자 그대로 대조하면(10-16 #13) 세 가지가 샜다.
  - 다른 말: '캠핑 장비' ↔ 종류 '기타캠핑용품', 'SONY' ↔ 브랜드 '소니', '카카오프렌즈' ↔ 상품명 '춘식이'
  - 넓게 걸림: '레고' → 다른 회사 '레고트', '음향기기' → 소분류 '영상·음향기기' 의 TV, '콘' → '실리콘'
  - 꾸밈말: '한우 세트' · '과일 선물세트' 는 어떤 종류 이름에도 그대로 들어 있지 않다
그래서 축마다 작은 어휘(`vocab_data/`)로 **상품마다 축 태그(대표 이름)** 를 계산하고(`product_tags`), 사람 비선호 태그도 같은
대표 이름으로 바꿔(`canonicals`) 둘이 겹치는지 본다. 상품 태그는 카탈로그 적재 때 DB(`ai_catalog.products.tags`, 0011)에 넣어 두고,
판(`RULE` · `tags_sha256`)이 이 코드와 같을 때만 그것을 쓴다 — 다르거나 없으면 **같은 함수로** 그 자리에서 계산한다(결과가 같다).

  브랜드  — 브랜드 칸 → 대표 이름(같은 계열 · 괄호 꼬리 무시, brand.tsv) `via=field`. 브랜드 칸이 판매처인 상품(오디오(LuX) · 하이마트)을
           위해 상품명에 어휘 브랜드가 **낱말로** 나오면 그 브랜드도 `via=name` — 영문 표기 또는 한글 3글자 이상만, IP 이름 · '안 씀' 표시 ·
           '호환' 상품 · 'for Mac' · '… 증정' · 기기 이름(PS5 · XBOX — 다른 회사 상품에선 호환 표시)은 빼고, 이름만 이어진 다른 회사
           (레고 ↔ 레고트)는 아니다. **협업**: 상품명 'A x B' 의 상대가 카탈로그 브랜드면 그 브랜드도(마뗑킴 x 케이스티파이), 브랜드 칸이
           협업 이름이면 나눠서(반얀트리X하겐다즈 → 둘 다). 카탈로그 브랜드 목록은 카탈로그를 읽는 쪽이 `set_catalog_brands` 로 준다.
  IP     — IP · 캐릭터 · 별칭(ip.tsv). 상품명에 그 IP 의 이름이 나오면 IP 대표 이름, 캐릭터 이름(별칭 포함)이 나오면 캐릭터 대표 이름.
           짧거나 흔한 이름('~')은 낱말로 떨어져 있고 같은 IP 의 다른 이름이 상품명 · 브랜드에 함께 있을 때만.
  카테고리 — 묶음 말(category_groups.tsv): 묶음 정의(소분류 · 대분류 · 종류 정규식)에 들면 `via=field`, 상품명에 묶음 이름이 나오거나
           상품명 규칙(N:, 종류 칸이 '카페세트' 처럼 뭉뚱그려진 상품 — 10-17)에 맞으면 `via=name`.
           상품 종류 자체는 `product_kind` 열에 있으므로 태그로 넣지 않는다.
사람 태그가 대표 이름으로 안 바뀌면(어휘에 없음) 글자 대조(#13 규칙: 종류 · 소분류 · 상품명에 들어 있으면, '세트 · 장비' 꼬리 떼고)로 본다.
두 글자 미만은 대조하지 않는다(카테고리 묶음 말 '옷' · '술' · '차' 는 예외).
선호 태그의 검색어(`query_text`): 한 글자 태그는 묶음 이름 · 브랜드 대표 이름으로 풀어 쓰고, 못 풀면 검색어에서 뺀다(10-17).

입력은 상품 칸(브랜드 · 상품명 · 종류 · 소분류 · 대분류)뿐이고 DB · HTTP 를 쓰지 않는다.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import cache, lru_cache
from pathlib import Path
from typing import Protocol

DATA = Path(__file__).parent / "vocab_data"
RULE = "vocab-v3"                  # 상품 태그 계산 규칙의 판 — 계산 결과가 바뀌면 올린다(test_vocab 의 지문 시험이 알려 준다). v3(10-17): 상품명 규칙 N: · 차 묶음
MIN_LEN = 2                        # 이보다 짧은 태그 · 표기는 대조하지 않는다(selection.DISLIKED_MIN_LEN 과 같음)
LOOSE_LEN = 3                      # 이 길이 이상 한글 표기만 '들어 있으면' 대조. 짧으면 낱말로 떨어져 있어야
_NORM = re.compile(r"[\s·/_\-()\[\]'\".,&:!]+")
_PAREN = re.compile(r"\(.*?\)")
_ASCII = re.compile(r"^[\x00-\x7f]+$")
_SUFFIX = re.compile(r"(선물세트|세트|선물|장비)$")
## 협업 표시 'A x B' — x 앞뒤가 띄어 있거나 한글에 붙은 것만(MX · X5 · 85X85 · 50gX6 같은 모델명 · 크기는 아님)
_COLLAB = re.compile(r"([0-9A-Za-z가-힣&]+)(?:\s+[xX×]\s+|(?<=[가-힣])[xX×](?=[가-힣A-Za-z])|(?<=[A-Za-z가-힣])[xX×](?=[가-힣]))([0-9A-Za-z가-힣&]+)")
_COLLAB_BRAND = re.compile(r"\s*(?<![A-Za-z])[xX×](?![A-Za-z])\s*|\s+[xX×]\s+")
AXES = ("브랜드", "IP", "카테고리")


class Product(Protocol):
    brand: str
    name: str
    productKind: str | None
    categoryName: str
    parentCategoryName: str | None


Matcher = Callable[[Product], bool]


@lru_cache(maxsize=1 << 16)
def norm(x: str | None) -> str:
    """대소문자 · 띄어쓰기 · 가운뎃점 · 괄호 · 문장부호 무시."""
    return _NORM.sub("", (x or "").lower())


@lru_cache(maxsize=1 << 16)
def base(x: str | None) -> str:
    """괄호 꼬리를 뗀 정규형 — '레토(캠핑)' · '다이슨(일반)' → '레토' · '다이슨'."""
    return norm(_PAREN.sub("", x or "")) or norm(x)


@cache
def tags_sha256() -> str:
    """상품 태그의 판 지문 — 규칙 판 + 어휘 파일 내용. DB `catalog_versions.tags_sha256` 과 같으면 DB 의 상품 태그를 그대로 쓴다."""
    h = hashlib.sha256(RULE.encode())
    for f in sorted(DATA.glob("*.tsv")):
        h.update(f.name.encode())
        h.update(f.read_bytes())
    return h.hexdigest()


def _word(term: str) -> re.Pattern[str]:
    """낱말로 떨어져 있을 때만 — 앞뒤가 한글 · 영문 · 숫자가 아니다('콘' 은 '실리콘' 에 안 걸림)."""
    return re.compile(r"(?<![0-9a-z가-힣])" + re.escape(term.strip().lower()) + r"(?![0-9a-z가-힣])")


def _word_fn(term: str) -> Callable[[str, str], bool]:
    pat = _word(term)
    return lambda low, nn: bool(pat.search(low))


def _in_name(term: str) -> Callable[[str, str], bool]:
    """상품명 대조 함수(원문 소문자, 정규형) — 3글자 이상 한글은 들어 있으면, 영문 · 짧은 표기는 낱말일 때만."""
    n = norm(term)
    if len(n) < MIN_LEN and not _ASCII.match(term):
        n = ""
    if not n:
        return lambda low, nn: False
    if _ASCII.match(term) or len(n) < LOOSE_LEN:
        return _word_fn(term)
    return lambda low, nn: n in nn


def _split(s: str) -> list[str]:
    return [x.strip() for x in s.split(",") if x.strip()]


def _rows(name: str, width: int = 4) -> list[list[str]]:
    out = []
    for line in (DATA / name).read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            out.append((line.split("\t") + [""] * width)[:width])
    return out


# ---- 브랜드

@dataclass(frozen=True)
class BrandEntry:
    canon: str
    members: tuple[str, ...]
    aliases: tuple[str, ...]
    distinct: tuple[str, ...]
    mention: str                  # 상품명 언급: "" 모두 · "영문만" · "안 씀" · "뺌:A|B"(그 표기만 빼기)


@cache
def _brand_entries() -> tuple[BrandEntry, ...]:
    return tuple(BrandEntry(c, tuple(_split(m)) or (c,), tuple(_split(a)), tuple(_split(d)), x.strip())
                 for c, m, a, d, x in _rows("brand.tsv", 5))


@cache
def _brands() -> dict[str, list[BrandEntry]]:
    idx: dict[str, list[BrandEntry]] = {}
    for e in _brand_entries():
        for k in {norm(e.canon), base(e.canon), *(base(x) for x in e.members), *(norm(x) for x in e.aliases)}:
            if k:
                idx.setdefault(k, []).append(e)
    return idx


def brand_canonical(brand: str) -> str:
    """브랜드 칸 값 → 대표 이름. 어휘에 없으면 괄호 꼬리를 뗀 그 이름."""
    es = _brands().get(norm(brand)) or _brands().get(base(brand))
    return es[0].canon if es else (_PAREN.sub("", brand).strip() or brand.strip())


@cache
def _brand_mentions() -> tuple[tuple[BrandEntry, frozenset[str], tuple[tuple[str, re.Pattern[str]], ...]], ...]:
    """상품명 언급으로 볼 표기 — (브랜드, 다른 브랜드 정규형, [(표기, 낱말 패턴)])."""
    ip_names = _ip_name_keys()
    out = []
    for e in _brand_entries():
        if e.mention == "안 씀":
            continue
        surf = []
        skip = {norm(x) for x in e.mention[2:].split("|")} if e.mention.startswith("뺌:") else set()
        for s in dict.fromkeys((e.canon, *e.members, *e.aliases)):
            n, asc = base(s), bool(_ASCII.match(s))
            if norm(s) in skip:
                continue
            if n in ip_names or norm(s) in ip_names:
                continue                                     # IP 이름(헬로키티 · 산리오)은 IP 축이 맡는다
            if e.mention == "영문만" and not asc:
                continue
            if (asc and len(n) >= MIN_LEN) or (not asc and len(n) >= LOOSE_LEN):
                surf.append((_PAREN.sub("", s).strip() or s, _word(_PAREN.sub("", s).strip() or s)))
        if surf:
            other = frozenset({base(x) for x in e.distinct} | {norm(x) for x in e.distinct})
            out.append((e, other, tuple(surf)))
    return tuple(out)


def _mentions_brand(low: str, surface: str, pat: re.Pattern[str]) -> bool:
    for m in pat.finditer(low):
        before, after = low[max(0, m.start() - 5):m.start()], low[m.end():m.end() + 20]
        if re.search(r"for\s*$", before) or "증정" in after:
            continue                                         # 'for Mac' 은 호환 표시, '스타벅스 … 증정' 은 사은품
        return True
    return False


def _brand_fallback(text: str) -> Matcher:
    """어휘에 없는 브랜드 태그 — 브랜드 칸이 같거나(괄호 꼬리 무시) 3글자 이상이 들어 있거나, 상품명에 낱말로 나오면."""
    t, b = norm(text), base(text)
    loose = t if (len(t) >= LOOSE_LEN and not _ASCII.match(text)) or (_ASCII.match(text) and len(t) >= 4) else ""
    word = _word_fn(text)

    def hit(p: Product) -> bool:
        pb, pn = base(p.brand), norm(p.brand)
        return pb in (t, b) or pn in (t, b) or bool(loose and loose in pn) or word((p.name or "").lower(), "")
    return hit


# 카탈로그 브랜드 목록(협업 상대 확인용) — 정규형 → 대표 이름. 카탈로그를 읽는 쪽(DbCatalogReader · 적재 도구)이 넣는다
_CATALOG_BRANDS: dict[str, str] = {}
_CATALOG_KEY = [0]


def set_catalog_brands(brands) -> None:
    """협업 상대('마뗑킴 x 케이스티파이')를 브랜드로 볼지 판단할 카탈로그 브랜드 목록. 바뀌면 상품 태그 계산 기억을 비운다."""
    new = {}
    for b in brands:
        for part in _brand_parts(b or ""):
            if len(base(part)) >= MIN_LEN:
                new[base(part)] = brand_canonical(part)
    if new != _CATALOG_BRANDS:
        _CATALOG_BRANDS.clear()
        _CATALOG_BRANDS.update(new)
        _CATALOG_KEY[0] += 1


def _brand_parts(brand: str) -> list[str]:
    """브랜드 칸 → 브랜드들. 협업 이름('반얀트리X하겐다즈' · '콘래드 서울 X 하겐다즈')은 나눈다."""
    parts = [x.strip() for x in _COLLAB_BRAND.split(brand) if x.strip()]
    return parts if len(parts) > 1 and all(re.search(r"[A-Za-z가-힣]{2}", x) for x in parts) else [brand.strip()]


# ---- IP

@dataclass(frozen=True)
class IpName:
    ip: str
    text: str
    needs_context: bool


@cache
def _ip_table() -> tuple[tuple[str, str, tuple[IpName, ...]], ...]:
    """ip.tsv 행 — (IP, 캐릭터 대표 이름, 그 캐릭터의 이름들(별칭 포함))."""
    out = []
    for ip, ch, al, _ in _rows("ip.tsv"):
        names = tuple(IpName(ip, x.strip().lstrip("~"), x.strip().startswith("~")) for x in [ch, *_split(al)] if x.strip())
        out.append((ip, ch.strip().lstrip("~"), names))
    return tuple(out)


def _ip_heads(ip: str) -> list[str]:
    """IP 이름의 표기 — '치이카와(먼작귀)' → 치이카와 · 먼작귀."""
    return [x for x in dict.fromkeys([_PAREN.sub("", ip).strip(), *re.findall(r"\((.*?)\)", ip)]) if x]


@cache
def _ips() -> tuple[dict[str, list[IpName]], dict[str, str], dict[str, set[str]]]:
    """(IP 정규형 → 그 IP 의 모든 이름, IP 표기 정규형 → IP 대표 이름, 캐릭터 이름 · 별칭 정규형 → 캐릭터 대표 이름들)."""
    by_ip: dict[str, list[IpName]] = {}
    ip_key: dict[str, str] = {}
    char_key: dict[str, set[str]] = {}
    for ip, ch, names in _ip_table():
        own = [IpName(ip, h, False) for h in _ip_heads(ip)]
        for k in {norm(ip), *(norm(h) for h in _ip_heads(ip))}:
            ip_key[k] = ip
            lst = by_ip.setdefault(k, list(own))
            lst.extend(names)
        for n in names:
            char_key.setdefault(norm(n.text), set()).add(ch)
    return by_ip, ip_key, char_key


@cache
def _ip_name_keys() -> frozenset[str]:
    _, ip_key, char_key = _ips()
    return frozenset(ip_key) | frozenset(char_key)


def _ip_hit(targets: list[IpName] | tuple[IpName, ...]) -> Matcher:
    by_ip, _, _ = _ips()
    ctx: dict[str, list[Callable[[str, str], bool]]] = {}
    for ip in {t.ip for t in targets if t.ip}:
        ctx[ip] = [_in_name(n.text) for n in by_ip[norm(ip)] if not n.needs_context]
    plain = [_in_name(t.text) for t in targets if not t.needs_context]
    loose = [(_word_fn(t.text), ctx.get(t.ip, [])) for t in targets if t.needs_context and t.ip]

    def hit(p: Product) -> bool:
        low, nn = (p.name or "").lower(), norm(p.name)
        if any(f(low, nn) for f in plain):
            return True
        if not loose:
            return False
        blow, bn = f"{low} {(p.brand or '').lower()}", nn + norm(p.brand)
        return any(f(low, nn) and any(c(blow, bn) for c in cs) for f, cs in loose)
    return hit


@cache
def _ip_taggers() -> tuple[tuple[str, Matcher], ...]:
    """상품 태그용 — (IP 또는 캐릭터 대표 이름, 대조 함수)."""
    by_ip, _, _ = _ips()
    out: list[tuple[str, Matcher]] = []
    for ip in dict.fromkeys(ip for ip, _, _ in _ip_table()):
        out.append((ip, _ip_hit(by_ip[norm(ip)])))
    for _, ch, names in _ip_table():
        out.append((ch, _ip_hit(names)))
    return tuple(dict.fromkeys(out))


# ---- 카테고리

@dataclass(frozen=True)
class Group:
    name: str                    # 대표 이름('@' 뗀 것)
    sub: frozenset[str]          # 소분류 이름
    top: frozenset[str]          # 대분류 이름
    kind: re.Pattern[str] | None
    drop: re.Pattern[str] | None
    names: re.Pattern[str] | None = None        # N: 상품명 — 종류 칸이 뭉뚱그려진 상품('카페세트' 의 아메리카노)
    name_drop: re.Pattern[str] | None = None    # NX: 상품명에 있으면 N 으로는 묶지 않음('아메리카노 쿠폰 증정' 케이크)
    aliases: tuple[str, ...] = ()               # 대표 이름 + 별칭 — 한 글자 선호 태그를 검색어로 풀어 쓸 때(query_text)


def _pat(parts: list[str]) -> re.Pattern[str] | None:
    return re.compile("|".join(parts)) if parts else None


@cache
def _groups() -> dict[str, Group]:
    raw = {g: (a, d) for g, a, d, _ in _rows("category_groups.tsv")}

    def build(g: str, seen: frozenset[str] = frozenset()) -> Group:
        sub, top, kinds, drops, names, name_drops = set(), set(), [], [], [], []
        for part in raw[g][1].split():
            tag, _, val = part.partition(":")
            if part.startswith("@") and part not in seen:
                inner = build(part, seen | {g})
                sub |= inner.sub
                top |= inner.top
                for got, pat in ((kinds, inner.kind), (drops, inner.drop), (names, inner.names), (name_drops, inner.name_drop)):
                    got += [pat.pattern] if pat else []
            elif tag == "S":
                sub.add(norm(val))
            elif tag == "R":
                top.add(norm(val))
            elif tag == "K":
                kinds.append(val)
            elif tag == "X":
                drops.append(val)
            elif tag == "N":
                names.append(val)
            elif tag == "NX":
                name_drops.append(val)
        return Group(g[1:], frozenset(sub), frozenset(top), _pat(kinds), _pat(drops), _pat(names), _pat(name_drops))
    idx: dict[str, Group] = {}
    for g, (aliases, _) in raw.items():
        words = tuple(dict.fromkeys([g[1:], *_split(aliases)]))
        grp = replace(build(g), aliases=words)
        for a in words:
            idx.setdefault(norm(a), grp)
    return idx


def _in_group(g: Group, p: Product) -> bool:
    kind = p.productKind or ""
    if g.drop and g.drop.search(kind):
        return False
    return (norm(p.categoryName) in g.sub or norm(p.parentCategoryName) in g.top
            or bool(g.kind and kind and g.kind.search(kind)))


def _named_in_group(g: Group, p: Product) -> bool:
    """N: 상품명 규칙 — 종류 칸으로는 못 잡는 상품(10-17 D06: 종류 '카페세트' 의 아메리카노가 커피 비선호를 빠져나감)."""
    name = p.name or ""
    return bool(g.names and g.names.search(name)) and not (g.name_drop and g.name_drop.search(name))


def _group_of(text: str) -> Group | None:
    t = norm(text)
    stem = _SUFFIX.sub("", t)
    return _groups().get(t) or (_groups().get(stem) if len(stem) >= 1 else None)


def _category_fallback(text: str) -> Matcher | None:
    """어휘(묶음)에 없는 카테고리 태그 — 꼬리('세트 · 장비')를 뗀 글자가 종류 · 소분류 이름 · 상품명에 들어 있으면."""
    t = norm(text)
    if len(t) < MIN_LEN:
        return None
    stem = _SUFFIX.sub("", t)
    stem = stem if len(stem) >= MIN_LEN else t

    def plain(p: Product) -> bool:
        return any(stem in norm(f) for f in (p.productKind, p.categoryName, p.name) if f and len(norm(f)) >= MIN_LEN)
    return plain


# ---- 상품 태그 · 사람 태그 맞추기

def _compute(brand: str, name: str, kind: str | None, category: str, parent: str | None,
             catalog_key: int = 0) -> tuple[tuple[str, str, str], ...]:
    p = _P(brand or "", name or "", kind, category or "", parent)
    out: dict[tuple[str, str], str] = {}
    for part in _brand_parts(p.brand):
        out.setdefault(("브랜드", brand_canonical(part)), "field")
    low = p.name.lower()
    own = {base(p.brand), norm(p.brand)} | {base(x) for x in _brand_parts(p.brand)}
    for m in _COLLAB.finditer(p.name):                       # 협업 상대가 카탈로그 브랜드면 그 브랜드도
        for side in m.groups():
            key = base(side)
            if key in _CATALOG_BRANDS and key not in own and key not in _ip_name_keys():
                out.setdefault(("브랜드", _CATALOG_BRANDS[key]), "name")
    if "호환" not in p.name:                                 # 호환 상품의 브랜드 이름은 '어디에 맞나'다
        for e, other, surf in _brand_mentions():
            if own & other:
                continue                                     # 이름만 이어진 다른 회사(레고 ↔ 레고트)
            if any(_mentions_brand(low, s, pat) for s, pat in surf):
                out.setdefault(("브랜드", e.canon), "name")
    for tag, hit in _ip_taggers():
        if hit(p):
            out.setdefault(("IP", tag), "name")
    nn = norm(p.name)
    for g in dict.fromkeys(_groups().values()):
        if g.drop and g.drop.search(p.productKind or ""):
            continue                                         # 뺄 종류(커피잔 · 와인잔 …)는 이름에 묶음 말이 있어도 아니다
        if _in_group(g, p):
            out.setdefault(("카테고리", g.name), "field")
        elif _named_in_group(g, p) or (len(norm(g.name)) >= MIN_LEN and norm(g.name) in nn):
            out.setdefault(("카테고리", g.name), "name")
    return tuple((a, t, v) for (a, t), v in out.items())


@dataclass(frozen=True)
class _P:
    brand: str
    name: str
    productKind: str | None
    categoryName: str
    parentCategoryName: str | None


_compute_cached = lru_cache(maxsize=1 << 14)(_compute)


def product_tags(p: Product) -> list[dict[str, str]]:
    """상품 하나의 축 태그 `[{axis, tag, via}]` — DB `products.tags` 에 넣는 모양(0011)."""
    return [{"axis": a, "tag": t, "via": v} for a, t, v in
            _compute_cached(p.brand, p.name, p.productKind, p.categoryName, p.parentCategoryName, _CATALOG_KEY[0])]


def tag_set(p: Product) -> frozenset[tuple[str, str]]:
    """상품의 (축, 대표 이름) 집합 — DB 에서 온 판이 맞는 태그(`p.tags`)가 있으면 그것, 없으면 같은 함수로 계산."""
    got = getattr(p, "tags", None)
    if got is not None:
        return got
    return frozenset((a, t) for a, t, _ in
                     _compute_cached(p.brand, p.name, p.productKind, p.categoryName, p.parentCategoryName, _CATALOG_KEY[0]))


@lru_cache(maxsize=4096)
def canonicals(axis: str, text: str) -> frozenset[str]:
    """사람 태그 → 대표 이름(들). 어휘에 없으면 빈 집합(글자 대조로)."""
    if axis == "브랜드":
        es = _brands().get(norm(text)) or _brands().get(base(text)) or []
        return frozenset(e.canon for e in es)
    if axis == "IP":
        _, ip_key, char_key = _ips()
        k = norm(text)
        if k in ip_key:
            return frozenset({ip_key[k]})
        if k in char_key:
            return frozenset(char_key[k])
        words = text.split()
        if len(words) > 1:                                   # 'LLM 이 쓴 '카카오프렌즈 춘식이' — 캐릭터 먼저, 없으면 IP
            for w in reversed(words):
                if norm(w) in char_key:
                    return frozenset(char_key[norm(w)])
            for w in words:
                if norm(w) in ip_key:
                    return frozenset({ip_key[norm(w)]})
        return frozenset()
    g = _group_of(text)
    return frozenset({g.name}) if g else frozenset()


@lru_cache(maxsize=4096)
def matcher(axis: str, text: str) -> Matcher:
    """비선호 태그 하나 → 상품 하나에 걸리나. 대표 이름이 있으면 상품 태그와 겹치는지, 없으면 글자 대조."""
    keys = {(axis, c) for c in canonicals(axis, text)}
    if keys:
        return lambda p: not keys.isdisjoint(tag_set(p))
    if axis == "카테고리" or axis not in AXES:
        return _category_fallback(text) or _never
    if len(norm(text)) < MIN_LEN:
        return _never
    return _brand_fallback(text) if axis == "브랜드" else _ip_hit([IpName("", text, False)])


def _never(p: Product) -> bool:
    return False


@lru_cache(maxsize=4096)
def query_text(axis: str, text: str) -> str | None:
    """선호 태그 → 임베딩 검색어. 두 글자 이상은 그대로. 한 글자는 검색에서 엉뚱한 말에 붙는다(10-17 D06: '차' → 차량용품 9개) —
    카테고리 묶음 말이면 묶음의 두 글자 이상 이름들로, 브랜드면 두 글자 이상 대표 이름으로 풀어 쓰고, 그 밖은 None(검색어에서 뺌).
    저장되는 태그(`preferred_tags`)는 그대로다 — 바뀌는 것은 검색어뿐."""
    if len(norm(text)) >= MIN_LEN:
        return text
    if axis == "브랜드":
        words = sorted(c for c in canonicals(axis, text) if len(norm(c)) >= MIN_LEN)
    elif axis == "카테고리":
        g = _group_of(text)
        one = text.strip()                               # 그 한 글자가 낱말로 남은 별칭('차 선물')은 같은 혼동을 다시 부른다('새차선물')
        words = [a for a in g.aliases if len(norm(a)) >= MIN_LEN and one not in a.split()] if g else []
    else:
        words = []
    return " ".join(words) or None
