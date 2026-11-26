/* Whole-route display policy. Density is always computed from all evidence. */
(function(root){
function decide(route,cells,threshold,minimumFraction=.2,minimumMeters=500){
 if(!(threshold>=0&&Number.isFinite(threshold)))throw Error('invalid density threshold');
 if(!(minimumFraction>=0&&minimumFraction<=1&&Number.isFinite(minimumFraction)&&minimumMeters>0&&Number.isFinite(minimumMeters)))throw Error('invalid retention limits');
 if(route.decision.kind==='none')return {keep:false,kind:'none',lowLength:0,fraction:0,reason:'原始轨迹基础质量待复核'};
 let lowLength=0,total=0;
 for(const v of route.contributions){if(v[0]!==5000)continue;const density=cells.get(`${v[1]},${v[2]}`);if(density===undefined)throw Error('5km density missing');total+=v[3];if(density<=threshold)lowLength+=v[3];}
 if(total<=0)return {keep:false,kind:'none',lowLength:0,fraction:0,reason:'缺少5公里统计，无法判断'};
 const fraction=lowLength/total,low=fraction+1e-9>=minimumFraction&&lowLength+1e-6>=Math.min(minimumMeters,total);
 if(low)return {keep:true,kind:'original',lowLength,fraction,reason:'低密度段达到门槛，优先使用整条原始轨迹'};
 if((route.reliableRebuilt??(route.decision.kind==='rebuilt'))&&route.rebuilt?.length)return {keep:true,kind:'rebuilt',lowLength,fraction,reason:'以高密度区为主，采用可靠重建'};
 return {keep:false,kind:'original',lowLength,fraction,reason:'以高密度区为主且无可靠重建，降低默认展示优先级'};
}
function select(routes,cells,threshold,minimumFraction=.2,minimumMeters=500){
 const decisions=new Map(),covered=new Set(),uncovered=new Set();
 for(const [key,value] of cells)if(value>threshold)uncovered.add(key);
 for(const r of routes){const d=decide(r,cells,threshold,minimumFraction,minimumMeters);decisions.set(r.id,d);if(d.keep)for(const v of r.contributions)if(v[0]===5000)covered.add(`${v[1]},${v[2]}`);}
 for(const key of covered)uncovered.delete(key);
 const emptyBefore=uncovered.size,candidates=[];
 for(const r of routes){if(decisions.get(r.id).keep||decisions.get(r.id).kind==='none')continue;let score=0;for(const v of r.contributions)if(v[0]===5000&&uncovered.has(`${v[1]},${v[2]}`))score+=Math.min(v[3],5000);if(score>=Math.min(minimumMeters,r.length))candidates.push({r,score});}
 candidates.sort((a,b)=>b.score-a.score||a.r.id-b.r.id);
 let fallback=0;
 for(const {r} of candidates){let novel=0;for(const v of r.contributions)if(v[0]===5000&&uncovered.has(`${v[1]},${v[2]}`))novel+=v[3];if(novel<Math.min(minimumMeters,r.length))continue;const d=decisions.get(r.id);d.keep=true;d.kind='original';d.fallback=true;d.reason='高密度网格缺少已选路线，保留原始路线补充区域覆盖';fallback++;for(const v of r.contributions)if(v[0]===5000)uncovered.delete(`${v[1]},${v[2]}`);}
 let original=0,rebuilt=0,hidden=0;for(const d of decisions.values()){if(!d.keep)hidden++;else if(d.kind==='rebuilt')rebuilt++;else original++;}
 return {original,rebuilt,hidden,fallback,emptyBefore,emptyAfter:uncovered.size,decisions};
}
root.DensityPolicy={decide,select};if(typeof module!=='undefined')module.exports=root.DensityPolicy;
})(globalThis);
