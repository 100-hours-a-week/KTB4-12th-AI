"""태그 그래프 — 규칙 C(넓은 말로 올림)가 '이 말이 저 태그의 상위인가'를 묻는 데 쓴다. 모델 안 부름.

노드  (축, 이름) — 종류: 묶음(category_groups) · 대분류 · 소분류 · 종류(카탈로그) · 낱말(묶음 K 의 글자) · 브랜드 · 브랜드계열 · IP · 캐릭터
간선  상위 → 하위(넓은 말 → 좁은 말): 대분류→소분류→종류 · 묶음→묶음 · 묶음→소분류/종류(상품 60% 이상이 묶음에 걸림) · 묶음→낱말
      · IP→캐릭터 · 브랜드계열→브랜드
이름표 정규화한 표기 → 노드(여럿이면 '애매'). 대표 이름 · 별칭 · 카탈로그 이름 낱말(수건/타월) · 보강 별칭(graph_extra.tsv)

실험(10-26 tag_graph.build)을 옮겼다. 실험은 10-26 카탈로그로 만든 표를 읽었고, 서버는 **지금 활성 카탈로그**로 만든다(카탈로그가 바뀌면 다시).
"""
import re
from collections import defaultdict
from collections.abc import Iterable
from functools import cache
from pathlib import Path

from profiling import vocab
from profiling.schemas import ProductRecord

EXTRA = Path(__file__).resolve().parent / "graph_extra.tsv"
AXES = ("카테고리", "브랜드", "IP")
GROUP_SHARE = 0.6
SUFFIX = re.compile(r"(선물세트|세트|선물|장비|용품|제품|류)$")
GENERIC = re.compile(r"기타|^etc$|상세페이지")

Key = tuple[str, str]


def n(s: str | None) -> str:
    return vocab.norm(s or "")


class Graph:
    def __init__(self) -> None:
        self.kind: dict[Key, str] = {}
        self.down: dict[Key, set[Key]] = defaultdict(set)
        self.up: dict[Key, set[Key]] = defaultdict(set)
        self.labels: dict[Key, set[Key]] = defaultdict(set)      # (축, 정규화 표기) → 노드

    def node(self, ax: str, name: str, kind: str) -> Key:
        key = (ax, name)
        self.kind.setdefault(key, kind)
        self.label(ax, name, key)
        return key

    def label(self, ax: str, text: str, key: Key) -> None:
        t = n(text)
        if len(t) >= 1 and not GENERIC.search(t):
            self.labels[(ax, t)].add(key)

    def edge(self, hi: Key, lo: Key) -> None:
        if hi != lo:
            self.down[hi].add(lo)
            self.up[lo].add(hi)

    def resolve(self, ax: str, text: str) -> set[Key]:
        """표기 → 노드 집합. 없으면 ① 꼬리(세트 · 용품 …)를 떼고 ② IP 는 낱말마다(캐릭터 먼저 — '카카오프렌즈 춘식이')
        ③ 카테고리는 그 말을 이름에 품은 종류 노드들('등산용품' → 등산스틱 · 등산양말 …, 30개까지)."""
        t = n(text)
        got = self.labels.get((ax, t))
        if not got:
            s = SUFFIX.sub("", t)
            got = self.labels.get((ax, s)) if len(s) >= 1 and s != t else None
        if not got and ax == "IP" and len((text or "").split()) > 1:
            for w in reversed(text.split()):
                got = self.labels.get(("IP", n(w)))
                if got:
                    break
        if not got and ax == "카테고리":
            s = SUFFIX.sub("", t)
            if len(s) >= 2:
                ks = {k for k, kind in self.kind.items() if kind == "종류" and k[0] == "카테고리" and s in n(k[1])}
                got = ks if 0 < len(ks) <= 30 else None
        return set(got or ())

    @cache  # noqa: B019 — 그래프는 카탈로그 판마다 하나, 프로세스 동안 산다
    def below(self, key: Key) -> frozenset[Key]:
        seen, todo = set(), [key]
        while todo:
            k = todo.pop()
            for c in self.down.get(k, ()):
                if c not in seen:
                    seen.add(c)
                    todo.append(c)
        return frozenset(seen)

    def __hash__(self) -> int:
        return id(self)


def nodes(g: Graph, axis: str, tag: str) -> frozenset[Key]:
    """그 축에서 먼저 찾고, 없으면 다른 축에서(모델이 축을 잘못 붙인 경우)."""
    got = g.resolve(axis, tag) if axis in AXES else set()
    if not got:
        for ax in AXES:
            if ax != axis:
                got |= g.resolve(ax, tag)
    return frozenset(got)


def _split(s: str | None) -> list[str]:
    return [x.strip() for x in re.split(r"[/·,&]", s or "") if x.strip()]


def build(prods: Iterable[ProductRecord], extra: bool = True) -> Graph:
    prods = list(prods)
    g = Graph()
    by_kind, by_sub = defaultdict(list), defaultdict(list)
    for p in prods:                                                          # 카탈로그 위계
        top = g.node("카테고리", p.parentCategoryName, "대분류") if p.parentCategoryName else None
        for x in _split(p.parentCategoryName):
            if top and len(n(x)) >= 2:
                g.label("카테고리", x, top)                                  # '카페·디저트' → '디저트'로도
        sub = g.node("카테고리", p.categoryName, "소분류")
        if top:
            g.edge(top, sub)
        for x in _split(p.categoryName):
            g.label("카테고리", x, sub)
        if p.productKind and not GENERIC.search(p.productKind):
            k = g.node("카테고리", p.productKind, "종류")
            g.edge(sub, k)
            for x in _split(p.productKind):
                if len(n(x)) >= 2:
                    g.label("카테고리", x, k)
            by_kind[k].append(p)
        by_sub[sub].append(p)
    groups: dict[str, tuple] = {}
    for name, aliases, defs, _ in vocab._rows("category_groups.tsv"):        # 어휘 묶음
        key = g.node("카테고리", name.lstrip("@"), "묶음")
        groups[name] = (key, defs)
        g.kind[key] = "묶음"
        for a in vocab._split(aliases):
            g.label("카테고리", a, key)
    for name, (key, defs) in list(groups.items()):
        for part in defs.split():
            tag, _, val = part.partition(":")
            if part.startswith("@") and part in groups:
                g.edge(key, groups[part][0])
            elif tag == "K":
                for w in val.split("|"):
                    if re.fullmatch(r"[가-힣A-Za-z]{2,}", w):                    # 정규식 아닌 낱말만
                        hit = g.labels.get(("카테고리", n(w)))
                        lo = next(iter(hit)) if hit and len(hit) == 1 else g.node("카테고리", w, "낱말")
                        g.edge(key, lo)
        m = vocab.matcher("카테고리", name.lstrip("@"))
        groups[name] = (key, defs, frozenset(p.productId for p in prods if m(p)))
        for coll in (by_sub, by_kind):
            for node, ps in coll.items():
                if ps and sum(map(m, ps)) >= GROUP_SHARE * len(ps):
                    g.edge(key, node)
    for a, (ka, _, ha) in groups.items():                                     # 묶음 ⊃ 묶음 — B 상품의 60% 이상이 A 에도(캔들 ⊂ 향)
        for b, (kb, _, hb) in groups.items():
            if a != b and hb and len(hb) < len(ha) and len(ha & hb) >= GROUP_SHARE * len(hb):
                g.edge(ka, kb)
    for e in vocab._brand_entries():                                          # 브랜드
        key = g.node("브랜드", e.canon, "브랜드")
        for x in (*e.members, *e.aliases):
            g.label("브랜드", x, key)
    first = defaultdict(set)
    for p in prods:
        b = vocab.brand_canonical(p.brand)
        key = g.node("브랜드", b, "브랜드")
        g.label("브랜드", p.brand, key)
        w = p.brand.split()[0] if p.brand.split() else ""
        if len(n(w)) >= 2 and "(" not in w:
            first[w].add(key)
    for w, ms in first.items():                                               # '나이키 슈즈' · '나이키 골프' → 계열 '나이키'
        if len(ms) >= 2 and not g.labels.get(("브랜드", n(w))):
            fam = g.node("브랜드", w, "브랜드계열")
            for m_ in ms:
                g.edge(fam, m_)
    for ip, ch, al in vocab._rows("ip.tsv", 3):                               # IP · 캐릭터
        base = re.sub(r"\(.*?\)", "", ip).strip()
        ipk = g.node("IP", base, "IP")
        for x in re.findall(r"\((.*?)\)", ip):
            g.label("IP", x, ipk)
        if ch:
            ck = g.node("IP", ch.lstrip("~"), "캐릭터")
            g.edge(ipk, ck)
            for a in vocab._split(al):
                g.label("IP", a.lstrip("~"), ck)
    for p in prods:
        for ax, t in vocab.tag_set(p):
            if ax == "IP" and not g.resolve("IP", t):
                g.node("IP", t, "IP")
    if extra:
        for line in EXTRA.read_text(encoding="utf-8").splitlines():
            if line and not line.startswith("#"):
                alias, target = line.split("\t")
                ax, name = target.split(":", 1)
                for key in g.resolve(ax, name) or {g.node(ax, name, "보강")}:
                    g.label(ax, alias, key)
    return g
