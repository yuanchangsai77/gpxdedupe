/* Single evaluator shared by browser and delivery exports. */
(function(root){
const node=typeof module!=='undefined'&&module.exports;
const density=node?require('./density_policy.js'):root.DensityPolicy;
const strict=node?require('./rebuilt_dedup.js'):root.RebuiltDedup;
const overlap=node?require('./overlap_policy.js'):root.OverlapPolicy;
function evaluate(data,config={}){
 const threshold=config.threshold??10,metric=config.metric??'routes',mode=config.overlapMode??'compact',fraction=config.overlapFraction??.5,enabled=config.overlapEnabled??true;
 const cells=new Map(data.cells.filter(c=>c[0]===5000).map(c=>[`${c[1]},${c[2]}`,metric==='routes'?c[3]:c[4]/25000]));
 let result=density.select(data.routes,cells,threshold,config.lowFraction??.2,config.minimumMeters??500);
 if(config.strictDedup)result=strict.apply(data.routes,result,cells,threshold);
 if(enabled&&mode!=='off'){if(!data.overlap)throw Error('overlap_index_required');result=overlap.apply(data.routes,result,data.overlap,fraction,cells,threshold,mode);}
 result.finalPolicyVersion='route-final-policy-3';result.overlapSettings={enabled:enabled&&mode!=='off',mode,fraction};return result;
}
root.FinalPolicy={evaluate};if(node)module.exports=root.FinalPolicy;
})(globalThis);
