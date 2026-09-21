# 상품 데이터 적재

정제한 상품 JSON을 PostgreSQL에 넣는 코드입니다.
우선 상품 정보 6개 필드만 저장합니다. 임베딩과 검색은 다음에 붙일 예정입니다.

PostgreSQL에 `ai_chat` DB를 만든 뒤 이 폴더에서 실행합니다.

```sh
python -m pip install -r requirements.txt
export DATABASE_URL='postgresql://localhost/ai_chat'
python load_catalog.py /path/to/products.json
```

입력은 `{"products": [...]}` 형태의 정제본입니다. 데이터 파일은 따로 준비해야 합니다.
각 상품의 `product_id`, `name`, `brand`, `category_id`, `price_krw`, `description`을 읽습니다.
상품 ID와 분류 ID는 수집본의 문자열 값을 그대로 사용합니다.
같은 상품 ID로 다시 실행하면 값을 갱신하고, 입력에 없는 기존 상품은 그대로 둡니다.
