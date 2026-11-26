"""Aggregate aligned existing fine cells; no GPS reread or routing needed."""

import argparse
import math
import sqlite3


def rollup(path, source_size=100, target_size=5000):
    if source_size <= 0 or target_size <= source_size or target_size % source_size:
        raise ValueError("target must be an integer multiple of the finer scale")
    db = sqlite3.connect(path)
    db.create_function("floor", 1, math.floor)
    try:
        if not db.execute(
            "SELECT 1 FROM contributions WHERE size=? LIMIT 1", (source_size,)
        ).fetchone():
            raise ValueError("source scale missing")
        factor = target_size // source_size
        with db:
            db.execute("DELETE FROM contributions WHERE size=?", (target_size,))
            db.execute(
                """INSERT INTO contributions SELECT route_id,?,floor(x*1.0/?),floor(y*1.0/?),sum(length_m)
                          FROM contributions WHERE size=? GROUP BY route_id,floor(x*1.0/?),floor(y*1.0/?)""",
                (target_size, factor, factor, source_size, factor, factor),
            )
            db.execute("DELETE FROM cells WHERE size=?", (target_size,))
            db.execute(
                "INSERT INTO cells SELECT size,x,y,count(*),sum(length_m) FROM contributions WHERE size=? GROUP BY size,x,y",
                (target_size,),
            )
        return db.execute(
            "SELECT count(*) FROM cells WHERE size=?", (target_size,)
        ).fetchone()[0]
    finally:
        db.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--coverage", required=True)
    a = p.parse_args()
    print("5km cells:", rollup(a.coverage))
