async function u(r,i){const c=r.body.getReader(),d=new TextDecoder;let e="";for(;;){const{done:s,value:o}=await c.read();if(s)break;e+=d.decode(o,{stream:!0});let t;for(;(t=e.indexOf(`

`))!==-1;){const l=e.slice(0,t);e=e.slice(t+2);const n=l.split(`
`).find(f=>f.startsWith("data:"));if(!n)continue;let a;try{a=JSON.parse(n.slice(5).trim())}catch{continue}i(a)}}}export{u as r};
