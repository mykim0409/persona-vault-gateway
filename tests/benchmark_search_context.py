"""Compare stored knowledge layouts and one-hop context without a private Vault or network."""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
import tracemalloc
import types
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gateway import core
from gateway.app import search_response
from benchmark_search_evidence import seed_case
from test_rag_benchmark import AGENT, no_network


CASES = [
    {"id": "self-contained", "query": "deploy rollback", "fact": "Only on Linux after stopping the writer",
     "visible": True, "notes": [("policy", "deploy rollback uses the saved build. Only on Linux after stopping the writer.", {})]},
    {"id": "unlinked-condition", "query": "deploy rollback", "fact": "Only on Linux after stopping the writer",
     "visible": False, "notes": [("policy", "## Procedure\n\ndeploy rollback uses the saved build.\n\n## Limits\n\nOnly on Linux after stopping the writer.", {})]},
    {"id": "linked-local-condition", "query": "deploy rollback", "fact": "Only on Linux after stopping the writer",
     "visible": True, "notes": [("policy", "## Procedure\n\ndeploy rollback uses the saved build; see [limits](#limits).\n\n## Limits\n\nOnly on Linux after stopping the writer.", {})]},
    {"id": "linked-document-condition", "query": "deploy rollback", "fact": "Only on Linux after stopping the writer",
     "visible": True, "notes": [("policy", "deploy rollback uses the saved build; see [limits](details.md#limits).", {}),
                                ("details", "## Unrelated\n\nDo not return this unrelated noise.\n\n## Limits\n\nOnly on Linux after stopping the writer.", {})]},
    {"id": "korean-condition", "query": "배포 롤백", "fact": "검증은 Linux에서만 수행했으며 Windows는 미확인",
     "visible": True, "notes": [("policy", "배포 롤백은 저장된 빌드를 사용한다. [검증 범위](details.md#검증-범위)를 먼저 확인한다.", {}),
                                ("details", "## 검증 범위\n\n검증은 Linux에서만 수행했으며 Windows는 미확인이다.", {})]},
    {"id": "ambiguous-section", "query": "deploy rollback", "fact": "Only on Linux after stopping the writer",
     "visible": False, "notes": [("policy", "deploy rollback uses the saved build; see [limits](details.md#limits).", {}),
                                 ("details", "## Limits\n\nAn unrelated restriction.\n\n## Limits\n\nOnly on Linux after stopping the writer.", {})]},
    {"id": "whole-file-preview", "query": "deploy rollback", "fact": "Only on Linux after stopping the writer",
     "visible": False, "notes": [("policy", "deploy rollback uses the saved build; see [details](details.md).", {}),
                                 ("details", "## Intro\n\nGeneral background.\n\nMore background.\n\n## Limits\n\nOnly on Linux after stopping the writer.", {})]},
]


def baseline_module(ref):
    module = types.ModuleType("gateway._context_baseline")
    module.__package__ = "gateway"
    sys.modules[module.__name__] = module
    source = subprocess.check_output(["git", "show", f"{ref}:gateway/core.py"], text=True)
    exec(compile(source, f"{ref}:gateway/core.py", "exec"), module.__dict__)
    return module


def run(module, repeats):
    with TemporaryDirectory() as temporary, no_network():
        root = Path(temporary)
        results = []
        for case in CASES:
            settings = module.Settings(root / case["id"], root / "db", "bench", embedding_provider="none")
            seed_case(settings, case)
            result = module.search_vault(settings, AGENT, case["query"], 4, bundle="current")
            public = search_response(result, "current")
            results.append({
                "case": case["id"], "fact_visible": case["fact"] in public["context"],
                "answer_state": public["answer_state"]["state"],
                "context_chars": len(public["context"]),
                "response_bytes": len(json.dumps(public, ensure_ascii=False, separators=(",", ":")).encode()),
                "unrelated_noise": "Do not return this unrelated noise" in public["context"],
            })
        settings = module.Settings(root / "scale", root / "db", "bench", embedding_provider="none")
        for index in range(200):
            body = (f"## Procedure\n\nProject{index} deploy rollback uses the saved build; see [limits](#limits).\n\n"
                    + "\n\n".join(f"## Background {n}\n\n" + "Unrelated maintenance details. " * 45 for n in range(20))
                    + "\n\n## Limits\n\nOnly on Linux after stopping the writer.")
            seed_case(settings, {"notes": [(f"policy-{index}", body, {})]})
        samples = []
        cpu_samples = []
        for iteration in range(repeats + 2):
            start = time.perf_counter()
            cpu_start = time.process_time()
            module.search_vault(settings, AGENT, "Project42 deploy rollback", 4, bundle="current")
            if iteration >= 2:
                samples.append(1000 * (time.perf_counter() - start))
                cpu_samples.append(1000 * (time.process_time() - cpu_start))
        tracemalloc.start()
        module.search_vault(settings, AGENT, "Project42 deploy rollback", 4, bundle="current")
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        return {"cases": results, "fact_coverage": f"{sum(r['fact_visible'] for r in results)}/{len(results)}",
                "scale": {"documents": 200, "bytes": sum(p.stat().st_size for p in settings.vault_dir.rglob('*.md')),
                          "repeats": repeats, "median_ms": round(statistics.median(samples), 2),
                          "max_ms": round(max(samples), 2), "median_cpu_ms": round(statistics.median(cpu_samples), 2),
                          "python_peak_alloc_mib": round(peak / 1024**2, 2)}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref", default="HEAD", help="Git revision of the pre-change core.py")
    parser.add_argument("--repeats", type=int, default=10)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    print(json.dumps({"baseline_ref": args.baseline_ref, "baseline": run(baseline_module(args.baseline_ref), args.repeats),
                      "linked_context": run(core, args.repeats), "measurement": "Offline keyword search; bytes are not billed tokens."},
                     ensure_ascii=False, indent=2))
