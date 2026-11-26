/* Exact bounded discrete Frechet with the Python implementation's tie order. */
#include <math.h>
#include <stdlib.h>
#include <stdint.h>
static double dist(const double *a,const double *b){
 const double rad=0.017453292519943295;
 return 6371000.0*hypot((a[0]-b[0])*rad*cos((a[1]+b[1])*rad/2),(a[1]-b[1])*rad);
}
int route_frechet(const double *a,int n,const double *b,int m,double *score,double *residuals){
 if(n<1||m<1||(long)n*m>1500000)return 1;
 double *prev=malloc((size_t)m*sizeof(double)),*curr=malloc((size_t)m*sizeof(double));
 uint8_t *parents=malloc((size_t)n*m);
 if(!prev||!curr||!parents){free(prev);free(curr);free(parents);return 2;}
 for(int i=0;i<n;i++){
  for(int j=0;j<m;j++){
   double best=INFINITY;uint8_t direction=0;
   if(i==0&&j==0)best=0;
   else{
    if(i&&j){best=prev[j-1];direction=0;}
    if(i&&prev[j]<best){best=prev[j];direction=1;}
    if(j&&curr[j-1]<best){best=curr[j-1];direction=2;}
   }
   curr[j]=fmax(best,dist(a+2*i,b+2*j));parents[(size_t)i*m+j]=direction;
  }
  double *swap=prev;prev=curr;curr=swap;
 }
 *score=prev[m-1];for(int i=0;i<n;i++)residuals[i]=0;
 int i=n-1,j=m-1;
 for(;;){
  residuals[i]=fmax(residuals[i],dist(a+2*i,b+2*j));
  if(i==0&&j==0)break;
  uint8_t d=parents[(size_t)i*m+j];if(d!=2)i--;if(d!=1)j--;
 }
 free(prev);free(curr);free(parents);return 0;
}
static double segment_distance(const double *p,const double *a,const double *b){
 const double rad=0.017453292519943295,c=cos(p[1]*rad);
 double x=(b[0]-a[0])*c,y=b[1]-a[1],norm=x*x+y*y;
 double t=norm?(((p[0]-a[0])*c*x+(p[1]-a[1])*y)/norm):0;
 t=fmax(0,fmin(1,t));double q[2]={a[0]+t*(b[0]-a[0]),a[1]+t*(b[1]-a[1])};return dist(p,q);
}
/* Exact nearest-segment distance. Conservative AABB bounds skip only segments
   that mathematically cannot improve the current distance; loops stay intact. */
int route_corridor(const double *p,int n,const double *r,int m,double *out){
 if(n<0||m<2||n>100000||m>100000)return 1;
 double maxlat=0;for(int i=0;i<n;i++){double v=fabs(p[2*i+1]);if(v>maxlat)maxlat=v;}
 for(int j=0;j<m;j++){double v=fabs(r[2*j+1]);if(v>maxlat)maxlat=v;}
 const double scale=6371000.0*0.017453292519943295,c=cos(maxlat*0.017453292519943295);
 double *bounds=malloc((size_t)(m-1)*4*sizeof(double));if(!bounds)return 2;
 for(int j=0;j<m-1;j++){
  const double *a=r+2*j,*b=r+2*(j+1);double *v=bounds+4*j;
  v[0]=(a[0]<b[0]?a[0]:b[0])*scale*c;v[1]=(a[0]>b[0]?a[0]:b[0])*scale*c;
  v[2]=(a[1]<b[1]?a[1]:b[1])*scale;v[3]=(a[1]>b[1]?a[1]:b[1])*scale;
 }
 int hint=0;
 for(int i=0;i<n;i++){
  const double *q=p+2*i;double qx=q[0]*scale*c,qy=q[1]*scale;
  double best=segment_distance(q,r+2*hint,r+2*(hint+1)),best2=best*best;int winner=hint;
  for(int j=0;j<m-1;j++){
   const double *v=bounds+4*j;
   double dx=qx<v[0]?v[0]-qx:qx>v[1]?qx-v[1]:0;
   if(dx*dx>best2+1e-6)continue;
   double dy=qy<v[2]?v[2]-qy:qy>v[3]?qy-v[3]:0;
   if(dx*dx+dy*dy>best2+1e-6)continue;
   double d=segment_distance(q,r+2*j,r+2*(j+1));if(d<best){best=d;best2=best*best;winner=j;}
  }
  out[i]=best;hint=winner;
 }
 free(bounds);return 0;
}
