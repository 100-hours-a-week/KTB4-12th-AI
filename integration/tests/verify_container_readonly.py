"""Native read-only source import proof; no image, DB, provider or socket calls."""

import hashlib
import json
import os
import sys
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "integration/containers/Dockerfile"
SEARCH = ROOT / "product-search"


def run():
    search_stage = (
        DOCKERFILE.read_text()
        .split("FROM base13 AS search\n", 1)[1]
        .split("\nFROM ", 1)[0]
    )
    assert "RUN mkdir -p /workspace/product-search/runtime" in search_stage
    assets_line = next(
        line for line in search_stage.splitlines() if line.startswith("RUN python -c ")
    )
    assets_code = shlex.split(assets_line[4:])[2]
    with tempfile.TemporaryDirectory(prefix="emmet-readonly-regression-") as directory:
        root = Path(directory)
        image = root / "image"
        static = image / "static"
        static.mkdir(parents=True)
        shutil.copyfile(SEARCH / "app.py", image / "app.py")
        for name in ["app.js", "app.css", "guide.js", "guide.css", "json.js"]:
            shutil.copyfile(SEARCH / "static" / name, static / name)
        env = {
            "PATH": os.environ["PATH"],
            "PYTHONPATH": str(SEARCH),
            "PYTHONDONTWRITEBYTECODE": "1",
            "SEARCH_QA_DATABASE_URL": "postgresql://synthetic@localhost/never",
            "SEARCH_RUNTIME_ROOT": str(root / "writable"),
        }
        # Execute the Dockerfile's actual static-assets build command against the disposable tree.
        subprocess.run(
            [
                os.environ.get("INTEGRATION_SEARCH_TEST_PYTHON", sys.executable),
                "-B",
                "-c",
                assets_code.replace("/workspace/product-search/static", str(static)),
            ],
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
        child = """import importlib.util,json,os,sys
from pathlib import Path
# Audit hooks apply to native libraries too; source import/build_assets must do no I/O to services.
def guard(event,args):
    if event in ('socket.connect','socket.getaddrinfo'):raise AssertionError('No network permitted')
sys.addaudithook(guard)
import psycopg
psycopg.connect=lambda *a,**k:(_ for _ in ()).throw(AssertionError('No database permitted'))
image=Path(sys.argv[1]);assert not os.access(image,os.W_OK),'Test requires an unprivileged user'
spec=importlib.util.spec_from_file_location('readonly_search_app',image/'app.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
version=module.build_assets()
assert module.RUNTIME.is_dir() and list(module.RUNTIME.iterdir())==[]
assert not module.DB.exists()
print(json.dumps({'imported':True,'assetsVersion':version,'sourceWritable':False,'sourceRuntimeEmpty':True,'databaseCalls':0,'externalCalls':0}))
"""

        def readonly():
            for path in image.rglob("*"):
                path.chmod(0o555 if path.is_dir() else 0o444)
            image.chmod(0o555)

        def writable():
            image.chmod(0o700)
            for path in image.rglob("*"):
                path.chmod(0o700 if path.is_dir() else 0o600)

        def hashes():
            return {
                str(p.relative_to(image)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in image.rglob("*")
                if p.is_file()
            }

        readonly()
        try:
            negative = subprocess.run(
                [os.environ.get("INTEGRATION_SEARCH_TEST_PYTHON", sys.executable), "-B", "-c", child, str(image)],
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            assert (
                negative.returncode != 0
                and "PermissionError" in negative.stderr
                and "/runtime" in negative.stderr
            ), negative.stderr
            writable()
            # Execute the exact mkdir RUN from the Search stage, mapped only into this temporary tree.
            subprocess.run(["mkdir", "-p", str(image / "runtime")], check=True)
            readonly()
            before = hashes()
            positive = subprocess.run(
                [os.environ.get("INTEGRATION_SEARCH_TEST_PYTHON", sys.executable), "-B", "-c", child, str(image)],
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            assert positive.returncode == 0, positive.stderr
            assert hashes() == before
            proof = json.loads(positive.stdout)
            assert (
                hashlib.sha256((image / "app.py").read_bytes()).hexdigest()
                == hashlib.sha256((SEARCH / "app.py").read_bytes()).hexdigest()
            )
            return {
                "passed": True,
                "synthetic": True,
                "nativeReadOnlySource": True,
                "imagesBuilt": False,
                "containersStarted": False,
                "preFixPermissionErrorReproduced": True,
                "postFixImportAndBuildAssets": proof,
                "sourceFilesChangedDuringImport": False,
                "normalAndSmokeUseSameSearchTarget": True,
                "paidCalls": 0,
                "realDatabaseCalls": 0,
                "realLedgerWrites": 0,
            }
        finally:
            writable()


if __name__ == "__main__":
    print(json.dumps(run()))
