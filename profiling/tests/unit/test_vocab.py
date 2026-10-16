"""비선호 태그 대조 어휘(vocab) — 다른 말은 잇고, 넓게 걸리던 것은 막는다."""

from datetime import UTC, datetime

from profiling import vocab
from profiling.schemas import ProductRecord


def _p(*, brand="b", name="상품", kind=None, cat="c1", top="대1") -> ProductRecord:
    return ProductRecord(productId=1, name=name, brand=brand, description=None, categoryId=1, categoryName=cat,
                         parentCategoryId=1, parentCategoryName=top, productKind=kind, price=1000, availability="available",
                         updatedAt=datetime(2026, 10, 1, tzinfo=UTC), viewCount=1)


def hit(axis, tag, p):
    return vocab.matcher(axis, tag)(p)


def test_brand_alias_family_and_paren_tail():
    assert hit("브랜드", "SONY", _p(brand="소니"))                       # 별칭 → 정규형
    assert hit("브랜드", "소니", _p(brand="소니플레이스테이션"))           # 같은 계열(사용자 결정)
    assert hit("브랜드", "소니", _p(brand="케이스티파이", name="SONY WH-1000XM6 케이스"))   # 상품명에 낱말로
    assert hit("브랜드", "다이슨", _p(brand="다이슨(일반)"))               # 괄호 꼬리 무시
    assert hit("브랜드", "아디다스", _p(brand="아디다스코리아"))           # 어휘에 없어도 3글자 이상은 브랜드 칸에 들어 있으면


def test_brand_does_not_reach_other_companies():
    assert not hit("브랜드", "레고", _p(brand="레고트", name="레고트 텀블러"))       # 다른 브랜드
    assert not hit("브랜드", "코코도르", _p(brand="코코도르팜", name="코코도르팜 꽃다발"))
    assert not hit("브랜드", "브라운", _p(brand="b", name="찰리브라운 머그"))       # 상품명은 낱말일 때만
    assert hit("브랜드", "브라운", _p(brand="브라운(미용)"))


def test_ip_name_expands_to_characters_and_short_names_need_context():
    assert hit("IP", "카카오프렌즈", _p(name="춘식이 칫솔 살균기"))
    assert hit("IP", "치이카와", _p(name="먼작귀 하치와레 키링"))
    assert not hit("IP", "카카오프렌즈", _p(name="실리콘 콘 받침"))                  # 짧은 이름 '콘' — 같은 IP 문맥 없음
    assert hit("IP", "카카오프렌즈", _p(name="카카오프렌즈 콘 인형"))
    assert not hit("IP", "라인프렌즈", _p(brand="브라운", name="브라운 체온계"))       # 가전 BRAUN
    assert hit("IP", "라인프렌즈", _p(name="라인프렌즈 브라운 쿠션"))
    assert hit("IP", "쿠로미", _p(name="산리오 쿠로미 우산"))
    assert hit("IP", "새캐릭터", _p(name="새캐릭터 키링"))                           # 어휘에 없으면 글자 그대로


def test_category_groups_suffix_and_no_wide_subcategory_match():
    camp = _p(kind="기타캠핑용품", cat="스포츠·레저용품", top="취미·여가")
    assert hit("카테고리", "캠핑 장비", camp) and hit("카테고리", "캠핑장비", camp) and hit("카테고리", "캠핑용품", camp)
    tv = _p(kind="TV", cat="영상·음향기기", top="디지털·가전")
    hp = _p(kind="헤드폰", cat="영상·음향기기", top="디지털·가전")
    assert hit("카테고리", "음향기기", hp) and hit("카테고리", "사운드", hp)
    assert not hit("카테고리", "음향기기", tv)                                       # 소분류 이름 글자로 TV 까지 빼지 않음
    assert hit("카테고리", "한우 세트", _p(kind="한우 선물세트", cat="정육·수산"))       # 꼬리 '세트' 떼고
    assert hit("카테고리", "정육세트", _p(kind="한우 선물세트", cat="정육·수산"))
    assert not hit("카테고리", "정육", _p(kind="전복1kg 선물세트", cat="정육·수산"))
    fruit = _p(kind="감귤", cat="과일·견과")
    assert hit("카테고리", "과일 선물세트", fruit) and not hit("카테고리", "과일", _p(kind="견과 선물세트", cat="과일·견과"))
    assert hit("카테고리", "향초", _p(kind="소이캔들", cat="홈프래그런스"))
    assert hit("카테고리", "옷", _p(kind="롱 티셔츠", cat="의류", top="패션"))           # 한 글자 묶음 말
    assert not hit("카테고리", "필", _p(name="필통"))                                  # 묶음 아닌 한 글자는 대조 안 함
    assert hit("카테고리", "수건", _p(kind="수건/타월", cat="욕실용품"))                 # 묶음 아니면 글자 대조(#13 그대로)


def test_data_files_have_no_counts_or_example_names():
    for f in vocab.DATA.glob("*.tsv"):
        for line in f.read_text(encoding="utf-8").splitlines():
            if not line.startswith("#"):
                assert not any(c.strip().isdigit() for c in line.split("\t")), (f.name, line)   # 공개 저장소: 상품 수 없음
                assert len(line.split("\t")) <= 5, (f.name, line)                                 # 예시 상품명 칸 없음


# ---- 상품 축 태그(0011) · 대표 이름 맞추기

def _tags(p):
    return {(t["axis"], t["tag"], t["via"]) for t in vocab.product_tags(p)}


def test_product_tags_brand_field_and_maker_named_in_seller_products():
    assert ("브랜드", "소니", "field") in _tags(_p(brand="소니플레이스테이션", name="듀얼센스"))            # 같은 계열 대표 이름
    assert ("브랜드", "마샬", "name") in _tags(_p(brand="오디오(LuX)", name="[정품] MARSHALL ACTON III 스피커"))   # 판매처 상품의 제조사
    assert not any(a == "브랜드" and t == "헬로키티" for a, t, _ in _tags(_p(brand="포맨트", name="포맨트 헬로키티 고체향수")))  # IP 이름은 IP 축
    assert ("IP", "헬로키티", "name") in _tags(_p(brand="포맨트", name="포맨트 헬로키티 고체향수"))


def test_product_tags_skip_compat_gift_seller_and_color_mentions():
    assert not any(t == "애플" for _, t, _ in _tags(_p(brand="신지모루", name="차량용 거치대 애플 갤럭시 호환")))
    assert not any(t == "맥(MAC)" for _, t, _ in _tags(_p(brand="로지텍", name="MX Anywhere 3S for Mac")))
    assert not any(t == "스타벅스" for _, t, _ in _tags(_p(brand="신키네도", name="롤케이크 리뷰이벤트 스타벅스 100% 증정")))
    assert not any(t == "예스24" for _, t, _ in _tags(_p(brand="북아이템", name="[예스24] 투명독서대")))      # 판매처 — 언급 안 씀
    assert not any(t == "브라운" for _, t, _ in _tags(_p(brand="조셉앤스테이시", name="니트 스몰 헬로키티 탄 브라운")))   # 한글은 색 이름
    assert ("브랜드", "브라운", "name") in _tags(_p(brand="하이마트", name="BRAUN 전기면도기 시리즈9"))
    assert not any(t == "이마트,신세계상품권" for _, t, _ in _tags(_p(brand="하이마트", name="LG 모니터")))   # 하이마트 ≠ 이마트


def test_product_tags_ip_alias_to_one_name_and_groups():
    tags = _tags(_p(name="산리오 마이 멜로디 키링"))
    assert ("IP", "마이멜로디", "name") in tags and not any(t == "마이 멜로디" for _, t, _ in tags)
    assert ("IP", "산리오", "name") in tags
    hp = _tags(_p(kind="헤드폰", cat="영상·음향기기", top="디지털·가전"))
    assert ("카테고리", "음향기기", "field") in hp and ("카테고리", "전자기기", "field") in hp
    assert not any(t == "커피" for _, t, _ in _tags(_p(kind="커피잔/찻잔", name="커피잔 세트")))          # 뺄 종류는 이름에 있어도 아님


def test_canonicals_and_matcher_use_the_db_tags_when_given():
    assert vocab.canonicals("브랜드", "SONY") == {"소니"}
    assert vocab.canonicals("브랜드", "SAINT LAURENT") == {"입생로랑", "생 로랑"}                        # 겹치는 별칭은 둘 다
    assert vocab.canonicals("IP", "카카오프렌즈 춘식이") == {"춘식이"}
    assert vocab.canonicals("카테고리", "캠핑 장비") == {"캠핑용품"}
    assert vocab.canonicals("카테고리", "수건") == frozenset()                                          # 어휘에 없으면 글자 대조
    p = _p(brand="b", name="그냥 상품")
    assert not hit("브랜드", "소니", p)
    q = p.model_copy(update={"tags": frozenset({("브랜드", "소니")})})                                  # DB 에서 판이 맞는 태그가 오면 그것을 쓴다
    assert hit("브랜드", "SONY", q)


def test_rule_fingerprint_pins_rule_version():
    """규칙이 바뀌어 결과가 달라지면 이 지문이 깨진다 — vocab.RULE 을 올리고(배포 때 상품 태그를 다시 계산하게) 아래 값을 고친다."""
    import hashlib
    import json as _json
    sample = [_p(brand="소니플레이스테이션", name="듀얼센스"), _p(brand="오디오(LuX)", name="MARSHALL 스피커"),
              _p(brand="포맨트", name="포맨트 헬로키티 고체향수"), _p(name="카카오프렌즈 콘 인형"), _p(name="실리콘 콘 받침"),
              _p(kind="기타캠핑용품", cat="스포츠·레저용품", top="취미·여가"), _p(kind="TV", cat="영상·음향기기", top="디지털·가전"),
              _p(brand="레고트", name="레고트 텀블러"), _p(kind="한우 선물세트", cat="정육·수산", top="식품"),
              _p(brand="지티기어", name="로지텍 G29 레이싱휠 PS5 PS4 PC"), _p(brand="반얀트리X하겐다즈", name="케이크")]
    got = hashlib.sha256(_json.dumps([sorted(_tags(p)) for p in sample], ensure_ascii=False).encode()).hexdigest()[:16]
    assert (vocab.RULE, got) == ("vocab-v2", PINNED), f"규칙 결과가 바뀜 — RULE 을 올리고 PINNED 를 {got} 로"
    assert len(vocab.tags_sha256()) == 64


PINNED = "36a8db2a6bc7a379"   # vocab-v2 결과 지문(10-16 — 기기 이름 · 협업 반영)


def test_device_names_and_collab_brands():
    """15 · 18(10-16 사용자 검토): 다른 회사 상품명 속 PS5 · XBOX 는 호환 표시. 26: 협업 'A x B' 는 둘 다 브랜드."""
    assert not any(t == "소니" for _, t, _ in _tags(_p(brand="지티기어", name="로지텍 G29 드라이빙 포스 레이싱휠 PS5 PS4 PC")))
    assert ("브랜드", "로지텍", "name") in _tags(_p(brand="지티기어", name="로지텍 G29 드라이빙 포스 레이싱휠 PS5 PS4 PC"))
    assert not any(t == "엑스박스" for _, t, _ in _tags(_p(brand="커세어", name="커세어 스커프 벨러 PRO XBOX 게임패드")))
    assert ("브랜드", "소니", "field") in _tags(_p(brand="소니플레이스테이션", name="[PS5] 듀얼센스"))
    vocab.set_catalog_brands(["케이스티파이", "마뗑킴", "앱코", "위글위글 테크", "로지텍"])
    try:
        collab = _tags(_p(brand="케이스티파이", name="[빠른배송] 마뗑킴 x 케이스티파이 카드홀더"))
        assert {("브랜드", "케이스티파이", "field"), ("브랜드", "마뗑킴", "name")} <= collab
        assert ("브랜드", "앱코", "name") in _tags(_p(brand="위글위글 테크", name="[앱코X위글위글] 무선 키보드"))
        assert not any(t == "로지텍" and v == "name" for _, t, v in _tags(_p(brand="로지텍", name="로지텍 G502 X PLUS 마우스")))   # 모델명 X
        assert {("브랜드", "반얀트리", "field"), ("브랜드", "하겐다즈", "field")} <= _tags(_p(brand="반얀트리X하겐다즈", name="케이크"))
    finally:
        vocab.set_catalog_brands([])
