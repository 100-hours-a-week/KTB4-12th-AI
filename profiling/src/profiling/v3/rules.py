"""비선호 후처리 규칙 A · B · C(실험 rules_1030) — 판정(12B 질문 · Laya)으로 거른 **뒤** 비선호 태그에만 건다. 모델 안 부름.

  A 근거 없음 버림   비선호 태그의 근거(evidence)도 태그 말도 입력 글에 없으면 버린다.
  B 리뷰 브랜드 → 종류  리뷰(r*)에서 뽑은 브랜드 비선호인데 그 리뷰 글이 브랜드 이름은 안 쓰고 분류(중분류) · 종류 이름을 쓰면
                    그 이름의 카테고리 비선호로 바꾼다.
  C 넓은 말로 올림   근거 문장에 나온 카테고리 노드가 태그 노드의 상위(그래프)이면 그 상위 이름으로('음향기기 선물은 그만' →
                    이어폰 · 헤드폰 → 음향기기 하나). 상위가 여럿이면 가장 긴 표기. 태그를 근거에 그대로 쓴 경우는 올리지 않는다.
  바뀐 뒤 같은 (축, 태그) 비선호는 하나만 남긴다.

실험과 다른 점: 규칙 C 에서 길이가 같은 상위가 여럿이면 실험은 집합 순서(실행마다 달라짐, PYTHONHASHSEED 로 고정)로 골랐다 —
서버는 이름 순서로 고른다(늘 같은 답).
"""
from collections.abc import Callable

from profiling.v3 import text as tx
from profiling.v3.graph import Graph, n, nodes


def rule_a(inp: str, t: dict) -> bool:
    s = n(inp)
    ev, tg = n(t.get("evidence") or ""), n(t["tag"])
    return (len(ev) >= 2 and ev in s) or (len(tg) >= 1 and tg in s)


def rule_b(rv: dict[str, dict], t: dict) -> dict:
    if t["axis"] != "브랜드" or not str(t.get("ref", "")).startswith("r") or t["ref"] not in rv:
        return t
    r = rv[t["ref"]]
    txt = n(r["text"])
    if n(r["brand"]) in txt or n(t["tag"]) in txt:
        return t
    for name in (r["mid"], r["kind"]):
        if n(name) and n(name) in txt:
            return {**t, "axis": "카테고리", "tag": name}
    return t


def make_rule_c(g: Graph) -> Callable[[dict], dict]:
    cat = sorted((lab, sorted(keys)) for (ax, lab), keys in g.labels.items() if ax == "카테고리" and len(lab) >= 2)

    def rule_c(t: dict) -> dict:
        if t["axis"] != "카테고리":
            return t
        ev = n(t.get("evidence") or "")
        mine = nodes(g, t["axis"], t["tag"])
        if not ev or not mine or n(t["tag"]) in ev:          # 태그를 근거에 그대로 썼으면 그 말이 맞다
            return t
        best = None
        for lab, keys in cat:
            if lab in ev and lab != n(t["tag"]):
                for k in keys:
                    if any(m in g.below(k) for m in mine) and (best is None or len(lab) > len(n(best[1]))):
                        best = k
        return {**t, "tag": best[1]} if best else t
    return rule_c


def apply(keep: list[dict], inp: str, rule_c: Callable[[dict], dict]) -> list[dict]:
    rv = tx.review_lines(inp)
    out, seen = [], set()
    for t in keep:
        if t.get("polarity") == "비선호":
            if not rule_a(inp, t):
                continue
            t = rule_c(rule_b(rv, t))
            k = (t["axis"], n(t["tag"]))
            if k in seen:
                continue
            seen.add(k)
        out.append(t)
    return out
