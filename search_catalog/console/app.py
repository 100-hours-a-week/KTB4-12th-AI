import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import Optional

from search_catalog.search.service import SearchService
from search_catalog.search.types import SearchFilters, SearchRequest

app = FastAPI(title="선잘알 AI 검색 엔진 테스트 콘솔")
search_service = SearchService()


class ConsoleSearchQuery(BaseModel):
    query: str
    min_price: Optional[int] = None
    max_price: Optional[int] = None
    limit: int = 5


@app.post("/api/console/search")
async def console_search(data: ConsoleSearchQuery):
    req = SearchRequest(
        query=data.query,
        filters=SearchFilters(
            min_price=data.min_price,
            max_price=data.max_price,
        ),
        limit=data.limit,
    )
    res = await search_service.search(req)
    return {
        "total": res.total_count,
        "hits": [
            {
                "id": h.evidence.product_id,
                "name": h.evidence.name,
                "price": h.evidence.price,
                "brand": h.evidence.brand,
                "category": h.evidence.category_name,
                "desc": h.evidence.description,
                "score": round(h.score, 4),
            }
            for h in res.hits
        ],
    }


@app.get("/", response_class=HTMLResponse)
async def console_ui():
    return """
<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>선잘알 AI 검색기 테스트 콘솔</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-50 text-gray-800">
    <div class="max-w-4xl mx-auto py-10 px-4">
        <header class="mb-8 border-b pb-4">
            <h1 class="text-3xl font-bold text-indigo-600">🎁 선잘알 AI 검색 엔진 테스트 콘솔</h1>
            <p class="text-gray-500 mt-1">pgvector 임베딩 및 조건 필터링 튜닝용 독립 웹 콘솔입니다.</p>
        </header>

        <div class="bg-white p-6 rounded-xl shadow-sm border mb-8">
            <div class="grid grid-cols-1 md:grid-cols-3 gap-4 mb-4">
                <div class="md:col-span-2">
                    <label class="block text-sm font-semibold mb-1">자연어 검색어</label>
                    <input id="query" type="text" placeholder="예: 30대 집들이 선물, 실용적인 머그컵" 
                           class="w-full border rounded-lg px-4 py-2 focus:ring-2 focus:ring-indigo-500 outline-none"
                           value="실용적인 선물">
                </div>
                <div>
                    <label class="block text-sm font-semibold mb-1">최대 개수 (Limit)</label>
                    <input id="limit" type="number" value="5" min="1" max="30"
                           class="w-full border rounded-lg px-4 py-2 focus:ring-2 focus:ring-indigo-500 outline-none">
                </div>
            </div>

            <div class="grid grid-cols-2 gap-4 mb-6">
                <div>
                    <label class="block text-sm font-semibold mb-1">최소 예산 (원)</label>
                    <input id="min_price" type="number" placeholder="예: 10000"
                           class="w-full border rounded-lg px-4 py-2 focus:ring-2 focus:ring-indigo-500 outline-none">
                </div>
                <div>
                    <label class="block text-sm font-semibold mb-1">최대 예산 (원)</label>
                    <input id="max_price" type="number" placeholder="예: 50000"
                           class="w-full border rounded-lg px-4 py-2 focus:ring-2 focus:ring-indigo-500 outline-none">
                </div>
            </div>

            <button onclick="doSearch()" 
                    class="w-full bg-indigo-600 hover:bg-indigo-700 text-white font-semibold py-3 rounded-lg transition">
                검색 실행
            </button>
        </div>

        <div>
            <h2 class="text-xl font-bold mb-4">검색 결과 (<span id="resultCount">0</span>건)</h2>
            <div id="results" class="space-y-4">
                <div class="text-center text-gray-400 py-10 bg-white rounded-lg border">
                    검색어를 입력하고 검색 버튼을 눌러주세요.
                </div>
            </div>
        </div>
    </div>

    <script>
        async function doSearch() {
            const query = document.getElementById('query').value;
            const limit = parseInt(document.getElementById('limit').value) || 5;
            const min_price = document.getElementById('min_price').value ? parseInt(document.getElementById('min_price').value) : null;
            const max_price = document.getElementById('max_price').value ? parseInt(document.getElementById('max_price').value) : null;

            const resDiv = document.getElementById('results');
            resDiv.innerHTML = '<div class="text-center py-6 text-indigo-600">검색 중...</div>';

            try {
                const response = await fetch('/api/console/search', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({query, limit, min_price, max_price})
                });
                const data = await response.json();
                document.getElementById('resultCount').innerText = data.total;

                if (data.hits.length === 0) {
                    resDiv.innerHTML = '<div class="text-center py-8 text-gray-400 bg-white border rounded">조건에 맞는 검색 결과가 없습니다.</div>';
                    return;
                }

                resDiv.innerHTML = data.hits.map(h => `
                    <div class="bg-white p-5 rounded-lg border shadow-sm flex justify-between items-start">
                        <div>
                            <div class="flex items-center gap-2 mb-1">
                                <span class="bg-indigo-100 text-indigo-700 text-xs px-2 py-0.5 rounded font-semibold">${h.category}</span>
                                <span class="text-xs text-gray-400 font-mono">ID: ${h.id}</span>
                                ${h.brand ? `<span class="text-xs text-gray-500 font-medium">| ${h.brand}</span>` : ''}
                            </div>
                            <h3 class="text-lg font-bold text-gray-900">${h.name}</h3>
                            <p class="text-sm text-gray-600 mt-1">${h.desc}</p>
                            <p class="text-indigo-600 font-bold mt-2">${h.price.toLocaleString()}원</p>
                        </div>
                        <div class="text-right">
                            <span class="bg-emerald-50 text-emerald-700 text-xs px-2.5 py-1 rounded-full font-mono font-bold">
                                score: ${h.score}
                            </span>
                        </div>
                    </div>
                `).join('');
            } catch (err) {
                resDiv.innerHTML = `<div class="text-center py-6 text-red-500">오류 발생: ${err.message}</div>`;
            }
        }
    </script>
</body>
</html>
    """


if __name__ == "__main__":
    print("🚀 선잘알 검색기 웹 콘솔 시작: http://localhost:8501")
    uvicorn.run("search_catalog.console.app:app", host="0.0.0.0", port=8501, reload=True)
