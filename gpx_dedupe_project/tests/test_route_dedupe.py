import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import numpy as np
from route_dedupe import Config, find_retraced_tails, find_identical_laps, resample

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

if __name__=='__main__':
    for name in sorted(x for x in globals() if x.startswith('test_')):
        globals()[name]();print('PASS',name)
