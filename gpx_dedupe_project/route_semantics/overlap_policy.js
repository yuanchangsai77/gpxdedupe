/* Physical road coverage filtering, not semantic equivalence or trace deletion. */
(function(root){
class Heap{constructor(){this.a=[]}better(a,b){return a.score>b.score+1e-8||(Math.abs(a.score-b.score)<=1e-8&&(a.quality<b.quality||(a.quality===b.quality&&a.id<b.id)))}push(x){const a=this.a;a.push(x);let i=a.length-1;while(i){const p=(i-1)>>1;if(!this.better(x,a[p]))break;a[i]=a[p];i=p;}a[i]=x;}pop(){const a=this.a;if(!a.length)return null;const best=a[0],x=a.pop();if(a.length){let i=0;while(true){let c=i*2+1;if(c>=a.length)break;if(c+1<a.length&&this.better(a[c+1],a[c]))c++;if(!this.better(a[c],x))break;a[i]=a[c];i=c;}a[i]=x;}return best;}}
function longest(route,predicate,weights,cap){let run=0,best=0;for(const atom of route.walk){if(predicate(atom)){run+=weights[atom];best=Math.max(best,run)}else run=0;}return Math.min(best,cap);}
function compute(index,ids,fraction=.5,minimumUnique=500,minimumRun=300,mode='compact',forced=[]){
 if(!Number.isFinite(fraction)||fraction<0||fraction>1||!Number.isFinite(minimumUnique)||!Number.isFinite(minimumRun)||minimumUnique<=0||minimumRun<=0||!['compact','coverage'].includes(mode))throw Error('invalid overlap policy');
 function qualifies(unique,physical,run){return unique>1e-6&&(unique/physical+1e-9>=fraction||(mode==='coverage'&&(unique+1e-6>=minimumUnique||run+1e-6>=minimumRun)));}
 const forcedSet=new Set(forced);
 const routes=new Map(),inverse=new Map(),scores=new Map(),weights=index.weights,covered=new Set(),selected=new Set(),atDecision=new Map(),protectedIds=new Set();
 for(const id of ids){const r=index.routes[String(id)];if(!r)continue;routes.set(id,r);let sum=0;for(const atom of r.atoms){sum+=weights[atom];let list=inverse.get(atom);if(!list){list=[];inverse.set(atom,list)}list.push(id);}scores.set(id,sum);}
 function choose(id){const r=routes.get(id);atDecision.set(id,{novel_m:scores.get(id),novel_fraction:r.physical_m?scores.get(id)/r.physical_m:0});selected.add(id);for(const atom of r.atoms)if(!covered.has(atom)){covered.add(atom);for(const other of inverse.get(atom))scores.set(other,Math.max(0,scores.get(other)-weights[atom]));}}
 for(const id of [...routes.keys()].sort((a,b)=>a-b))if(routes.get(id).protect||forcedSet.has(id)){protectedIds.add(id);choose(id);}
 const heap=new Heap();for(const [id,r] of routes)if(!selected.has(id))heap.push({id,score:scores.get(id),quality:r.quality||0});
 while(heap.a.length){const item=heap.pop(),id=item.id,r=routes.get(id),novel=scores.get(id);if(selected.has(id))continue;if(Math.abs(novel-item.score)>1e-7){heap.push({...item,score:novel});continue;}const maxRun=longest(r,a=>!covered.has(a),weights,novel);if(qualifies(novel,r.physical_m,maxRun))choose(id);}
 const counts=new Map();for(const id of selected)for(const atom of routes.get(id).atoms)counts.set(atom,(counts.get(atom)||0)+1);
 let stabilizationRounds=0,reconsidered=0,stable=false;const seen=new Set();
 function residual(id,self){const r=routes.get(id);let unique=0;for(const atom of r.atoms)if((counts.get(atom)||0)<=self)unique+=weights[atom];return {unique,run:longest(r,a=>(counts.get(a)||0)<=self,weights,unique)};}
 for(let round=0;round<128;round++){
  stabilizationRounds++;const signature=[...selected].sort((a,b)=>a-b).join(',');if(seen.has(signature))throw Error('overlap_selection_cycle');seen.add(signature);let changed=false;
  for(const id of [...selected].reverse()){if(protectedIds.has(id))continue;const r=routes.get(id),m=residual(id,1);if(qualifies(m.unique,r.physical_m,m.run))continue;selected.delete(id);for(const atom of r.atoms)counts.set(atom,counts.get(atom)-1);changed=true;}
  const refill=new Heap();for(const [id,r] of routes)if(!selected.has(id)){const m=residual(id,0);scores.set(id,m.unique);refill.push({id,score:m.unique,quality:r.quality||0});}
  while(refill.a.length){const item=refill.pop(),id=item.id,r=routes.get(id),unique=scores.get(id);if(selected.has(id))continue;if(Math.abs(unique-item.score)>1e-7){refill.push({...item,score:unique});continue;}const run=longest(r,a=>(counts.get(a)||0)===0,weights,unique);if(!qualifies(unique,r.physical_m,run))continue;
   selected.add(id);reconsidered++;changed=true;atDecision.set(id,{novel_m:unique,novel_fraction:r.physical_m?unique/r.physical_m:0,phase:'reconsidered'});
   for(const atom of r.atoms){const before=counts.get(atom)||0;counts.set(atom,before+1);if(before===0)for(const other of inverse.get(atom))scores.set(other,Math.max(0,scores.get(other)-weights[atom]));}
  }
  if(!changed){stable=true;break;}
 }
 if(!stable)throw Error('overlap_selection_resource_limit');
 const records=new Map();let loops=0;
 for(const [id,r] of routes){const keep=selected.has(id);let unique=0,globalUnique=0;const providers=new Map();for(const atom of r.atoms){const self=keep?1:0;if((counts.get(atom)||0)<=self)unique+=weights[atom];if(inverse.get(atom).length===1)globalUnique+=weights[atom];for(const provider of (keep?[]:inverse.get(atom)))if(provider!==id&&selected.has(provider))providers.set(provider,(providers.get(provider)||0)+weights[atom]);}
  const run=longest(r,a=>(counts.get(a)||0)<=(keep?1:0),weights,unique),loop=r.protect==='closed_or_near_closed_loop';if(loop)loops++;
  records.set(id,{keep,physical_m:r.physical_m,traveled_m:r.traveled_m,unique_m:unique,unique_fraction:r.physical_m?unique/r.physical_m:0,global_unique_fraction:r.physical_m?globalUnique/r.physical_m:0,longest_unique_m:run,protected_reason:r.protect||(forcedSet.has(id)?'high_density_region':null),at_selection:atDecision.get(id)||null,covered_by:[...providers].sort((a,b)=>b[1]-a[1]||a[0]-b[0]).slice(0,5).map(([route_id,overlap_m])=>({route_id,overlap_m})),reason:r.protect?'环线或复杂分段结构保护':forcedSet.has(id)?'高密度区域覆盖保护':keep?(mode==='compact'?'独有覆盖比例达到热门精简门槛':'独有覆盖或连续独有段达到门槛'):'相对保留集合独有覆盖不足，降低默认展示优先级'});
 }
 let totalWeight=0,coveredWeight=0;for(const atom of inverse.keys()){totalWeight+=weights[atom];if((counts.get(atom)||0)>0)coveredWeight+=weights[atom];}
 return {input:routes.size,kept:selected.size,hidden:routes.size-selected.size,loop_protected:loops,coverage_m:coveredWeight,total_coverage_m:totalWeight,coverage_fraction:totalWeight?coveredWeight/totalWeight:1,unique_fraction_threshold:fraction,mode,minimum_unique_m:mode==='coverage'?minimumUnique:null,minimum_run_m:mode==='coverage'?minimumRun:null,policy_version:'road-overlap-policy-3',stabilization_rounds:stabilizationRounds,reconsidered,rule_consistent:true,records};
}
const caches=new WeakMap();
function apply(routes,result,index,fraction,cells,densityThreshold,mode='compact'){
 const ids=[],byId=new Map(),baseCells=new Set(),fixedCells=new Set();
 for(const r of routes){const d=result.decisions.get(r.id);if(!d?.keep)continue;const high=r.contributions.filter(v=>v[0]===5000&&cells.get(`${v[1]},${v[2]}`)>densityThreshold);for(const v of high)baseCells.add(`${v[1]},${v[2]}`);
  if(d.kind==='rebuilt'&&index.routes[String(r.id)]){ids.push(r.id);byId.set(r.id,{route:r,high});}else for(const v of high)fixedCells.add(`${v[1]},${v[2]}`);
 }
 let cache=caches.get(index);if(!cache){cache=new Map();caches.set(index,cache)}const forced=new Set();let chosen;
 for(let round=0;round<32;round++){
  const key=mode+':'+fraction+':'+ids.join(',')+':'+[...forced].sort((a,b)=>a-b).join(',');chosen=cache.get(key);if(!chosen){chosen=compute(index,ids,fraction,500,300,mode,[...forced]);cache.set(key,chosen);if(cache.size>8)cache.delete(cache.keys().next().value);}
  const covered=new Set(fixedCells);for(const [id,record]of chosen.records)if(record.keep)for(const v of byId.get(id).high)covered.add(`${v[1]},${v[2]}`);
  const missing=new Set([...baseCells].filter(key=>!covered.has(key)));if(!missing.size)break;
  while(missing.size){let best=null,bestScore=0;for(const id of ids){if(chosen.records.get(id).keep||forced.has(id))continue;let score=0;for(const v of byId.get(id).high)if(missing.has(`${v[1]},${v[2]}`))score+=Math.min(v[3],5000);if(score>bestScore+1e-8||(score>0&&Math.abs(score-bestScore)<=1e-8&&(best===null||id<best))){best=id;bestScore=score;}}
   if(best===null)throw Error('overlap_region_protection_missing_candidate');forced.add(best);for(const v of byId.get(best).high)missing.delete(`${v[1]},${v[2]}`);
  }
  if(round===31)throw Error('overlap_region_protection_resource_limit');
 }
 let hidden=0;for(const [id,record]of chosen.records){const d=result.decisions.get(id);d.coverage={...record};d.reason=record.reason;if(!record.keep){d.keep=false;hidden++;}}
 result.rebuilt-=hidden;result.hidden+=hidden;result.overlapHidden=hidden;result.overlapLoops=chosen.loop_protected;result.overlapCoverageProtected=forced.size;result.overlapCoverageFraction=chosen.coverage_fraction;result.overlapReconsidered=chosen.reconsidered;result.overlapConsistent=chosen.rule_consistent;return result;
}
root.OverlapPolicy={compute,apply};if(typeof module!=='undefined')module.exports=root.OverlapPolicy;
})(globalThis);
