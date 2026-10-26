"""Run Cometmode channel proof with isolated Next build-marker shims."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("checkout", type=Path)
parser.add_argument("--scratch-database", required=True, choices=["cometmode_chatbot_cx0_20261022"])
args = parser.parse_args()
checkout = args.checkout.resolve()
script = Path(__file__).with_suffix(".ts").resolve()
with tempfile.TemporaryDirectory(prefix="meta-proof-markers-") as temp:
    for name in ["server-only", "client-only"]:
        package = Path(temp) / name
        package.mkdir()
        (package / "package.json").write_text(json.dumps({"main":"index.js"}))
        (package / "index.js").write_text("module.exports = {};\n")
    env = {**os.environ, "E2E_META_DATABASE_NAME":args.scratch_database,
           "NODE_PATH":temp + (os.pathsep + os.environ["NODE_PATH"] if os.environ.get("NODE_PATH") else "")}
    result = subprocess.run(["bun", "--cwd", str(checkout / "apps/web"), str(script), str(checkout)], env=env)
raise SystemExit(result.returncode)
