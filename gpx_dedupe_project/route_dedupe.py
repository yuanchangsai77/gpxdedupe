#!/usr/bin/env python3
"""GPX trajectory topology cleanup V0.1.

Reliable supported transforms:
 - exact/sufficiently close repeated entire laps (same orientation)
 - opposite-direction overlapping out-and-back tails of closed loops
 - route statistics and overlap evidence.

NOT a general unique-edge graph reconstruction engine. It never silently deletes
internal branches. Raw timestamps are intentionally omitted from output routes.
"""
from __future__ import annotations
import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
import xml.etree.ElementTree as ET
import numpy as np
from scipy.spatial import cKDTree
from pyproj import CRS, Transformer

NS = "http://www.topografix.com/GPX/1/1"
ET.register_namespace('', NS)

def tag(n): return f'{{{NS}}}{n}'

@dataclass
class Config:
    spacing_m: float = 10.0
    match_radius_m: float = 18.0
    min_overlap_m: float = 150.0
    heading_min_opposite: float = 0.55
    closure_radius_m: float = 25.0
    allow_bridge_m: float = 20.0
    max_tail_m: float = 2500.0


def load_gpx(path):
    root = ET.parse(path).getroot()
    segs = root.findall('.//' + tag('trkseg'))
    if len(segs) != 1:
        raise ValueError(f'V0.1 only supports one track segment, found {len(segs)}; merge deliberately rather than bridging gaps')
    elems = segs[0].findall(tag('trkpt'))
    if len(elems)<3: raise ValueError('Need >=3 trackpoints')
    lonlat=np.array([[float(e.attrib['lon']),float(e.attrib['lat'])] for e in elems])
    eles=np.array([float(e.findtext(tag('ele'))) if e.findtext(tag('ele')) is not None else np.nan for e in elems])
    title=root.findtext('.//' + tag('metadata') + '/' + tag('name')) or Path(path).stem
    return lonlat, eles, title, root


def projection(lonlat):
    meanlon,meanlat=np.mean(lonlat,axis=0)
    zone=min(60,max(1,int((meanlon+180)//6)+1))
    epsg=(32600 if meanlat>=0 else 32700)+zone
    fw=Transformer.from_crs('EPSG:4326',f'EPSG:{epsg}',always_xy=True)
    inv=Transformer.from_crs(f'EPSG:{epsg}','EPSG:4326',always_xy=True)
    xy=np.column_stack(fw.transform(lonlat[:,0],lonlat[:,1]))
    return xy,inv,epsg


def arclength(xy): return np.r_[0.,np.cumsum(np.linalg.norm(np.diff(xy,axis=0),axis=1))]

def resample(xy, step):
    d=arclength(xy)
    keep=np.r_[True, np.diff(d)>1.e-7]
    d=d[keep];xy=xy[keep]
    s=np.r_[np.arange(0,d[-1],step),d[-1]]
    q=np.column_stack([np.interp(s,d,xy[:,j]) for j in range(2)])
    return s,q

def tangent(q):
    delta=np.empty_like(q)
    k=min(3, max(1,len(q)//8))
    for i in range(len(q)):
        delta[i]=q[min(len(q)-1,i+k)]-q[max(0,i-k)]
    norm=np.linalg.norm(delta,axis=1)
    return delta/np.maximum(norm[:,None],1.e-9)


def overlap_stats(s,q,cfg):
    # Evidence only: endpoints, heading-agnostic potential overlaps of nonadjacent samples.
    tree=cKDTree(q)
    pairs=tree.query_pairs(cfg.match_radius_m,output_type='ndarray')
    if len(pairs)==0: return {'candidate_sample_ratio':0.,'candidate_pairs':0}
    far=(s[pairs[:,1]]-s[pairs[:,0]])>=max(150.,cfg.min_overlap_m)
    pairs=pairs[far]
    marked=np.zeros(len(q),bool)
    if len(pairs):marked[pairs.ravel()]=True
    return {'candidate_sample_ratio':round(float(marked.mean()),5),'candidate_pairs':int(len(pairs))}


def find_retraced_tails(s,q,cfg):
    """Find the longest continuous prefix matching the reversed end of a closed route.

    Return a splice point (index in sampled trajectory) at the far end of the
    shared stem. We ignore isolated crossings and penalize non-reverse headings.
    """
    L=float(s[-1]); n=len(q)
    if np.linalg.norm(q[0]-q[-1])>cfg.closure_radius_m:return None
    extent=min(cfg.max_tail_m,L*0.18)
    first=np.flatnonzero(s<=extent)
    last=np.flatnonzero(s>=L-extent)
    last=last[last>first[-1]+5]
    if len(last)<10 or len(first)<10: return None
    tree=cKDTree(q[last]);dirs=tangent(q)
    distances,near=tree.query(q[first],k=min(8,len(last)))
    if distances.ndim==1:distances=distances[:,None];near=near[:,None]
    # Pick closest candidate with opposite direction and progression compatibility.
    chosen=np.full(len(first),-1,dtype=int)
    candidate_dist=np.full(len(first),np.inf)
    last_j=n
    for x,i in enumerate(first):
        for d,index in sorted(zip(distances[x],near[x]),key=lambda z:z[0]):
            j=int(last[index])
            if d>cfg.match_radius_m:break
            if float(dirs[i]@dirs[j])>-cfg.heading_min_opposite:continue
            # Along shared stem, the sum of arclengths stays near route total,
            # allowing offsets due to GPS noise and sampling differences.
            if abs((s[i]+s[j])-L)>max(120.,2*cfg.match_radius_m+60.):continue
            if j>last_j+4:continue
            chosen[x]=j;candidate_dist[x]=d;last_j=min(last_j,j)
            break
    ok=chosen>=0
    # Allow short GPS mismatch gaps; stop when > 70 m without match.
    longest_gap=int(max(7,70/cfg.spacing_m))
    last_good=0;miss=0
    for ix,v in enumerate(ok):
        if v: last_good=ix;miss=0
        else: miss+=1
        if miss>longest_gap: break
    if s[first[last_good]]<cfg.min_overlap_m:return None
    # Robustly select the farthest match from a 60m window before divergence.
    eligible=np.flatnonzero(ok & (np.arange(len(ok))<=last_good))
    if not len(eligible):return None
    finish=int(eligible[-1]); j=int(chosen[finish]);i=int(first[finish])
    return {'cut_start_m':float(s[i]),'cut_end_m':float(s[j]),
            'cut_start_index':i,'cut_end_index':j,
            'gap_m':float(np.linalg.norm(q[i]-q[j])),
            'matched_samples':int(sum(ok[:finish+1])),
            'prefix_matched_m':float(s[i])}


def find_identical_laps(s,q,cfg):
    """Detect k>=2 full traversals of nearly identical circles by phase alignment.

    Conservative: not invoked unless entire route consists of equal complete laps.
    """
    L=s[-1]
    for count in range(6,1,-1):
        period=L/count
        if period<cfg.min_overlap_m*3:continue
        distances=[]
        for fraction in np.linspace(0,1,151,endpoint=False):
            u=fraction*period
            base=np.array([np.interp(u,s,q[:,j]) for j in range(2)])
            for lap in range(1,count):
                p=np.array([np.interp(u+lap*period,s,q[:,j]) for j in range(2)])
                distances.append(np.linalg.norm(base-p))
        d=np.asarray(distances)
        if np.quantile(d,.9)<=cfg.match_radius_m and np.mean(d<=cfg.match_radius_m)>=.85:
            return count,float(period),{'p90_deviation_m':float(np.quantile(d,.9)), 'match_fraction':float(np.mean(d<=cfg.match_radius_m))}
    return None


def crop_at_distances(xy,ele,a,b):
    d=arclength(xy); keep=(d>a)&(d<b)
    def interp(x):
        c=np.array([np.interp(x,d,xy[:,j]) for j in range(2)])
        elevation=np.interp(x,d,np.nan_to_num(ele,nan=0)) if np.all(np.isfinite(ele)) else np.nan
        return c,elevation
    start,ea=interp(a);end,eb=interp(b)
    ret=np.vstack([start,xy[keep],end]);el=np.r_[ea,ele[keep],eb]
    return ret,el


def write_gpx(path,coords,ele,title,closed=False):
    root=ET.Element(tag('gpx'),{'version':'1.1','creator':'GPX Route Deduper V0.1'})
    meta=ET.SubElement(root,tag('metadata'))
    ET.SubElement(meta,tag('name')).text=title
    trk=ET.SubElement(root,tag('trk'));ET.SubElement(trk,tag('name')).text=title
    seg=ET.SubElement(trk,tag('trkseg'))
    for (lon,lat),z in zip(coords,ele):
        x=ET.SubElement(seg,tag('trkpt'),{'lat':f'{lat:.8f}','lon':f'{lon:.8f}'})
        if np.isfinite(z):ET.SubElement(x,tag('ele')).text=f'{z:.2f}'
    ET.ElementTree(root).write(path,encoding='utf-8',xml_declaration=True)


def write_geojson(path,coords,properties):
    obj={'type':'FeatureCollection','features':[{'type':'Feature','properties':properties,
        'geometry':{'type':'LineString','coordinates':coords.tolist()}}]}
    Path(path).write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding='utf8')


def run(path,out_dir,cfg=Config()):
    out_dir=Path(out_dir);out_dir.mkdir(parents=True,exist_ok=True)
    coords,ele,title,root=load_gpx(path)
    xy,inv,epsg=projection(coords);d=arclength(xy);s,q=resample(xy,cfg.spacing_m)
    closure=float(np.linalg.norm(xy[0]-xy[-1]));stats=overlap_stats(s,q,cfg)
    tails=find_retraced_tails(s,q,cfg)
    lap=find_identical_laps(s,q,cfg) if tails is None and closure<=cfg.closure_radius_m else None
    result_xy,result_ele=xy.copy(),ele.copy();method='unchanged'
    if tails is not None:
        a,b=tails['cut_start_m'],tails['cut_end_m']
        if tails['gap_m'] <=cfg.allow_bridge_m and b>a:
            result_xy,result_ele=crop_at_distances(xy,ele,a,b)
            # The same physical road is represented twice with a ~10m GPS bias.
            # Midpoint produces a small closure snap; do not pretend a connector road exists.
            mid=(result_xy[0]+result_xy[-1])/2
            result_xy[0]=mid;result_xy[-1]=mid
            method='reversed_tail_collapse'
        else:method='unchanged_tails_not_connectable'
    elif lap is not None:
        count,period,details=lap
        result_xy,result_ele=crop_at_distances(xy,ele,0,period)
        if np.linalg.norm(result_xy[0]-result_xy[-1])<=cfg.allow_bridge_m:
            mid=(result_xy[0]+result_xy[-1])/2
            result_xy[0]=mid;result_xy[-1]=mid
            method='repeated_laps_collapse'
        else:
            result_xy,result_ele=xy.copy(),ele.copy()
            method='unchanged_lap_gap_too_large'
    out=np.column_stack(inv.transform(result_xy[:,0],result_xy[:,1]))
    original_len=float(d[-1]);result_len=float(arclength(result_xy)[-1])
    report={'source':Path(path).name,'title':title,'projection_epsg':epsg,'input_points':len(coords),
            'input_length_m':round(original_len,2),'start_end_distance_m':round(closure,2),
            'is_closed':closure<=cfg.closure_radius_m,
            'overlap_candidates':stats,'retraced_tail':tails,
            'identical_laps':None if lap is None else {'count':lap[0],'length_m':lap[1],**lap[2]},
            'method':method,'output_points':len(out),'output_length_m':round(result_len,2),
            'removed_length_m':round(original_len-result_len,2),
            'output_closed':bool(np.linalg.norm(result_xy[0]-result_xy[-1])<0.1),
            'notes':['Counts and matches are based on projected geometry, not OSM map matching.',
                     'This prototype does not automatically remove arbitrary internal repeated branches.',
                     'Output omits timestamps because modified geometry invalidates original time semantics.',
                     'For retraced tails, two GPS observations are snapped to a shared midpoint; validate against roads before navigation.'],
            'config':asdict(cfg)}
    write_gpx(out_dir/'cleaned.gpx',out,result_ele,title+' (deduplicated)')
    write_geojson(out_dir/'cleaned.geojson',out,{'method':method,'length_m':round(result_len,2)})
    (out_dir/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    return report,xy,result_xy


def plot(xy,clean_xy,out):
    import matplotlib;matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig,axes=plt.subplots(1,2,figsize=(13,6),constrained_layout=True)
    for ax, data, ttl, color in zip(axes,[xy,clean_xy],['Original GPX','Collapsed route'],['#d97706','#1878c9']):
        ax.plot(data[:,0],data[:,1],lw=1.5,color=color)
        ax.scatter(data[0,0],data[0,1],s=40,c='#d64d4d',zorder=4)
        ax.set_title(ttl);ax.set_aspect('equal');ax.grid(alpha=.2)
    xlim=(min(xy[:,0])-600,max(xy[:,0])+600);ylim=(min(xy[:,1])-600,max(xy[:,1])+600)
    for ax in axes:ax.set_xlim(xlim);ax.set_ylim(ylim);ax.ticklabel_format(axis='both',style='plain',useOffset=False)
    fig.savefig(out,dpi=180)
    plt.close(fig)

if __name__=='__main__':
    p=argparse.ArgumentParser(description='GPX repeated lap / reversed-stem deduplication')
    p.add_argument('input');p.add_argument('--out',default='route_output')
    p.add_argument('--radius',type=float,default=18.)
    a=p.parse_args();report,xy,clean=run(a.input,a.out,Config(match_radius_m=a.radius))
    plot(xy,clean,Path(a.out)/'comparison.png')
    print(json.dumps(report,indent=2,ensure_ascii=False))
