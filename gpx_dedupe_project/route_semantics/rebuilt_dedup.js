/* Display-only equivalence groups; original routes are never deduplicated. */
(function(root){
function apply(routes,result,cells,threshold){
 const groups=new Map(),coverage=new Map();let merged=0,protectedRoutes=0;
 for(const r of routes){const d=result.decisions.get(r.id);if(!d?.keep)continue;for(const v of r.contributions)if(v[0]===5000&&cells.get(`${v[1]},${v[2]}`)>threshold){const key=`${v[1]},${v[2]}`;coverage.set(key,(coverage.get(key)||0)+1);}if(d.kind==='rebuilt'&&r.dedup){const id=r.dedup.group;let g=groups.get(id);if(!g){g=[];groups.set(id,g)}g.push(r);}}
 for(const group of groups.values()){group.sort((a,b)=>a.dedup.rank-b.dedup.rank||a.id-b.id);const canonical=group[0].id;for(const r of group.slice(1)){const high=r.contributions.filter(v=>v[0]===5000&&cells.get(`${v[1]},${v[2]}`)>threshold);if(high.some(v=>(coverage.get(`${v[1]},${v[2]}`)||0)<=1)){protectedRoutes++;continue;}const d=result.decisions.get(r.id);d.keep=false;d.duplicateOf=canonical;d.reason=`等价重建已由路线 ${canonical} 展示，原始证据与几何保留`;merged++;for(const v of high){const key=`${v[1]},${v[2]}`;coverage.set(key,coverage.get(key)-1);}}}
 result.rebuilt-=merged;result.hidden+=merged;result.merged=merged;result.dedupCoverageProtected=protectedRoutes;return result;
}
root.RebuiltDedup={apply};if(typeof module!=='undefined')module.exports=root.RebuiltDedup;
})(globalThis);
