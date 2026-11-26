"""Create a source-only, allowlisted algorithm ZIP with per-file checksums."""

import argparse
import hashlib
import json
import zipfile
from pathlib import Path

VERSION = "route-density-delivery-1"
MODULE_FILES = [
    "COVERAGE.md",
    "DEDUP.md",
    "DELIVERY.md",
    "DELIVERY_VALIDATION.md",
    "README.md",
    "WORKFLOW.md",
    "__init__.py",
    "adapters.py",
    "build_gallery.py",
    "city-route.schema.json",
    "city_finalize.py",
    "city_limited.py",
    "city_pipeline.py",
    "city_rebuild.py",
    "city_road_catalog.py",
    "city_server.py",
    "city_store.py",
    "city_view.html",
    "comparison.template.html",
    "coverage.py",
    "coverage_fast.py",
    "coverage_rollup.py",
    "coverage_selection.py",
    "coverage_view.html",
    "coverage_view.py",
    "delivery.py",
    "density_batch.py",
    "density_policy.js",
    "density_refresh.cjs",
    "endpoint_recovery.py",
    "examples/synthetic.ndjson",
    "failure_diagnosis.py",
    "fast_frechet.c",
    "fast_geometry.py",
    "geometry.py",
    "graphhopper-city.yml",
    "package_delivery.py",
    "rebuilt_dedup.js",
    "rebuilt_dedup.py",
    "requirements.lock.txt",
    "requirements.txt",
    "results/chengdu-density-delivery.json",
    "route_motion.js",
    "sparse_routing.py",
    "方案介绍.md",
]

TESTS = [
    "test_city_pipeline.py",
    "test_city_map.py",
    "test_sparse_routing.py",
    "test_endpoint_recovery.py",
    "test_failure_diagnosis.py",
    "test_coverage.py",
    "test_coverage_selection.py",
    "test_coverage_fast.py",
    "test_rebuilt_dedup.py",
    "test_delivery.py",
    "test_package_delivery.py",
    "test_density_policy.cjs",
    "test_rebuilt_dedup.cjs",
    "test_route_motion.cjs",
    "browser_delivery.cjs",
]
README = """# 历史路线密度筛选算法包

先阅读 [算法包交付与运行说明](route_semantics/DELIVERY.md)。
对外讲解使用 [方案介绍](route_semantics/方案介绍.md)。

包含源码、14条明确标记的合成样例、测试和版本清单；不含真实轨迹、数据库、路网图或引擎运行时。
运行环境为Linux/WSL，详细依赖及新重建的GraphHopper准备条件见运行说明。

快速验证：

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.lock.txt
python -m route_semantics.city_pipeline ingest --source route_semantics/examples/synthetic.ndjson --database work/demo.sqlite3
python -m route_semantics.delivery --database work/demo.sqlite3 --output-dir work/demo-result --region 合成样例
```

预期14条输入、3条原始入选（含1条空格补充），不涉及真实道路重建验证。
输出清单、CSV、完整GeoJSON、报告及HTML均在work/demo-result。
manifest.json记录算法版本、文件列表和SHA256；不同数据与路网可能产生不同结果。
"""


def build(output):
    root = Path(__file__).resolve().parent.parent
    output = Path(output)
    if output.suffix != ".zip":
        raise ValueError("delivery output must end in .zip")

    def read_checked(relative):
        path = root / relative
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError("refuse symlinks or out-of-root files in delivery")
        return path.read_bytes()

    payload = {
        "route_semantics/" + name: read_checked("route_semantics/" + name)
        for name in MODULE_FILES
    }
    for name in TESTS:
        payload["tests/" + name] = read_checked("tests/" + name)
    payload["requirements.txt"] = read_checked("route_semantics/requirements.txt")
    payload["requirements.lock.txt"] = read_checked(
        "route_semantics/requirements.lock.txt"
    )
    payload["README.md"] = README.encode()
    payload["VERSION.txt"] = (VERSION + "\n").encode()
    manifest = {
        "version": VERSION,
        "source_only": True,
        "real_traces_included": False,
        "files": {
            name: {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for name, data in sorted(payload.items())
        },
    }
    payload["manifest.json"] = json.dumps(
        manifest, ensure_ascii=False, indent=2
    ).encode()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".next.zip")
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(payload.items()):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
    with zipfile.ZipFile(temporary) as archive:
        if archive.testzip():
            raise ValueError("ZIP integrity failure")
    temporary.replace(output)
    checksum = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(".zip.sha256").write_text(checksum + "  " + output.name + "\n")
    return {
        "version": VERSION,
        "files": len(payload),
        "bytes": output.stat().st_size,
        "sha256": checksum,
        "output": str(output),
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", required=True)
    a = p.parse_args()
    print(json.dumps(build(a.output), ensure_ascii=False))
