"""README.md 에서 링크로 닿는 문서를 트리로 그려 docs/문서_목록.md 의 tree 블록을 다시 쓴다.

  python3 docs/assets/build_doc_tree.py          문서가 늘거나·바뀌거나·이름이 바뀌면 다시 돌린다 (그림 스크립트와 같은 습관)

트리는 **실제 링크**를 따라간다 — 손으로 적은 목록은 낡지만 링크는 낡으면 깨져서 드러난다.
README 가 링크하지 않는 문서는 "못 가는 문서"로 따로 찍고 종료 코드 1 을 낸다: 검토 트리에서 빠졌다는 뜻이다.
같은 문서는 처음 나온 자리에만 보인다(문서끼리 서로 링크하므로 그대로 그리면 그물이 된다).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent            # docs/assets
ROOT = HERE.parent.parent                          # profiling/
INDEX = ROOT / "docs" / "문서_목록.md"
START, END = "<!-- tree:start -->", "<!-- tree:end -->"
LINK = re.compile(r"(?<!!)\[([^\]]*)\]\(([^)\s]+)\)")   # 그림(![…]) 은 제외
MAX_DEPTH = 4


def all_docs() -> set[Path]:
    docs = {ROOT / "README.md", ROOT / "이름_대조표.md"}
    docs |= set(ROOT.glob("docs/**/*.md")) | set(ROOT.glob("tools/**/README.md"))
    return {p.resolve() for p in docs if p.exists() and "_템플릿" not in p.name}


def links_of(md: Path) -> list[Path]:
    out: list[Path] = []
    for _, tgt in LINK.findall(md.read_text(encoding="utf-8")):
        if tgt.startswith(("http", "#", "mailto")):
            continue
        p = (md.parent / tgt.split("#")[0]).resolve()
        if p.is_dir():
            p = p / "README.md"
        if p.suffix == ".md" and p.exists() and ROOT in p.parents and p not in out:
            out.append(p)
    return out


def title_of(md: Path) -> str:
    for line in md.read_text(encoding="utf-8").splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return md.name


def build() -> tuple[list[str], set[Path]]:
    lines: list[str] = []
    seen: set[Path] = set()

    def rel(p: Path) -> str:                     # 문서_목록.md(docs/) 기준 상대경로
        docs = ROOT / "docs"
        return str(p.relative_to(docs)) if docs in p.parents else "../" + str(p.relative_to(ROOT))

    # 너비 우선 — 각 문서를 **가장 얕은 자리**(README 에서 가장 가까운 곳)에 둔다.
    # 깊이 우선으로 가면 README 가 직접 링크한 문서가 다른 문서 밑에 먼저 잡혀 엉뚱한 곳에 놓인다.
    start = ROOT / "README.md"
    seen.add(start)
    children: dict[Path, list[Path]] = {start: []}
    frontier = [start]
    depth = 0
    while frontier and depth < MAX_DEPTH:
        nxt: list[Path] = []
        for md in frontier:
            for child in links_of(md):
                if child in seen:
                    continue
                seen.add(child)
                children[md].append(child)
                children[child] = []
                nxt.append(child)
        frontier, depth = nxt, depth + 1

    def emit(md: Path, level: int) -> None:
        for child in children[md]:
            lines.append("  " * level + f"- [{child.relative_to(ROOT)}]({rel(child)}) — {title_of(child)}")
            emit(child, level + 1)

    lines.append(f"- [README.md]({rel(start)}) — {title_of(start)}  ← 시작점")
    emit(start, 1)
    return lines, seen


def main() -> int:
    lines, seen = build()
    unreachable = sorted(all_docs() - seen)
    block = [START, "", "README 에서 링크를 따라 닿는 순서. 같은 문서는 처음 나온 자리에만 보인다. `python3 docs/assets/build_doc_tree.py` 가 만든다 — 손으로 고치지 않는다.", ""]
    block += lines
    if unreachable:
        block += ["", "**⚠ README 에서 링크로 못 가는 문서** — 검토 트리에서 빠진다. README 에 링크를 달거나 지운다:", ""]
        block += [f"- `{p.relative_to(ROOT)}`" for p in unreachable]
    block += ["", END]

    text = INDEX.read_text(encoding="utf-8")
    if START not in text or END not in text:
        print(f"✗ {INDEX.name} 에 {START} … {END} 표시가 없다", file=sys.stderr)
        return 2
    head, rest = text.split(START, 1)
    _, tail = rest.split(END, 1)
    INDEX.write_text(head + "\n".join(block) + tail, encoding="utf-8")

    print(f"트리 {len(seen)}개 문서 → {INDEX.relative_to(ROOT)}")
    if unreachable:
        print("✗ README 에서 못 가는 문서:", file=sys.stderr)
        for p in unreachable:
            print(f"    {p.relative_to(ROOT)}", file=sys.stderr)
        return 1
    print("✓ 모든 문서가 README 에서 닿는다")
    return 0


if __name__ == "__main__":
    sys.exit(main())
