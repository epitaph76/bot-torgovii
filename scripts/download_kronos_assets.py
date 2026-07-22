from __future__ import annotations

import argparse
from pathlib import Path

from huggingface_hub import snapshot_download


HF_REPOS = {
    "base": {
        "model": "NeoQuasar/Kronos-base",
        "tokenizer": "NeoQuasar/Kronos-Tokenizer-base",
    }
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Download Kronos weights into kronos/weights.")
    parser.add_argument("--model", default="base", choices=sorted(HF_REPOS))
    parser.add_argument("--weights-dir", default="kronos/weights")
    args = parser.parse_args()

    weights_dir = Path(args.weights_dir)
    weights_dir.mkdir(parents=True, exist_ok=True)
    repos = HF_REPOS[args.model]
    for repo_id in (repos["model"], repos["tokenizer"]):
        local_name = repo_id.replace("/", "__")
        target = weights_dir / local_name
        print(f"Downloading {repo_id} -> {target}", flush=True)
        snapshot_download(repo_id=repo_id, local_dir=str(target))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
