import tempfile
import unittest
from pathlib import Path

import numpy as np

from route_semantics.city_server import (
    decode_world,
    encode_world,
    pick_routes,
    projected,
)
from route_semantics.city_store import CityStore, pack


class CityMapTests(unittest.TestCase):
    def test_projection_cache_keeps_parts_and_coordinates(self):
        parts = [[[104, 30], [104.01, 30.01]], [[104.03, 30.02], [104.04, 30.02]]]
        restored = decode_world(encode_world(parts))
        self.assertEqual(len(restored), 2)
        for original, cached in zip(parts, restored):
            np.testing.assert_array_equal(cached, projected(original, 18))
            np.testing.assert_allclose(
                cached * 2 ** (11 - 18), projected(original, 11), atol=1e-9
            )
        self.assertEqual(decode_world(encode_world([])), [])

    def test_pick_matches_line_interior_and_preserves_overlapping_choices(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "map.db"
            store = CityStore(database)
            with store.db:
                for identifier, state, latitude in [
                    (1, "candidate", 30),
                    (2, "review", 30),
                    (3, "failed", 30),
                    (4, "candidate", 30.01),
                ]:
                    geometry = pack([[[103.999, latitude], [104.001, latitude]]])
                    store.db.execute(
                        "INSERT INTO routes(id,state,distance_m,rebuilt,rebuilt_display) VALUES(?,?,?,?,?)",
                        (identifier, state, 200, geometry, geometry),
                    )
                    store.db.execute(
                        "INSERT INTO route_bounds VALUES(?,?,?,?,?)",
                        (identifier, 103.99, 104.01, 29.99, 30.02),
                    )
            x, y = projected([[104, 30]], 18)[0]
            picked = pick_routes(database, x, y, 18)
            self.assertEqual({r["id"] for r in picked["routes"]}, {1, 2})
            self.assertEqual(picked["total_matches"], 2)
            self.assertEqual(
                [
                    r["id"]
                    for r in pick_routes(database, x, y, 18, review=False)["routes"]
                ],
                [1],
            )
            self.assertEqual(pick_routes(database, x, y + 300, 18)["routes"], [])

    def test_pick_rejects_invalid_zoom_and_nonfinite_position(self):
        for x, y, z in [(1, 2, 19), (float("nan"), 2, 10), (1, float("inf"), 10)]:
            with self.assertRaises(ValueError):
                pick_routes("unused", x, y, z)
