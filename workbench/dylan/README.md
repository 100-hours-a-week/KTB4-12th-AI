# 상품 데이터 적재

정제한 상품 JSON을 PostgreSQL에 넣는 코드입니다.
우선 상품 정보 6개 필드만 저장합니다. 임베딩과 검색은 다음에 붙일 예정입니다.

PostgreSQL에 `ai_chat` DB를 만든 뒤 이 폴더에서 실행합니다.

```sh
python -m pip install -r requirements.txt
export DATABASE_URL='postgresql://localhost/ai_chat'
python load_catalog.py sample_products.json
```

샘플은 실행 확인용 가상 상품 2건입니다. 실제 상품 데이터는 포함하지 않았습니다.
입력은 `{"products": [...]}` 형태이며, 다른 파일을 넣으려면 실행할 때 경로를 바꾸면 됩니다.
각 상품의 `product_id`, `name`, `brand`, `category_id`, `price_krw`, `description`을 읽습니다.
상품 ID와 분류 ID는 입력 파일의 문자열 값을 그대로 사용합니다.
같은 상품 ID로 다시 실행하면 값을 갱신하고, 입력에 없는 기존 상품은 그대로 둡니다.

저장된 샘플은 아래 명령으로 확인할 수 있습니다. 두 번 실행해도 샘플은 2건입니다.

```sh
psql "$DATABASE_URL" -c "SELECT * FROM ai_search.products WHERE product_id IN ('SAMPLE:001', 'SAMPLE:002') ORDER BY product_id;"
```
