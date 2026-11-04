"""Laya 문장 분류기 — 비선호 후보(문장 + 태그)를 5갈래로: 본인 비선호 · 남 얘기 · 습관·사실 · 이미 있음 · 그 외.
Modal v3 컨테이너 안에서 vLLM 과 같은 GPU 에 올린다(core.Core 가 부름, AI 서버는 torch 를 안 가짐).

모델 = 인코더(HF 폴더) 평균 + 선형 5갈래(실험 laya_sent 보강 판). 폴더 꼴(LAYA_DIR):
    encoder/            HF 인코더(config.json · model.safetensors)
    head.safetensors    선형 층(out.weight · out.bias)
    tokenizer/          토크나이저
torch · transformers 는 **v3 를 켤 때만** 필요하다 — 불러올 때가 아니라 load() 에서 가져온다(v2 이미지에 안 넣어도 됨).
device="cuda" 가 기본(실험 판정이 쓴 값). "cpu" 도 된다 — CPU 전용 torch 로 상주 약 2.5GB, GPU 판과 확률 최대 차 0.012 · 이름표 17/17 같음(11-03).
"""
import threading
from pathlib import Path

LABELS = ["본인 비선호", "남 얘기", "습관·사실", "이미 있음", "그 외"]
NEG = frozenset({"남 얘기", "습관·사실"})       # 거부 이름표 — 이것이 가장 높으면 비선호 태그를 버린다
MAX_LEN = 192


def argmax_label(v: list[float]) -> str:
    return LABELS[max(range(len(v)), key=lambda i: v[i])]


class Laya:
    def __init__(self, laya_dir: Path | str, *, device: str = "cuda", threads: int = 2) -> None:
        self.dir = Path(laya_dir)
        self.device, self.threads = device, threads
        self._model = self._tok = None
        self._lock = threading.Lock()

    def load(self) -> None:
        """올리기(기동 때 한 번) — 실패하면 예외(부르는 쪽이 v3 를 끄고 v2 로)."""
        import torch
        from safetensors.torch import load_file
        from transformers import AutoModel, AutoTokenizer
        if self.device == "cpu":
            torch.set_num_threads(self.threads)
        enc = AutoModel.from_pretrained(str(self.dir / "encoder"), attn_implementation="sdpa", dtype=torch.float32)

        class SentClf(torch.nn.Module):
            def __init__(self) -> None:
                super().__init__()
                self.enc, self.drop, self.out = enc, torch.nn.Dropout(0.1), torch.nn.Linear(enc.config.hidden_size, len(LABELS))

            def forward(self, ids, mask):
                h = self.enc(input_ids=ids, attention_mask=mask).last_hidden_state
                m = mask.unsqueeze(-1).to(h.dtype)
                return self.out(self.drop((h * m).sum(1) / m.sum(1).clamp_min(1)))

        m = SentClf()
        m.enc.config.reference_compile = False
        m.out.load_state_dict({k[4:]: v for k, v in load_file(str(self.dir / "head.safetensors")).items()}, strict=True)
        self._model = m.to(self.device).eval()
        self._tok = AutoTokenizer.from_pretrained(str(self.dir / "tokenizer"))
        self.predict(["문장: 예열\n태그: 예열"])           # 첫 호출 예열 — 첫 요청이 늦지 않게

    @property
    def ready(self) -> bool:
        return self._model is not None

    def predict(self, xs: list[str]) -> list[tuple[list[float], list[float]]]:
        """사람 한 명의 후보 묶음 → [(확률, 로그 비율)] (laya_ctx_eval.predict(lo=True) 와 같은 셈)."""
        import torch
        import torch.nn.functional as F
        if self._model is None:
            raise RuntimeError("Laya 가 올라가지 않음")
        with self._lock, torch.no_grad():       # 한 프로세스 안 여러 요청 — 한 번에 하나
            b = self._tok(xs, padding=True, truncation=True, max_length=MAX_LEN, return_tensors="pt")
            z = self._model(b["input_ids"].to(self.device), b["attention_mask"].to(self.device)).float().double().cpu()
        P = F.softmax(z, -1)
        LO = torch.stack([z[:, k] - torch.logsumexp(torch.cat([z[:, :k], z[:, k + 1:]], 1), 1) for k in range(z.shape[1])], 1)
        return list(zip(P.tolist(), LO.tolist()))
