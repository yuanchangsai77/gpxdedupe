"""Export a standalone viewer of the final GeoJSON, without changing selection."""

import argparse
import html
import json
from pathlib import Path


def export(geojson, output, region="成都"):
    source, destination = Path(geojson), Path(output)
    if source.resolve() == destination.resolve():
        raise ValueError("viewer must differ from input")
    template = Path(__file__).with_name("final_view.html").read_text()
    gcj = next(
        line
        for line in Path(__file__).with_name("city_view.html").read_text().splitlines()
        if line.startswith("function gcj(")
    )
    template = template.replace("__REGION__", html.escape(region)).replace(
        "__GCJ__", gcj
    )
    before, after = template.split("__DATA__")
    temporary = destination.with_suffix(".next.html")
    with source.open(encoding="utf8") as stream:
        data = json.load(stream)
        if data.get("type") != "FeatureCollection":
            raise ValueError("expected final FeatureCollection")
        ids = set()
        for feature in data["features"]:
            props, geometry = feature["properties"], feature["geometry"]
            rid = props["route_id"]
            if rid in ids or props["geometry_kind"] not in ("original", "rebuilt"):
                raise ValueError("duplicate route or invalid expression")
            if geometry["type"] != "MultiLineString":
                raise ValueError("expected preserved route parts")
            ids.add(rid)
        count = len(ids)
        del data
        stream.seek(0)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("w", encoding="utf8") as out:
            out.write(before)
            while chunk := stream.read(1024 * 1024):
                out.write(chunk.replace("<", "\\u003c"))
            out.write(after)
    temporary.replace(destination)
    return {"routes": count, "output": str(destination)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--geojson", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--region", default="成都")
    args = parser.parse_args()
    print(
        json.dumps(export(args.geojson, args.output, args.region), ensure_ascii=False)
    )
