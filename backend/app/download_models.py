"""检索所需向量模型与精排模型的下载脚本。"""

import argparse
import json
from pathlib import Path


def main():
    from huggingface_hub import snapshot_download

    parser = argparse.ArgumentParser(description="准备向量与精排模型")
    parser.add_argument("--directory", default=str(Path(__file__).resolve().parents[2] / "models"))
    args = parser.parse_args()
    base = Path(args.directory)
    base.mkdir(parents=True, exist_ok=True)
    report = []
    revisions = {
        "BAAI/bge-small-zh-v1.5": "7999e1d3359715c523056ef9478215996d62a620",
        "BAAI/bge-reranker-base": "2cfc18c9415c912f9d8155881c133215df768a70",
    }
    for model_id, revision in revisions.items():
        path = base / model_id.rsplit("/", 1)[-1]
        snapshot_download(
            model_id,
            revision=revision,
            local_dir=str(path),
            allow_patterns=["*.json", "*.safetensors", "*.txt", "*.model", "*/config.json"],
        )
        report.append(
            {
                "model_id": model_id,
                "directory": str(path),
                "weights_ready": (path / "model.safetensors").exists(),
            }
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
