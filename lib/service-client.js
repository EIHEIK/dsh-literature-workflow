import {readFileSync} from 'node:fs';

const operations=['get_capabilities','refresh_index','get_index_status','prepare_batch','start_batch',
  'get_status','read_events','pause_batch','resume_batch','cancel_batch','get_delivery'];

export function readAdapterConfig(path) {
  if(typeof path!=='string'||!path.trim())throw new Error('Local controller adapter configuration is not installed. No legacy fallback is allowed.');
  const raw=readFileSync(path,'utf8');
  if(raw.length>16384)throw new Error('Invalid adapter configuration');
  const config=JSON.parse(raw);const url=new URL(config.url);
  if(Object.keys(config).some(key=>!['url','dshToken'].includes(key))||url.protocol!=='http:'||
     url.hostname!=='127.0.0.1'||!url.port||url.username||url.password||url.search||url.hash||url.pathname!=='/'||
     typeof config.dshToken!=='string'||config.dshToken.length<40)throw new Error('Invalid local-only adapter configuration');
  return {url:url.origin,token:config.dshToken};
}

export async function serviceCall(configPath,method,args,{signal,transport=fetch}={}) {
  if(!operations.includes(method))throw new Error('Unsupported controller operation');
  const config=readAdapterConfig(configPath);
  const body=JSON.stringify({method,arguments:args});
  if(Buffer.byteLength(body)>65536)throw new Error('Request exceeds controller limit');
  const signals=[AbortSignal.timeout(40000)];if(signal)signals.push(signal);
  let response;
  try {
    response=await transport(config.url+'/rpc',{method:'POST',redirect:'error',signal:AbortSignal.any(signals),
      headers:{'Content-Type':'application/json','Authorization':'Bearer '+config.token},body});
    const reader=response.body.getReader();let size=0;const chunks=[];
    while(true){const {done,value}=await reader.read();if(done)break;size+=value.byteLength;
      if(size>2*1024*1024){await reader.cancel();throw new Error('Response too large');}chunks.push(Buffer.from(value));}
    const result=JSON.parse(Buffer.concat(chunks).toString('utf8'));
    if(!response.ok||!Object.hasOwn(result,'result'))throw new Error('Controller rejected request');
    return result.result;
  } catch {
    // Do not expose connection config, headers, server errors or response text.
    // A timeout does not prove the controller rejected the queued operation.
    throw new Error('Controller unavailable or request rejected. Inspect the existing batch/index before retrying; retain its request ID.');
  }
}

export function registerServiceTool(ctx,config) {
  const string={type:'string'};
  ctx.effect(()=>ctx.tools.register({
    name:'literature_workflow',
    description:'Submit and supervise Zotero translation through the installed local background controller. Read dsh-literature-batch first. refresh_index is unpaid; start requires deployment readiness and an existing trusted user confirmation. No arbitrary paths, shell, model settings or self-approval.',
    parameters:{type:'object',properties:{action:{type:'string',enum:operations},
      scope:{type:'object',properties:{kind:{enum:['items','collection']},keys:{type:'array',items:{type:'string',pattern:'^[A-Z0-9]{8}$'},minItems:1,maxItems:100}},required:['kind','keys'],additionalProperties:false},
      requestId:{type:'string',pattern:'^[a-zA-Z0-9_-]{16,64}$'},index:string,batch:string,summaryHash:string,
      pages:string,title:string,after:{type:'integer',minimum:0},limit:{type:'integer',minimum:1,maximum:100}},
      required:['action'],additionalProperties:false},
    output:{schema:{type:'object'},render:(_args,value)=>[{type:'text',text:JSON.stringify(value,null,2)}]},
    timeoutMs:45000,isConcurrencySafe:()=>false,
    async execute(args,exec){const {action,...arguments_}=args;return serviceCall(config.controllerConfig,action,arguments_,{signal:exec?.signal});}
  }));
}
