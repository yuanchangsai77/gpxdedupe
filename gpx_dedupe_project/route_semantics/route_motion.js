/* Distance-based inspection animation, never an inference of recorded speed. */
(function(root,factory){if(typeof module==='object'&&module.exports)module.exports=factory();else root.RouteMotion=factory();})(globalThis,()=>{
 'use strict';
 function distance(a,b){const rad=Math.PI/180;return 6371000*Math.hypot((b[0]-a[0])*rad*Math.cos((a[1]+b[1])*rad/2),(b[1]-a[1])*rad);}
 function build(entries,project){const points=[],coordinates=[],kinds=[],cumulative=[];let total=0;
  for(const entry of entries){const p=entry.coordinate;if(p.length!==2||!p.every(Number.isFinite))throw new Error('invalid animation coordinate');
   const previous=coordinates.at(-1),step=previous?distance(previous,p):0;if(previous&&step<1e-6)continue;
   total+=step;coordinates.push(p);points.push(project(p));kinds.push(entry.kind||'observed');cumulative.push(total);
  }return {points,coordinates,kinds,cumulative,total};
 }
 function at(track,progress){if(track.points.length<2||!track.total)return null;const target=Math.max(0,Math.min(1,progress))*track.total;let lo=0,hi=track.points.length-1;
  while(lo+1<hi){const middle=(lo+hi)>>1;if(track.cumulative[middle]<=target)lo=middle;else hi=middle;}
  const span=track.cumulative[lo+1]-track.cumulative[lo],fraction=span?(target-track.cumulative[lo])/span:0,a=track.points[lo],b=track.points[lo+1];
  return {index:lo,fraction,distance:target,point:[a[0]+(b[0]-a[0])*fraction,a[1]+(b[1]-a[1])*fraction],delta:[b[0]-a[0],b[1]-a[1]],kind:track.kinds[lo+1]};
 }
 function visits(track,state,radius=12){if(!state)return 0;const factor=6371000*Math.cos(track.coordinates[state.index][1]*Math.PI/180);let count=0,lastContact=-Infinity;
  for(let i=0;i<=state.index;i++){const a=track.points[i],b=track.points[i+1],x=b[0]-a[0],y=b[1]-a[1],norm=x*x+y*y,t=norm?Math.max(0,Math.min(1,((state.point[0]-a[0])*x+(state.point[1]-a[1])*y)/norm)):0,close=Math.hypot(state.point[0]-a[0]-t*x,state.point[1]-a[1]-t*y)*factor<=radius;
   if(close){const contact=track.cumulative[i]+t*(track.cumulative[i+1]-track.cumulative[i]);if(contact-lastContact>Math.max(20,radius*2.5))count++;lastContact=contact;}
  }return count;
 }
 function tail(track,state,metres=65){const begin=at(track,Math.max(0,(state.distance-metres)/track.total));if(!begin)return [];
  return [begin.point,...track.points.slice(begin.index+1,state.index+1),state.point];
 }
 return {build,at,visits,tail};
});
