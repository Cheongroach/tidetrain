"""Render the friendly edition's validation figure from checked-in evidence."""
import json
import math
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


def main():
    repo = Path(__file__).resolve().parents[2]
    folder = repo / "results/qwen05b-4round"
    records = [json.loads((folder / f"validation-r{round_}-full-gpu.json").read_text())
               for round_ in (0, 4)]
    metrics = [row["metrics"] for row in records]
    for row in metrics:
        assert row["full_validation"] and row["precision"] == "fp32"
        assert row["validation_tokens"] == 262209 and row["validation_windows"] == 1025
        assert math.isclose(math.exp(row["validation_loss"]), row["perplexity"], rel_tol=1e-12)
    assert records[0]["data_manifest_sha256"] == records[1]["data_manifest_sha256"]
    values = [row["perplexity"] for row in metrics]
    change = (values[1] / values[0] - 1) * 100
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 11,
                         "axes.spines.top": False, "axes.spines.right": False,
                         "svg.fonttype": "none"})
    fig, ax = plt.subplots(figsize=(7.5, 5.3))
    fig.subplots_adjust(left=.13, right=.98, top=.86, bottom=.28)
    ax.bar([0, 1], values, width=.5, color=["#48799D", "#B7654A"])
    ax.set_xticks([0, 1], ["Initial checkpoint\nround 0", "After 524,288 accepted tokens\nround 4"])
    ax.set_ylabel("Full-validation perplexity (lower is better)")
    ax.set_ylim(0, 36)
    ax.yaxis.grid(True, alpha=.2)
    ax.set_axisbelow(True)
    for x, y in enumerate(values):
        ax.text(x, y + .7, f"{y:.3f}", ha="center")
    ax.set_title(f"Qwen2.5-0.5B: perplexity increased by {change:.2f}%", loc="left", pad=15)
    fig.text(.02, .035,
             "Same validation stream · 262,209 target tokens · FP32 on A10\n"
             "One training run; no multi-seed interval or DDP comparison.",
             fontsize=9, color="#444")
    output = repo / "docs/friendly"
    for suffix in ("png", "svg"):
        fig.savefig(output / f"validation-quality-friendly.{suffix}", dpi=180, bbox_inches="tight")
    svg = output / "validation-quality-friendly.svg"
    svg.write_text("\n".join(line.rstrip() for line in svg.read_text().splitlines()) + "\n")
    plt.close(fig)
    provenance = {"insight": "Validation quality worsened in this observed run.",
                  "source_files": ["results/qwen05b-4round/validation-r0-full-gpu.json",
                                   "results/qwen05b-4round/validation-r4-full-gpu.json"],
                  "perplexity": values, "relative_change_percent": change,
                  "target_tokens": 262209, "windows": 1025,
                  "training_runs": 1, "error_bars": "None; multi-seed variability not measured."}
    (output / "validation-quality-friendly.json").write_text(json.dumps(provenance, indent=2) + "\n")
    print(json.dumps(provenance))


if __name__ == "__main__":
    main()
