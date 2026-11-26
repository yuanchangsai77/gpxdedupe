"""One CPU and 1 GiB address-space cap for the city batch worker."""

import argparse
import os
import resource

os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["OMP_NUM_THREADS"] = "1"
os.sched_setaffinity(0, {min(os.sched_getaffinity(0))})
resource.setrlimit(resource.RLIMIT_AS, (1024**3, 1024**3))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["catalog", "run"])
    p.add_argument("--database", required=True)
    p.add_argument("--osm")
    p.add_argument("--url", default="http://127.0.0.1:18989")
    p.add_argument("--graph-version")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--workers", type=int, choices=[1, 2], default=2)
    a = p.parse_args()
    if a.action == "catalog":
        from .city_road_catalog import ingest

        ingest(a.osm, a.database)
    else:
        from .fast_geometry import available

        if not available():
            raise RuntimeError("build_native_kernel_before_full_city_run")
        from .city_pipeline import reconstruct

        try:
            reconstruct(
                a.database, a.url, a.graph_version, workers=a.workers, limit=a.limit
            )
        except BlockingIOError:
            print(
                "CITY_RUN_ALREADY_ACTIVE: existing worker retains exclusive ownership",
                flush=True,
            )
            return
        if not a.limit:
            from .city_finalize import finalize

            finalize(a.database)


if __name__ == "__main__":
    main()
