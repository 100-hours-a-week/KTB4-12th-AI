"""Precompute immutable AVIF + WebP images. Original files are read-only."""
from concurrent.futures import ProcessPoolExecutor, as_completed
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import time
from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "static/images"


def convert(item):
    paths = [(OUTPUT / f"{item['key']}-384.avif", 384, "AVIF"),
             (OUTPUT / f"{item['key']}-768.avif", 768, "AVIF"),
             (OUTPUT / f"{item['key']}-384.webp", 384, "WEBP")]
    if not all(p.exists() for p, _, _ in paths):
        with Image.open(item["path"]) as opened:
            opened.seek(0)
            original = ImageOps.exif_transpose(opened).convert("RGBA")
            background = Image.new("RGBA", original.size, "white")
            background.alpha_composite(original)
            original = background.convert("RGB")
            for path, width, fmt in paths:
                if path.exists():
                    continue
                im = original.copy()
                im.thumbnail((width, width), Image.Resampling.LANCZOS)
                temp = path.with_suffix(path.suffix + ".tmp")
                if fmt == "AVIF":
                    im.save(temp, format=fmt, quality=54, speed=8, max_threads=1)
                else:
                    im.save(temp, format=fmt, quality=78, method=3)
                os.replace(temp, path)
    return {"key": item["key"], "avif384": paths[0][0].stat().st_size, "avif768": paths[1][0].stat().st_size, "webp384": paths[2][0].stat().st_size}


if __name__ == "__main__":
    OUTPUT.mkdir(parents=True, exist_ok=True)
    items = json.loads((ROOT / "runtime/image-sources.json").read_text())
    unique = list({x['key']: x for x in items}.values())
    start, rows = time.perf_counter(), []
    with ProcessPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(convert, item) for item in unique]
        for f in as_completed(futures):
            rows.append(f.result())
            if len(rows) % 300 == 0:
                print(f"images {len(rows)}/{len(unique)} · {time.perf_counter()-start:.1f}s", flush=True)
    sizes = sorted(r['avif384'] for r in rows)
    result = {"products": len(items), "unique_images": len(rows), "seconds": round(time.perf_counter()-start, 2),
              "original_bytes": sum(x['original_bytes'] for x in items),
              "avif384_bytes": sum(sizes), "avif384_median": sizes[len(sizes)//2],
              "avif384_p95": sizes[int(len(sizes)*.95)], "avif768_bytes": sum(x['avif768'] for x in rows),
              "webp384_bytes": sum(x['webp384'] for x in rows)}
    (ROOT / "artifacts/images.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result), flush=True)
