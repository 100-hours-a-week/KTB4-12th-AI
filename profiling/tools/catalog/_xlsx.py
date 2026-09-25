"""xlsx 읽기 — 표준 라이브러리만 (zipfile + xml.etree). 의존성을 늘리지 않으려고 직접 푼다.

xlsx는 XML 몇 개를 담은 zip이다. 우리가 받는 파일(BE가 조회 결과를 내보낸 표)은 수식도 서식도 없고
문자열과 숫자뿐이라 아래 세 가지만 처리하면 충분하다.

  sharedStrings.xml  같은 문자열을 한 번만 저장하고 셀은 번호로 가리킨다 (t="s")
  inlineStr          셀 안에 문자열을 직접 넣는 방식 (t="inlineStr")
  셀 좌표            "BC12" 같은 A1 표기 → 열 번호. 빈 셀은 XML에 아예 없으므로 좌표로 자리를 맞춘다

숫자는 문자열 그대로 돌려준다 — 상품 ID·가격을 float로 바꾸면 정밀도가 깎이므로 호출자가 int()로 바꾼다.
날짜는 1900 기준 일련번호(예: 46216.82)로 나온다. 이 도구는 날짜를 쓰지 않는다.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_SHEET_FILE = re.compile(r"xl/worksheets/sheet(\d+)\.xml")
_LETTERS = re.compile(r"[A-Z]+")


class XlsxError(Exception):
    """xlsx 가 아니거나 기대한 모양이 아니다."""


def _column(ref: str) -> int:
    """셀 좌표 → 0부터 세는 열 번호. A=0 · Z=25 · AA=26."""
    letters = _LETTERS.match(ref)
    if letters is None:
        raise XlsxError(f"셀 좌표를 읽을 수 없다: {ref}")
    n = 0
    for ch in letters.group(0):
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def sheets(path: Path) -> list[str]:
    """시트 이름 목록 (파일에 들어 있는 순서)."""
    with zipfile.ZipFile(path) as z:
        book = ET.fromstring(z.read("xl/workbook.xml"))
    return [s.get("name", "") for s in book.iter(f"{NS}sheet")]


def rows(path: Path, sheet_index: int = 0) -> list[list[str | None]]:
    """시트 한 장을 행 목록으로. 셀 값은 문자열 또는 None(빈 칸)."""
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, FileNotFoundError) as e:
        raise XlsxError(f"xlsx 로 열 수 없다: {path} ({type(e).__name__})") from e
    with z:
        shared = _shared_strings(z)
        names = sorted((n for n in z.namelist() if _SHEET_FILE.fullmatch(n)),
                       key=lambda n: int(_SHEET_FILE.fullmatch(n).group(1)))  # type: ignore[union-attr]
        if sheet_index >= len(names):
            raise XlsxError(f"시트 {sheet_index} 가 없다 (총 {len(names)}장): {path}")
        sheet = ET.fromstring(z.read(names[sheet_index]))

    out: list[list[str | None]] = []
    for row in sheet.iter(f"{NS}row"):
        cells: dict[int, str | None] = {}
        for c in row.iter(f"{NS}c"):
            ref = c.get("r")
            if ref is None:
                continue
            cells[_column(ref)] = _value(c, shared)
        width = max(cells) + 1 if cells else 0
        out.append([cells.get(i) for i in range(width)])
    return out


def _shared_strings(z: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in z.namelist():
        return []
    doc = ET.fromstring(z.read("xl/sharedStrings.xml"))
    return ["".join(t.text or "" for t in si.iter(f"{NS}t")) for si in doc.iter(f"{NS}si")]


def _value(cell: ET.Element, shared: list[str]) -> str | None:
    if cell.get("t") == "inlineStr":
        return "".join(t.text or "" for t in cell.iter(f"{NS}t"))
    v = cell.find(f"{NS}v")
    if v is None or v.text is None:
        return None
    if cell.get("t") == "s":
        i = int(v.text)
        if i >= len(shared):
            raise XlsxError(f"sharedStrings 범위를 벗어난 셀: {cell.get('r')}")
        return shared[i]
    return v.text
