const assert=require('node:assert/strict'),{compute}=require('../route_semantics/overlap_policy.js');
function r(id,atoms,protect=null){return {id,atoms:[...new Set(atoms)],walk:atoms,physical_m:new Set(atoms).size*100,traveled_m:atoms.length*100,quality:0,protect};}
let index={weights:Array(30).fill(100),routes:{'1':r(1,[0,1,2,3,4,5,6,7,8,9]),'2':r(2,[0,1,2,3,4]),'3':r(3,[0,1,2,3,4], 'closed_or_near_closed_loop')}};
let result=compute(index,[1,2],.1);assert.equal(result.kept,1);assert.equal(result.records.get(2).keep,false);assert.equal(result.records.get(2).unique_fraction,0);
result=compute(index,[1,2,3],.1);assert.equal(result.records.get(3).keep,true);assert.equal(result.records.get(2).keep,false);
index.routes['4']=r(4,[0,1,2,3,4,5,6,7,8,10,11,12]);result=compute(index,[1,4],.5,500,300,'coverage');assert.equal(result.records.get(4).keep,true); // legacy branch protection
index.routes={'1':r(1,[0,1,2]),'2':r(2,[0,1,2])};result=compute(index,[1,2],.1);assert.equal(result.kept,1);assert.ok(result.coverage_fraction===1);assert.ok(result.records.get(2).covered_by.some(r=>r.route_id===1));
console.log('PASS overlap policy: subset hidden, loop protected, branch retained, no mutual deletion');
const {apply}=require('../route_semantics/overlap_policy.js');
const rows=[{id:1,contributions:[[5000,1,1,1000]]},{id:2,contributions:[[5000,2,1,1000]]},{id:3,contributions:[[5000,3,1,1000]]}];
const base={original:1,rebuilt:2,hidden:0,decisions:new Map([[1,{keep:true,kind:'rebuilt',reason:''}],[2,{keep:true,kind:'rebuilt',reason:''}],[3,{keep:true,kind:'original',reason:'raw'}]])};
const withProtection=apply(rows,base,index,.1,new Map([['1,1',100],['2,1',100],['3,1',1]]),10);
assert.equal(withProtection.overlapCoverageProtected,2);assert.equal(withProtection.rebuilt,2);assert.equal(withProtection.original,1);assert.equal(withProtection.decisions.get(3).reason,'raw');assert.equal(withProtection.decisions.get(1).coverage.unique_fraction,0);assert.equal(withProtection.decisions.get(1).coverage.protected_reason,'high_density_region');
console.log('PASS overlap scope: original untouched and last high-density region protected');

const zero=compute(index,[1,2],0);assert.equal(zero.kept,1);assert.equal(zero.coverage_fraction,1);

// A long branch must not bypass the requested percentage in compact mode.
const branchIndex={weights:Array(120).fill(100),routes:{'1':r(1,Array.from({length:100},(_,i)=>i)), '2':r(2,[...Array.from({length:90},(_,i)=>i),100,101,102,103,104,105])}};
const compact=compute(branchIndex,[1,2]);
assert.equal(compact.mode,'compact');assert.equal(compact.unique_fraction_threshold,.5);
assert.equal(compact.records.get(2).keep,false);assert.equal(compact.minimum_unique_m,null);
assert.equal(compute(branchIndex,[1,2],.5,500,300,'coverage').records.get(2).keep,true);
assert.equal(compute(branchIndex,[1,2],.05).records.get(2).keep,true);
assert.equal(compute(branchIndex,[1,2],.8).records.get(2).keep,false);
for(const v of compact.records.values())if(v.keep&&!v.protected_reason)assert.ok(v.unique_fraction+1e-9>=.5);
const branchRows=[{id:1,contributions:[[5000,1,1,1000]]},{id:2,contributions:[[5000,1,1,1000]]}];
const fresh=()=>({original:0,rebuilt:2,hidden:0,decisions:new Map([[1,{keep:true,kind:'rebuilt',reason:''}],[2,{keep:true,kind:'rebuilt',reason:''}]])});
const cells=new Map([['1,1',100]]);
assert.equal(apply(branchRows,fresh(),branchIndex,.5,cells,10,'compact').rebuilt,1);
assert.equal(apply(branchRows,fresh(),branchIndex,.5,cells,10,'coverage').rebuilt,2);
assert.equal(apply(branchRows,fresh(),branchIndex,.5,cells,10,'compact').rebuilt,1);
for(const args of [[NaN,500,300,'compact'],[.5,NaN,300,'compact'],[.5,500,Infinity,'compact'],[.5,500,300,'unknown']])assert.throws(()=>compute(branchIndex,[1,2],...args));
console.log('PASS compact policy: percentage cannot be bypassed, coverage mode opt-in, mode-aware cache, invalid inputs rejected');

// Pruning a covering route must reconsider previously rejected routes.
const repairIndex={weights:Array(10).fill(100),routes:{1:r(1,[0,1,2,3,4,5]),2:r(2,[0,1,6,7]),3:r(3,[2,3,8,9]),4:r(4,[4,5])}};
const repaired=compute(repairIndex,[1,2,3,4],.5,500,300,'compact');
assert.equal(repaired.records.get(4).keep,true);assert.ok(repaired.reconsidered>0);
for(const v of repaired.records.values())if(!v.protected_reason)assert.equal(v.keep,v.unique_m>1e-6&&v.unique_fraction+1e-9>=.5);
let seed=42;const random=()=>((seed=(Math.imul(seed,1664525)+1013904223)>>>0)/2**32);
for(let trial=0;trial<150;trial++){const routes={};for(let id=1;id<=8;id++){const atoms=[];for(let a=0;a<20;a++)if(random()<.35)atoms.push(a);if(!atoms.length)atoms.push(id);routes[id]=r(id,atoms);}const result=compute({weights:Array(20).fill(100),routes},[1,2,3,4,5,6,7,8],.5);for(const v of result.records.values())assert.equal(v.keep,v.unique_m>1e-6&&v.unique_fraction+1e-9>=.5);}
console.log('PASS stabilization: rejected routes reconsidered and both keep/drop rules checked on random fixtures');
