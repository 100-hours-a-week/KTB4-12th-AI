"""Read-only source audit; optional exact Compose config with synthetic placeholders."""
import argparse
import ast
import json
import re
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTAINERS = ROOT / "integration/containers"


def source_check():
    paths = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
    ).decode().split("\0")
    paths = sorted({p for p in paths if p and (ROOT / p).is_file()})
    forbidden_parts = {".venv", "node_modules", "models", "secrets", "verification",
                       "reports", "__pycache__", "returned"}
    forbidden_suffixes = {".pem", ".key", ".p12", ".jks", ".db", ".dump", ".log",
                          ".onnx", ".safetensors", ".ckpt", ".pt", ".hash"}
    violations = []
    for name in paths:
        p = Path(name)
        if (forbidden_parts.intersection(p.parts) or p.suffix in forbidden_suffixes
                or (p.name.startswith(".env") and p.name != ".env.example")
                or p.name in {"이름_대조표.md", "openrouter-key.json", "openrouter-chat-key.json", "openrouter-profiler-key.json", "be-shared-service-token.json", "be-service-token.json", "be-chat-token.json", "be-profiler-token.json"}):
            violations.append(name + ": forbidden source path")
            continue
        if p.suffix == ".py":
            ast.parse((ROOT / p).read_text(), filename=name)
        if (ROOT / p).stat().st_size > 2_000_000:
            violations.append(name + ": large artifact requires explicit review")
        raw = (ROOT / p).read_bytes()
        if re.search(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", raw):
            violations.append(name + ": private key marker")
    for name in ["profiling/uv.lock", "chat/uv.lock", "product-search/uv.lock",
                 "integration/uv.lock", "console/uv.lock",
                 "product-search/embedding/package-lock.json"]:
        if not (ROOT / name).is_file():
            violations.append(name + ": missing dependency lock")
    if violations:
        raise ValueError("\n".join(violations))
    return {"sourceFiles": len(paths), "privateOrGeneratedFiles": 0}


def compose_check():
    # config does not start containers, connect to a DB or read provider keys.
    subprocess.run(["docker", "compose", "version"], check=True, cwd=ROOT)
    files = [CONTAINERS / ("compose." + n + ".yaml")
             for n in ("guarded", "sync", "smoke", "admin")]
    required = set(re.findall(r"\$\{([A-Z_]+):\?[^}]+\}",
                              "\n".join(p.read_text() for p in files)))
    with tempfile.TemporaryDirectory(prefix="ai-compose-config-") as tmp:
        root = Path(tmp)
        (root / "placeholder").write_text("synthetic-config-only\n")
        values = {k: str(root / "placeholder") for k in required}
        values.update(
            AI_DATABASE_NAME="emmet4930_config_only",
            CONSOLE_ADMIN_ORIGIN="https://console.example.invalid", CONSOLE_TRUSTED_PROXY_IPS="127.0.0.1",
            EMMET_POSTGRES_VOLUME="ai-rc-config-not-created",
            EMMET_MODEL_DIR=tmp, EMMET_MEDIA_DIR=tmp,
            PROFILING_BACKEND_BASE_URL="http://127.0.0.1:8002",
            BUDGET_GUARDED_ORIGIN="http://host.docker.internal:18942",
            BUDGET_EXPECTED_MAX_ATTEMPTS="6", BUDGET_EXPECTED_MAX_RESERVED_USD="0.2",
            BACKEND_EXPORT_URL="https://example.invalid/products",
            BACKEND_CATEGORIES_URL="https://example.invalid/categories",
        )
        env = root / "compose.env"
        env.write_text("".join(f"{k}={v}\n" for k, v in sorted(values.items())))
        for group in ([files[0]], files[:2], [files[2]], [files[0], files[3]]):
            command = ["docker", "compose", "--env-file", str(env)]
            for path in group:
                command += ["-f", str(path)]
            subprocess.run(command + ["config", "--quiet"], cwd=ROOT, check=True)
    return {"configurations": 4, "containersStarted": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose", action="store_true")
    args = parser.parse_args()
    result = source_check()
    if args.compose:
        result["compose"] = compose_check()
    print(json.dumps(result))
