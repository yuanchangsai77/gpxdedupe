import sys
import tempfile
import unittest
from pyproj import Transformer
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import numpy as np
from route_dedupe import Config, find_retraced_tails, find_identical_laps, resample, run, write_gpx, load_gpx

def test_clean_single_circle_not_removed():
    a=np.linspace(0,2*np.pi,700)
    q=np.column_stack([200*np.cos(a),200*np.sin(a)])
    s,r=resample(q,10)
    assert find_retraced_tails(s,r,Config()) is None
    assert find_identical_laps(s,r,Config()) is None

def test_two_identical_laps():
    a=np.linspace(0,4*np.pi,1500)
    q=np.column_stack([200*np.cos(a),200*np.sin(a)])
    s,r=resample(q,10)
    lap=find_identical_laps(s,r,Config())
    assert lap is not None and lap[0]==2

def test_out_and_back_stem():
    # 300m tail out, rectangular loop, 300m tail returning home.
    points=np.array([[0,0],[300,0],[300,300],[600,300],[600,0],[300,0],[0,0]],dtype=float)
    s,r=resample(points,10)
    tails=find_retraced_tails(s,r,Config())
    assert tails is not None, tails
    assert tails['cut_start_m']>=260
    assert tails['gap_m']<=18

class RouteRegressionTests(unittest.TestCase):
    def process(self, points, cfg=None):
        points=np.asarray(points,dtype=float)+[500000,4000000]
        transform=Transformer.from_crs('EPSG:32632','EPSG:4326',always_xy=True)
        coords=np.column_stack(transform.transform(points[:,0],points[:,1]))
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'input.gpx'
            output=Path(directory)/'result'
            write_gpx(source,coords,np.full(len(coords),100.),'Synthetic regression')
            report,original,cleaned=run(source,output,cfg or Config())
            exported,_,_,_=load_gpx(output/'cleaned.gpx')
            self.assertEqual(len(exported),len(cleaned))
            self.assertTrue((output/'cleaned.geojson').is_file())
            self.assertTrue((output/'report.json').is_file())
            return report,original,cleaned

    def test_existing_detectors(self):
        test_clean_single_circle_not_removed()
        test_two_identical_laps()
        test_out_and_back_stem()

    def test_long_tail_beyond_old_search_cap(self):
        points=[[0,0],[4000,0],[4000,2000],[8000,2000],
                [8000,-2000],[4000,-2000],[4000,0],[0,0]]
        r,_,cleaned=self.process(points)
        self.assertEqual(r['method'],'reversed_tail_collapse')
        self.assertGreater(r['retraced_tail']['cut_start_m'],3900)
        self.assertAlmostEqual(r['output_length_m'],16000,delta=60)
        self.assertTrue(r['output_closed'])
        np.testing.assert_allclose(cleaned[0],cleaned[-1])
        limited,_,_=self.process(points,Config(max_tail_m=2500))
        self.assertLess(limited['retraced_tail']['cut_start_m'],2600)

    def test_long_tail_does_not_hide_small_real_loop(self):
        points=[[0,0],[10000,0],[10000,250],[10250,250],
                [10250,0],[10000,0],[0,0]]
        r,_,_=self.process(points)
        self.assertEqual(r['method'],'reversed_tail_collapse')
        self.assertLess(r['output_length_m'],3000)
        self.assertTrue(r['output_closed'])

    def test_valid_splice_survives_later_nonbridgeable_matches(self):
        points=[[0,0],[4000,0],[4000,2000],[8000,2000],
                [8000,-2000],[4000,-2000],[4000,30],
                [3000,30],[2000,0],[0,0]]
        r,_,_=self.process(points,Config(match_radius_m=40))
        self.assertEqual(r['method'],'reversed_tail_collapse')
        self.assertGreater(r['retraced_tail']['cut_start_m'],2000)
        self.assertLess(r['retraced_tail']['cut_start_m'],3000)
        self.assertLessEqual(r['retraced_tail']['gap_m'],20)
        self.assertTrue(r['output_closed'])

    def test_unequal_nonoverlapping_access_before_shared_tail(self):
        # Different 1.0/1.2km access legs, then a shared 3.5km stem.
        # Like the reported route: matching starts late, arc sums have an offset.
        points=[[0,0],[0,500],[500,500],[4000,500],
                [4000,2500],[8000,2500],[8000,-1500],[4000,-1500],
                [4000,500],[500,500],[500,-100],[0,-100],[0,0]]
        r,_,_=self.process(points)
        self.assertEqual(r['method'],'reversed_tail_collapse')
        tail=r['retraced_tail']
        self.assertGreater(tail['match_start_m'],900)
        self.assertGreater(tail['prefix_matched_m'],3300)
        self.assertGreater(tail['unmatched_access_m'],2000)
        self.assertGreaterEqual(tail['tail_match_fraction'],.7)
        self.assertAlmostEqual(r['output_length_m'],16000,delta=60)

    def test_pure_out_and_back_is_not_cropped_to_fake_loop(self):
        r,original,cleaned=self.process([[0,0],[3000,0],[0,0]])
        self.assertEqual(r['method'],'unchanged')
        np.testing.assert_array_equal(original,cleaned)

    def test_open_route_is_unchanged(self):
        r,original,cleaned=self.process([[0,0],[1000,0],[1000,1000],[0,1000]])
        self.assertEqual(r['method'],'unchanged')
        np.testing.assert_array_equal(original,cleaned)

    def test_single_loop_is_unchanged_end_to_end(self):
        a=np.linspace(0,2*np.pi,701)
        r,original,cleaned=self.process(np.column_stack([200*np.cos(a),200*np.sin(a)]))
        self.assertEqual(r['method'],'unchanged')
        np.testing.assert_array_equal(original,cleaned)

    def test_two_laps_collapse_end_to_end(self):
        a=np.linspace(0,4*np.pi,1501)
        r,_,_=self.process(np.column_stack([200*np.cos(a),200*np.sin(a)]))
        self.assertEqual(r['method'],'repeated_laps_collapse')
        self.assertEqual(r['identical_laps']['count'],2)
        self.assertAlmostEqual(r['output_length_m'],r['input_length_m']/2,delta=2)
        self.assertTrue(r['output_closed'])

    def test_figure_eight_is_unchanged(self):
        a=np.linspace(0,2*np.pi,801)
        r,original,cleaned=self.process(np.column_stack([800*np.sin(a),500*np.sin(2*a)]))
        self.assertEqual(r['method'],'unchanged')
        np.testing.assert_array_equal(original,cleaned)

    def test_parallel_roads_outside_radius_are_unchanged(self):
        r,original,cleaned=self.process([[0,0],[4000,0],[4000,2000],
                                        [8000,2000],[8000,-2000],[4000,-2000],
                                        [4000,50],[0,50],[0,0]])
        self.assertEqual(r['method'],'unchanged')
        np.testing.assert_array_equal(original,cleaned)

    def test_short_shared_segment_is_not_enough(self):
        r,original,cleaned=self.process([[0,0],[100,0],[100,1000],[1000,1000],
                                        [1000,-1000],[100,-1000],[100,0],[0,0]])
        self.assertEqual(r['method'],'unchanged')
        np.testing.assert_array_equal(original,cleaned)

    def test_small_overlap_far_from_start_fails_access_coverage(self):
        points=[[0,0],[0,1000],[2000,1000],[2300,1000],
                [2300,3000],[6000,3000],[6000,-1000],[2300,-1000],
                [2300,1000],[2000,1000],[2000,-500],[0,-500],[0,0]]
        r,original,cleaned=self.process(points)
        self.assertEqual(r['method'],'unchanged')
        np.testing.assert_array_equal(original,cleaned)

    def test_gap_above_bridge_limit_does_not_crop(self):
        r,original,cleaned=self.process([[0,0],[4000,0],[4000,2000],
                                        [8000,2000],[8000,-2000],[4000,-2000],
                                        [4000,30],[0,30],[0,0]],
                                       Config(match_radius_m=40))
        self.assertEqual(r['method'],'unchanged')
        np.testing.assert_array_equal(original,cleaned)

    def test_optional_real_reported_route(self):
        source=Path(__file__).resolve().parents[1]/'examples'/'37002172.gpx'
        if not source.is_file():
            self.skipTest('Private 37002172.gpx fixture not present; synthetic regressions still run')
        with tempfile.TemporaryDirectory() as directory:
            r,_,cleaned=run(source,directory)
            self.assertEqual(r['method'],'reversed_tail_collapse')
            self.assertGreater(r['retraced_tail']['prefix_matched_m'],3000)
            self.assertGreater(r['retraced_tail']['unmatched_access_m'],2000)
            self.assertGreater(r['output_length_m'],48000)
            self.assertLess(r['output_length_m'],50000)
            self.assertTrue(r['output_closed'])
            np.testing.assert_allclose(cleaned[0],cleaned[-1])


if __name__=='__main__':
    unittest.main(verbosity=2)
