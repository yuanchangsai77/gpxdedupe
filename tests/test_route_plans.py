"""Only tests for the opt-in plan layer; no service or legacy-suite startup."""
import copy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from pyproj import Transformer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gpx_dedupe_project"))
import route_dedupe as rd
import route_plans as plans


class RoutePlanTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "source.gpx"
        self.output = self.root / "export"

    def source(self, points):
        xy = np.asarray(points, dtype=float) + [500000, 4000000]
        inv = Transformer.from_crs("EPSG:32632", "EPSG:4326", always_xy=True)
        coords = np.column_stack(inv.transform(xy[:,0], xy[:,1]))
        rd.write_gpx(self.path, coords, np.full(len(coords), 100.), "Synthetic plan")
        return self.path

    def lap(self):
        angle = np.linspace(0, 4*np.pi, 601)
        return self.source(np.column_stack([200*np.cos(angle), 200*np.sin(angle)]))

    def tail(self, offset=0):
        return self.source([[0,0],[1000,0],[1000,300],[1600,300],
                            [1600,-300],[1000,-300],[1000,offset],
                            [0,offset],[0,0]])

    def commit(self, analysis, **overrides):
        kwargs = dict(goal=analysis["candidate"]["goal"] if analysis["candidate"] else "main_loop",
                      plan_digest=analysis["plan_digest"], confirm=True,
                      acknowledge_access_loss=True, acknowledge_geometry_only=True)
        kwargs.update(overrides)
        return plans.apply(self.path, self.output, analysis, **kwargs)

    def assert_rejected(self, analysis, **kwargs):
        with self.assertRaises(plans.PlanError):
            self.commit(analysis, **kwargs)
        self.assertFalse(self.output.exists())

    def test_analysis_no_public_files_no_input_changes_and_json(self):
        self.tail()
        raw = self.path.read_bytes()
        before = set(self.root.rglob("*"))
        analysis = plans.analyze(self.path, rd.Config())
        self.assertEqual(before, set(self.root.rglob("*")))
        self.assertEqual(raw, self.path.read_bytes())
        self.assertEqual(analysis, json.loads(json.dumps(analysis, allow_nan=False)))
        self.assertEqual(analysis["schema_version"], "1.0")
        self.assertEqual(analysis["input_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(analysis["analysis_id"], analysis["plan_digest"])
        self.assertEqual(analysis, plans.analyze(self.path, asdict(rd.Config())))
        candidate = analysis["candidate"]
        self.assertEqual(candidate["goal"], "main_loop")
        self.assertEqual(candidate["blocked_reasons"], [])
        self.assertIs(candidate["requires_confirmation"], True)
        self.assertGreater(candidate["removed_travel_m"], 1500)
        self.assertFalse(analysis["report"]["road_identity_verified"])
        self.assertNotEqual(analysis["original"], analysis["preview"])

    def test_valid_lap_apply_manifest_and_export(self):
        self.lap()
        raw = self.path.read_bytes()
        analysis = plans.analyze(self.path)
        self.assertEqual(analysis["candidate"]["goal"], "one_lap")
        returned_report, original, cleaned = self.commit(analysis, acknowledge_access_loss=False)
        manifest = json.loads((self.output / "manifest.json").read_text("utf-8"))
        self.assertEqual(original, analysis["original"])
        self.assertEqual(cleaned, analysis["preview"])
        self.assertEqual(returned_report, manifest["report"])
        self.assertEqual({path.name for path in self.output.iterdir()}, set(plans.OUTPUT_NAMES))
        self.assertEqual(raw, self.path.read_bytes())
        report = json.loads((self.output / "report.json").read_text("utf-8"))
        self.assertEqual(report["schema_version"], "1.0")
        self.assertIs(report["confirmed"], True)
        self.assertIs(report["road_identity_verified"], False)
        self.assertEqual(report["goal"], "one_lap")
        self.assertEqual(report["plan_digest"], analysis["plan_digest"])
        self.assertIn("endpoint_adjustment_m", report)
        self.assertIn("unmatched_access_m", report)
        self.assertEqual(manifest, json.loads((self.output / "manifest.json").read_text("utf-8")))
        for name, metadata in manifest["artifacts"].items():
            data = (self.output / name).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), metadata["sha256"])
            self.assertEqual(len(data), metadata["bytes"])
        geojson = json.loads((self.output / "cleaned.geojson").read_text("utf-8"))
        self.assertFalse(geojson["features"][0]["properties"]["road_identity_verified"])
        coords, _, _, _ = rd.load_gpx(self.output / "cleaned.gpx")
        self.assertLess(len(coords), len(analysis["original"]))

    def test_valid_tail_apply_records_both_access_removal(self):
        self.tail()
        analysis = plans.analyze(self.path)
        report, _, _ = self.commit(analysis)
        self.assertGreater(report["removed_travel_m"], 1500)
        self.assertEqual(report["unmatched_access_m"], analysis["candidate"]["unmatched_access_m"])
        self.assertGreaterEqual(report["endpoint_adjustment_m"], 0)
        self.assertTrue(any("Both access legs" in note for note in report["cautions"]))

    def test_unequal_access_loss_is_measured_and_ack_required(self):
        self.source([[0,0],[0,500],[500,500],[4000,500],
                     [4000,2500],[8000,2500],[8000,-1500],[4000,-1500],
                     [4000,500],[500,500],[500,-100],[0,-100],[0,0]])
        analysis = plans.analyze(self.path)
        self.assertGreater(analysis["candidate"]["unmatched_access_m"], 2000)
        self.assert_rejected(analysis, acknowledge_access_loss=False)
        self.commit(analysis)

    def test_long_finite_route_within_sample_budget(self):
        # Retain practical long-route support; limits are admission checks only.
        self.source([[0,0],[4000,0],[4000,16000],[18000,16000],
                     [18000,-16000],[4000,-16000],[4000,0],[0,0]])
        analysis = plans.analyze(self.path)
        self.assertGreater(analysis["report"]["input_length_m"], 90000)
        self.assertEqual(analysis["candidate"]["goal"], "main_loop")
        self.assertEqual(analysis["candidate"]["blocked_reasons"], [])

    def test_errors_are_chinese_without_private_paths(self):
        private = self.root / "confidential-file-name.gpx"
        with self.assertRaises(plans.PlanError) as context:
            plans.analyze(private)
        self.assertNotIn(str(self.root), str(context.exception))
        self.assertNotIn(private.name, str(context.exception))
        self.assertTrue(any("一" <= c <= "鿿" for c in str(context.exception)))

    def test_exact_boolean_confirmation(self):
        self.lap()
        analysis = plans.analyze(self.path)
        for value in (False, 1, "true", None, [], np.bool_(True)):
            with self.subTest(confirm=repr(value)):
                self.assert_rejected(analysis, confirm=value)

    def test_acknowledgments_are_explicit_exact_bools(self):
        self.tail()
        analysis = plans.analyze(self.path)
        for field in ("acknowledge_access_loss", "acknowledge_geometry_only"):
            for value in (False, 1, "yes", None):
                with self.subTest(field=field, value=value):
                    self.assert_rejected(analysis, **{field: value})

    def test_goal_must_be_explicit_and_match(self):
        self.lap()
        analysis = plans.analyze(self.path)
        for value in (None, "", "main_loop", "auto", True):
            self.assert_rejected(analysis, goal=value)
        with self.assertRaises(TypeError):
            plans.apply(self.path, self.output, analysis, plan_digest=analysis["plan_digest"],
                        confirm=True, acknowledge_access_loss=True, acknowledge_geometry_only=True)
        self.assertFalse(self.output.exists())

    def test_digest_required_and_matching(self):
        self.lap()
        analysis = plans.analyze(self.path)
        for value in (None, "", "a"*64, 123):
            self.assert_rejected(analysis, plan_digest=value)

    def test_input_change_is_rejected(self):
        self.lap()
        analysis = plans.analyze(self.path)
        self.path.write_bytes(self.path.read_bytes() + b"\n")
        self.assert_rejected(analysis)

    def test_config_change_changes_digest(self):
        self.lap()
        first = plans.analyze(self.path)
        second = plans.analyze(self.path, rd.Config(match_radius_m=17))
        self.assertNotEqual(first["plan_digest"], second["plan_digest"])
        forged = copy.deepcopy(first)
        forged["config"]["match_radius_m"] = 17
        self.assert_rejected(forged)

    def test_full_analysis_tampering_rejected_even_if_rehashed(self):
        self.lap()
        analysis = plans.analyze(self.path)
        mutations = [lambda a: a["candidate"].update(removed_travel_m=0),
                     lambda a: a["candidate"].update(requires_confirmation=False),
                     lambda a: a["candidate"].update(warnings=[]),
                     lambda a: a["candidate"].update(blocked_reasons=[] , goal="main_loop"),
                     lambda a: a["preview"][0].__setitem__(0, 0),
                     lambda a: a["original"][0].__setitem__(0, 0),
                     lambda a: a["report"].update(method="forged"),
                     lambda a: a.update(extra=True)]
        for mutation in mutations:
            forged = copy.deepcopy(analysis)
            mutation(forged)
            payload = {key: value for key, value in forged.items() if key not in ("analysis_id", "plan_digest")}
            digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                               separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
            forged.update(plan_digest=digest, analysis_id=digest)
            self.assert_rejected(forged)

    def test_no_candidate_cannot_apply(self):
        self.source([[0,0],[1000,0],[1000,1000],[0,1000]])
        analysis = plans.analyze(self.path)
        self.assertIsNone(analysis["candidate"])
        self.assertEqual(analysis["preview"], analysis["original"])
        self.assert_rejected(analysis)

    def test_pure_out_and_back_with_noise_never_applies(self):
        x = np.linspace(0,3000,151)
        for noise in (0., 5., 12.):
            outward = np.column_stack([x, noise * np.sin(x/130)])
            inward = np.column_stack([x[::-1], noise * np.cos(x[::-1]/170)])
            self.source(np.vstack([outward, inward, outward[:1]]))
            analysis = plans.analyze(self.path)
            candidate = analysis["candidate"]
            self.assertTrue(candidate is None or candidate["blocked_reasons"])
            self.assertEqual(analysis["preview"], analysis["original"])
            self.assert_rejected(analysis)

    def test_near_linear_retained_middle_is_blocked(self):
        self.source([[0,0],[1000,0],[1000,10],[3000,10],
                     [3000,-10],[1000,-10],[1000,0],[0,0]])
        analysis = plans.analyze(self.path)
        self.assertIsNotNone(analysis["candidate"])
        self.assertIn("retained_middle_near_linear_or_degenerate", analysis["candidate"]["blocked_reasons"])
        self.assert_rejected(analysis)

    def test_stable_parallel_access_inside_match_radius_is_blocked(self):
        for offset in (5., 12., -12.):
            with self.subTest(offset=offset):
                self.tail(offset)
                analysis = plans.analyze(self.path)
                candidate = analysis["candidate"]
                self.assertIsNotNone(candidate)
                self.assertIn("stable_lateral_offset_parallel_access_evidence", candidate["blocked_reasons"])
                self.assertEqual(analysis["preview"], analysis["original"])
                self.assert_rejected(analysis)
                forged = copy.deepcopy(analysis)
                forged["candidate"]["blocked_reasons"] = []
                self.assert_rejected(forged)

    def test_noisy_parallel_access_uses_sustained_window_evidence(self):
        vertices = np.asarray([[0,0],[4000,0],[4000,2000],[8000,2000],
                               [8000,-2000],[4000,-2000],[4000,10],[0,10],[0,0]], dtype=float)
        pieces = [np.linspace(a, b, max(1, int(np.ceil(np.linalg.norm(b-a)/8)))+1)[:-1]
                  for a, b in zip(vertices[:-1], vertices[1:])]
        ideal = np.vstack([*pieces, vertices[-1:]])
        for noise in (3., 8.):
            for seed in (1729, 104729):
                with self.subTest(noise=noise, seed=seed):
                    points = ideal + np.random.default_rng(seed).normal(0, noise, ideal.shape)
                    self.source(points)
                    analysis = plans.analyze(self.path)
                    self.assertIsNotNone(analysis["candidate"])
                    self.assertIn("stable_lateral_offset_parallel_access_evidence",
                                  analysis["candidate"]["blocked_reasons"])
                    self.assertEqual(analysis["preview"], analysis["original"])
                    self.assert_rejected(analysis)

    def test_parallel_access_outside_radius_has_no_candidate(self):
        self.tail(50)
        analysis = plans.analyze(self.path)
        self.assertIsNone(analysis["candidate"])
        self.assert_rejected(analysis)

    def test_invalid_coordinates_elevation_and_dtd_rejected(self):
        self.tail()
        original = self.path.read_bytes()
        for bad in (b'nan', b'inf', b'181', b'-181'):
            self.path.write_bytes(original.replace(b'lon="9.00000000"', b'lon="'+bad+b'"', 1))
            with self.assertRaises(plans.PlanError):
                plans.analyze(self.path)
        for bad in (b'nan', b'inf', b'91', b'-91'):
            data = original.replace(b'lat="36.', b'lat="'+bad+b'" ignored="', 1)
            self.path.write_bytes(data)
            with self.assertRaises(plans.PlanError):
                plans.analyze(self.path)
        for data in (original.replace(b'<ele>100.00</ele>', b'<ele>NaN</ele>', 1),
                     b'<!DOCTYPE gpx [<!ENTITY x "danger">]>' + original,
                     original.decode("utf-8").encode("utf-16")):
            self.path.write_bytes(data)
            with self.assertRaises(plans.PlanError):
                plans.analyze(self.path)

    def test_invalid_configs_and_nan_analysis_rejected(self):
        self.lap()
        for key, value in (("spacing_m", 0), ("spacing_m", float('nan')),
                           ("spacing_m", True), ("spacing_m", 10**1000), ("match_radius_m", -1),
                           ("min_tail_match_fraction", 2), ("allow_bridge_m", -1)):
            config = asdict(rd.Config())
            config[key] = value
            with self.assertRaises(plans.PlanError):
                plans.analyze(self.path, config)
        with self.assertRaises(plans.PlanError):
            plans.analyze(self.path, {})
        analysis = plans.analyze(self.path)
        analysis["candidate"]["unmatched_access_m"] = float('nan')
        self.assert_rejected(analysis)

    def test_admission_limits_checked_before_legacy(self):
        self.lap()
        for limit in ("MAX_POINTS", "MAX_SAMPLES", "MAX_SPATIAL_PAIRS", "MAX_INPUT_BYTES"):
            with patch.object(plans, limit, 2), patch.object(rd, "run") as legacy:
                with self.assertRaises(plans.PlanError):
                    plans.analyze(self.path)
                legacy.assert_not_called()

    def test_dense_spatial_pairs_rejected_before_legacy(self):
        self.source([[0,0],[1,0],[0,0]])
        with patch.object(rd, "run") as legacy:
            with self.assertRaises(plans.PlanError):
                plans.analyze(self.path, rd.Config(spacing_m=.001))
            legacy.assert_not_called()

    def test_cannot_overwrite_source_or_existing_artifacts(self):
        self.lap()
        analysis = plans.analyze(self.path)
        self.commit(analysis)
        before = {p.name: p.read_bytes() for p in self.output.iterdir()}
        with self.assertRaises(plans.PlanError):
            self.commit(analysis)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.output.iterdir()})
        renamed = self.root / "cleaned.gpx"
        renamed.write_bytes(self.path.read_bytes())
        analysis = plans.analyze(renamed)
        with self.assertRaises(plans.PlanError):
            plans.apply(renamed, self.root, analysis, goal="one_lap", plan_digest=analysis["plan_digest"],
                        confirm=True, acknowledge_access_loss=False, acknowledge_geometry_only=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
