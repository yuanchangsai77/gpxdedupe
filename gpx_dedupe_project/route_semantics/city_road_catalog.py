"""Stable OSM junction/entrance candidates; proximity never proves a visit."""

import argparse
import json
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

from .city_store import CityStore


def ingest(osm, database):
    store = CityStore(database)
    signature = Path(osm).stat().st_size
    if store.meta("road_catalog_bytes") == signature:
        return
    nodes = {}
    degrees = Counter()
    named = {}
    context = ET.iterparse(osm, events=("start", "end"))
    _, root = next(context)
    for event, e in context:
        if event != "end":
            continue
        if e.tag == "node":
            identifier = e.attrib["id"]
            nodes[identifier] = (float(e.attrib["lon"]), float(e.attrib["lat"]))
            tags = {t.attrib["k"]: t.attrib["v"] for t in e.findall("tag")}
            if (
                tags.get("entrance")
                or tags.get("name")
                or tags.get("highway") in ("crossing", "traffic_signals")
            ):
                named[identifier] = tags
            e.clear()
            root.clear()
        elif e.tag == "way":
            degrees.update(n.attrib["ref"] for n in e.findall("nd"))
            e.clear()
            root.clear()
    total = 0
    with store.db:
        for identifier in set(named) | {i for i, n in degrees.items() if n >= 2}:
            coordinate = nodes.get(identifier)
            if not coordinate:
                continue
            tags = named.get(identifier, {})
            store.put_anchor(
                "osm:node:" + identifier,
                coordinate,
                "entrance" if tags.get("entrance") else "road_junction_candidate",
                tags.get("name"),
                "OpenStreetMap",
                {
                    "tags": tags,
                    "way_incidence": degrees[identifier],
                    "proximity_only": True,
                    "independent_evidence_count": None,
                },
            )
            total += 1
    store.meta("road_catalog_bytes", signature)
    print("ROAD_ANCHORS", total, flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--osm", required=True)
    p.add_argument("--database", required=True)
    a = p.parse_args()
    ingest(a.osm, a.database)


def ingest_pois(source, database):
    data = json.loads(Path(source).read_text())
    store = CityStore(database)
    total = 0
    with store.db:
        for e in data.get("elements", []):
            center = e.get("center", e)
            if "lon" not in center or "lat" not in center:
                continue
            tags = e.get("tags", {})
            store.put_anchor(
                "osm:" + e["type"] + ":" + str(e["id"]),
                [center["lon"], center["lat"]],
                tags.get("leisure")
                or tags.get("natural")
                or tags.get("tourism")
                or "entrance",
                tags.get("name"),
                "OpenStreetMap",
                {
                    "tags": tags,
                    "geometry_representation": "node_or_area_center",
                    "proximity_only": True,
                    "independent_evidence_count": None,
                },
            )
            total += 1
    store.meta(
        "poi_import",
        {"entities": total, "source": "OpenStreetMap", "proximity_only": True},
    )
    print("POI_IMPORTED", total, flush=True)
