"""입력 가드레일 — 내 선물 취향 · 리뷰 글을 LLM 에 보내기 **전에** 코드로 정리한다(10-13). LLM 에 맡기지 않는다.

  ① 정규화      NFKC · 보이지 않는 문자 제거 · 같은 글자 4번 이상 → 3번 · 공백 정리
  ② 개인정보    전화번호 · 이메일 · URL · 주민번호 꼴 · 카드/계좌 꼴(긴 숫자) → [연락처] · [이메일] · [링크] · [번호] 로 가림
  ③ 욕설        사전(띄어쓰기 · 숫자 · 기호 끼워 넣기 · 초성 변형까지) → 그 말만 [가림], 나머지 문장은 쓴다
  ④ 의미 없음   글자(한글 음절 · 영문) 비율이 낮음 · 자음/모음만(ㅋㅋㅋ) · 한두 글자 반복 · 키보드 막 치기 → 글을 버린다(별점은 그대로)
  ⑤ 인젝션 의심 "무시해 · 지시 · system · ignore previous · 중괄호 JSON" 꼴 → **그 글은 버린다**(별점 · 상품 정보는 그대로). 처음엔 표시만 했으나
               하네스(U13)에서 gemma4:12b 가 지시대로 태그를 만들어 검증기도 못 막았다(근거가 취향 문장이라) — 10-13 g2

결과는 GuardResult(정리된 글 또는 None, 표시 집합, 가린 개수). 원문은 로그에 남기지 않는다 — 표시와 개수만.
사전 · 패턴은 작게 시작한다(우리 목록). 넓히려면 PROFANITY · INJECTION 에 더한다 — 바꾸면 GUARD_VERSION 을 올린다.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field

GUARD_VERSION = "g4"   # g1: 인젝션 표시만 → g2: 인젝션 글 버림 → g3: 욕설 사전 갈래별 확장 + 오탐 예외 → g4: 주민 · 계좌 번호가 전화번호로 반만 가려지던 것 수정(10-13)

# ---------------------------------------------------------------- ② 개인정보
_PII: list[tuple[str, str, re.Pattern[str]]] = [          # 순서가 중요 — 긴 번호(주민 · 계좌 · 카드)를 먼저, 전화번호는 숫자 경계로
    ("email", "[이메일]", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")),
    ("url", "[링크]", re.compile(r"(?:https?://|www\.)\S+|\b[\w-]+\.(?:com|net|kr|co\.kr|org|io|me|ly)(?:/\S*)?\b", re.IGNORECASE)),
    ("rrn", "[번호]", re.compile(r"(?<!\d)\d{6}\s?-\s?[1-4]\d{6}(?!\d)")),
    ("long_number", "[번호]", re.compile(r"(?<!\d)(?:\d[-\s]?){11,19}\d(?!\d)")),     # 카드 · 계좌 꼴(숫자 12개 이상)
    ("phone", "[연락처]", re.compile(r"(?<![\d-])(?:\+?82[-\s.]?)?0\d{1,2}[-\s.]?\d{3,4}[-\s.]?\d{4}(?![\d-])")),
]

# ---------------------------------------------------------------- ③ 욕설 (작게 시작 — 넓히면 GUARD_VERSION 올리기)
# 갈래별로 둔다 — 넓힐 때 어느 갈래인지 보이게. 넓히면 GUARD_VERSION 을 올리고 아래 ALLOW(오탐 예외)도 함께 본다.
# 기준: 선물 리뷰에서 **정상 뜻으로 거의 안 쓰는 꼴**만. 흔한 정상 단어와 겹치는 것은 넣지 않거나(씹 · 졸라 · 자지 · 보지 · 걸레 · 튀기 · 홍어 ·
# 애자 · 한남 · 미친 · 개 단독) 겹치는 정상 단어를 ALLOW 에 둔다.
PROFANITY_GROUPS: dict[str, tuple[str, ...]] = {
    "욕설": (
        "시발", "씨발", "씨팔", "시팔", "시벌", "씨벌", "쓰발", "쓰벌", "씌발", "시바알", "씨바", "씨빠", "씨부랄", "씨부럴", "씹새", "씹할", "씹창", "씹질",
        "썅", "쌍놈", "쌍년", "니기미", "니미럴", "니미씨", "우라질", "염병", "옘병", "제기랄", "빌어먹을",
        "좆", "좃같", "존나", "존내", "존니", "좆나", "좆같", "개좆", "개씨발", "개새끼", "개새", "개색기", "개섀끼", "개세끼", "개쉐이", "개자식",
        "개놈", "개년", "개같은", "개같이", "개빡치", "새끼", "쌔끼", "섀끼", "세끼야", "지랄", "지럴", "미친놈", "미친년", "미친새", "또라이", "돌아이", "똘아이",
        "병신", "븅신", "빙신", "병쉰", "등신", "머저리", "찐따", "찌질이", "꺼져", "닥쳐", "닥치라", "뒤져", "뒤질래", "디져", "엿먹", "엿이나",
    ),
    "자모 줄임": ("ㅅㅂ", "ㅆㅂ", "ㅄ", "ㅂㅅ", "ㅈㄴ", "ㅈㄹ", "ㅅㄲ", "ㄲㅈ", "ㄷㅊ", "ㅁㅊ", "ㅗㅗ"),
    "패드립": ("느금", "니애미", "니애비", "애미", "애비", "엠창", "앰창", "니엄마", "느개비"),
    "성적": ("섹스", "보짓", "딸딸이", "창녀", "화냥년", "갈보", "씹질"),
    "비하 · 혐오": (
        "김치녀", "된장녀", "한남충", "맘충", "급식충", "틀딱", "진지충", "꼴페미", "메갈", "일베충", "짱깨", "쪽바리", "깜둥이", "똥남아", "정신병자", "장애새끼",
        "호모새끼",
    ),
    "영어": (
        "fuck", "fucking", "fxck", "fuk", "motherfucker", "shit", "bullshit", "bitch", "asshole", "bastard", "dick", "cunt", "slut", "whore",
        "retard", "nigger", "nigga", "faggot", "wtf", "stfu",
    ),
}
PROFANITY = tuple(w for ws in PROFANITY_GROUPS.values() for w in ws)

# 오탐 예외 — 욕설 꼴을 품은 정상 단어. 가리기 전에 잠시 감췄다가 되돌린다.
ALLOW = (
    "시발점", "시발역", "새끼손가락", "새끼발가락", "새끼줄", "새끼고양이", "새끼강아지", "등신대", "개새해", "염병학",
    "호모사피엔스", "dickens", "dickinson", "섹스턴", "유니섹스", "unisex",   # 10-13 상품명 전체 훑기에서 찾은 오탐(유니섹스 · 새끼 고릴라)
)
_ANIMAL = r"(?:강아지|고양이|오리|곰|토끼|돼지|사자|호랑이|펭귄|여우|판다|병아리|양|소|말|새|물고기|햄스터|다람쥐|고릴라|원숭이|코끼리|기린|공룡|너구리|수달|거북이|사슴|늑대)"
_ALLOW_RE = re.compile("|".join([re.escape(w) for w in sorted(ALLOW, key=len, reverse=True)]
                                + [rf"{_ANIMAL}\s?새끼", rf"새끼\s?{_ANIMAL}"]), re.IGNORECASE)   # 동물 새끼("강아지 새끼 인형")
_SEP = r"[\d_.\-*~!@#$%^&+=|/\\,·ㆍ]*"     # 글자 사이에 끼워 넣는 것(시1발 · 시.발) — 공백은 넣지 않는다: "다시 발견"의 "시 발" 같은 오탐


def _word_pattern(word: str) -> re.Pattern[str]:
    chars = [re.escape(c) for c in word.replace(" ", "")]
    jamo_only = all("ㄱ" <= c <= "ㆎ" for c in word.replace(" ", ""))
    sep = _SEP[:-2] + r"\s]*" if jamo_only else _SEP          # 자모 줄임은 띄어 써도 잡는다("ㅈ ㄴ") — 자모끼리는 정상 문장과 안 겹침
    return re.compile(sep.join(chars), re.IGNORECASE)


_PROFANITY_RE = [_word_pattern(w) for w in sorted(set(PROFANITY), key=len, reverse=True)]

# ---------------------------------------------------------------- ⑤ 인젝션 의심 (표시만)
INJECTION = re.compile(
    r"(이전|앞의|위의|모든)\s*(지시|명령|규칙|프롬프트)|지시\s*(를|는)?\s*무시|무시\s*하고|시스템\s*프롬프트|프롬프트\s*(를|는)?\s*(보여|알려|출력)|"
    r"태그(로|에)\s*.{0,10}(넣어|추가|만들어)|역할\s*을?\s*바꿔|너는\s*이제|"
    r"ignore\s+(all\s+)?(previous|above|prior)|system\s*prompt|you\s+are\s+now|\bassistant\s*:|\bsystem\s*:|\{\s*\"",
    re.IGNORECASE,
)

# ---------------------------------------------------------------- ④ 의미 없음
_HANGUL = re.compile(r"[가-힣]")
_LATIN = re.compile(r"[A-Za-z]")
_JAMO = re.compile(r"[ㄱ-ㅎㅏ-ㅣ]")
_KEYBOARD = ("qwer", "asdf", "zxcv", "wert", "sdfg", "xcvb", "ㅂㅈㄷㄱ", "ㅁㄴㅇㄹ", "ㅋㅌㅊㅍ")


@dataclass
class GuardResult:
    text: str | None                       # 정리된 글. 의미 없음 · 빈 글이면 None(별점은 그대로 쓴다)
    flags: set[str] = field(default_factory=set)   # pii_masked · profanity · gibberish · injection_suspect · empty
    counts: Counter = field(default_factory=Counter)


def normalize(text: str) -> str:
    # NFKC 는 전각 · 변형 글자를 표준으로 바꾸지만 한글 자모(ㅋ · ㅅ)를 조합용 자모(ᄏ · ᄉ)로 바꿔 버린다 → 자모는 그대로 둔다
    t = "".join(c if "\u3131" <= c <= "\u318e" else unicodedata.normalize("NFKC", c) for c in text)
    t = re.sub("[\u200b-\u200f\u2060\ufeff]", "", t)                    # 보이지 않는 문자(폭 0 공백 · 방향 표시 · BOM)
    t = re.sub(r"(\D)\1{3,}", r"\1\1\1", t)                             # 같은 글자 4번 이상 → 3번(숫자는 그대로 — 전화번호 1111)
    return re.sub(r"\s+", " ", t).strip()


def is_gibberish(text: str) -> bool:
    """의미 없는 글인가 — 마스킹 뒤의 글로 본다(가림 표시는 글자로 세지 않음)."""
    core = re.sub(r"\[(?:연락처|이메일|링크|번호|가림)\]", "", text)
    core = re.sub(r"[\s\W_]", "", core)
    if not core:
        return True
    letters = len(_HANGUL.findall(core)) + len(_LATIN.findall(core))
    no_digit = re.sub(r"\d", "", core)
    if letters == 0 or letters / max(len(no_digit), 1) < 0.5:          # 이모지 · 기호 · 자음 위주(숫자는 비율에서 뺀다 — "35000원 2026-10-12 배송")
        return True
    if len(_JAMO.findall(core)) / len(core) > 0.5:                      # ㅋㅋㅋ · ㅎㅎ · ㅠㅠ
        return True
    uniq = len(set(core.lower()))
    if (len(core) >= 3 and uniq == 1) or (len(core) >= 4 and uniq <= 2):  # 한두 글자 반복(반복 줄임 뒤 "아아아" · "하하하하")
        return True
    low = core.lower()
    if len(core) <= 12 and any(k in low for k in _KEYBOARD):           # 짧은 글이 키보드 줄 그대로("qwerty 키보드" 리뷰는 통과)
        return True
    words = re.findall(r"[A-Za-z]{5,}", text)                           # 모음 없는 긴 영문 = 막 치기
    return bool(words) and all(not re.search(r"[aeiouy]", w, re.IGNORECASE) for w in words) and not _HANGUL.search(core)


def mask_profanity(text: str) -> tuple[str, int]:
    """욕설을 [가림] 으로 — 오탐 예외(ALLOW)는 잠시 자리표로 감췄다가 되돌린다. (바뀐 글, 가린 개수)."""
    kept: list[str] = []

    def hide(m: re.Match[str]) -> str:
        kept.append(m.group(0))
        return f"\x00{len(kept) - 1}\x00"

    t = _ALLOW_RE.sub(hide, text) if ALLOW else text
    total = 0
    for pat in _PROFANITY_RE:
        t, n = pat.subn("[가림]", t)
        total += n
    t = re.sub(r"\x00(\d+)\x00", lambda m: kept[int(m.group(1))], t)
    return t, total


def clean(text: str | None) -> GuardResult:
    """글 하나(취향 문장 또는 리뷰 글) → GuardResult. None · 공백이면 text=None + empty."""
    if text is None or not text.strip():
        return GuardResult(None, {"empty"})
    res = GuardResult(normalize(text))
    t = res.text or ""
    for name, mark, pat in _PII:
        t, n = pat.subn(mark, t)
        if n:
            res.counts[name] += n
            res.flags.add("pii_masked")
    t, n = mask_profanity(t)
    if n:
        res.counts["profanity"] += n
        res.flags.add("profanity")
    if INJECTION.search(t):                      # 지시문 꼴 — 그 글은 LLM 으로 보내지 않는다(10-13 하네스: 표시만 했더니 gemma 가 지시대로 '루이비통 · 샤넬' 태그를 냄)
        res.counts["injection_suspect"] += 1
        res.flags.add("injection_suspect")
        res.text = None
        return res
    if is_gibberish(t):
        res.counts["gibberish"] += 1
        res.flags.add("gibberish")
        res.text = None
        return res
    res.text = t
    return res


# ---------------------------------------------------------------- ③′ 태그 검사(검증기가 부른다)
_SENTENCE_END = re.compile(r"(요|니다|[었았했한좋싫]다|이다|네요|죠|ㅋ|ㅎ)[.!?~]*$|[.!?]$")   # "소음 · 판다" 같은 명사는 걸리지 않게
MAX_TAG_LEN = 20


def tag_problem(tag: str) -> str:
    """태그 글자 하나의 문제 — 빈 문자열이면 문제 없음. 검증기 버림 사유로 쓴다."""
    t = normalize(tag)
    if any(pat.search(t) for _, _, pat in _PII):
        return "개인정보"
    if mask_profanity(t)[1] or "[가림]" in t:
        return "욕설"
    if len(t) > MAX_TAG_LEN:
        return f"너무 김(>{MAX_TAG_LEN}자)"
    if _SENTENCE_END.search(t):
        return "문장형"
    if is_gibberish(t):
        return "의미 없음"
    return ""
