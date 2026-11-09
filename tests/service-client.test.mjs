import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,writeFileSync,rmSync} from 'node:fs';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {createServer} from 'node:http';
import {once} from 'node:events';
import {serviceCall,readAdapterConfig} from '../lib/service-client.js';
import {register} from '../lib/host.js';
import {Config} from '../index.js';

const base=fileURLToPath(new URL('../',import.meta.url));
test('new plugin defaults to service, exposes no legacy worker or approval arguments',async()=>{
  assert.equal(Config({}).mode,'service');
  let tool,provider;
  const ctx={effect:f=>f(),tools:{register:value=>{tool=value;return()=>{};}},
    inject:(_names,fn)=>fn({effect:f=>f(),skills:{registerProvider:create=>{provider=create({signal:new AbortController().signal});return()=>{};}}})};
  register(ctx,{},base);
  const actions=tool.parameters.properties.action.enum;
  assert.ok(actions.includes('refresh_index'));assert.ok(!actions.includes('create'));assert.ok(!actions.includes('review_submit'));
  for(const field of ['source','command','token','confirm','model','budget'])assert.ok(!Object.hasOwn(tool.parameters.properties,field));
  assert.deepEqual((await provider.list()).map(x=>x.name),['dsh-literature-batch']);
  await assert.rejects(()=>tool.execute({action:'get_capabilities'},{}),/not installed/);
});

test('actual loopback transport uses provisioned role and rejects redirects',async()=>{
  const folder=mkdtempSync(join(base,'.test-output','service-client-'));const path=join(folder,'adapter.json');
  let redirect=false;let targetCalls=0;const received=[];
  const target=createServer((_req,res)=>{targetCalls++;res.end('{}');});target.listen(0,'127.0.0.1');await once(target,'listening');
  const server=createServer(async(req,res)=>{
    let raw='';for await(const chunk of req)raw+=chunk;
    received.push({path:req.url,auth:req.headers.authorization,body:JSON.parse(raw)});
    if(redirect){res.writeHead(307,{Location:'http://127.0.0.1:'+target.address().port+'/leak'});res.end();}
    else{res.writeHead(200,{'Content-Type':'application/json'});res.end(JSON.stringify({result:{productionReady:false}}));}
  });server.listen(0,'127.0.0.1');await once(server,'listening');
  try{
    writeFileSync(path,JSON.stringify({url:'http://127.0.0.1:'+server.address().port,dshToken:'test-only-token-'.repeat(4)}));
    assert.deepEqual(await serviceCall(path,'get_capabilities',{}),{productionReady:false});
    assert.equal(received[0].path,'/rpc');assert.equal(received[0].body.method,'get_capabilities');
    assert.equal(received[0].auth,'Bearer '+'test-only-token-'.repeat(4));
    redirect=true;await assert.rejects(()=>serviceCall(path,'get_capabilities',{}),/unavailable/);assert.equal(targetCalls,0);
    writeFileSync(path,JSON.stringify({url:'https://example.com',dshToken:'x'.repeat(40)}));
    assert.throws(()=>readAdapterConfig(path),/local-only/);
    writeFileSync(path,JSON.stringify({url:'http://127.0.0.1:1234',dshToken:'x'.repeat(40),confirmationToken:'y'.repeat(40)}));
    assert.throws(()=>readAdapterConfig(path),/local-only/);
  }finally{server.closeAllConnections();target.closeAllConnections();await Promise.all([new Promise(resolve=>server.close(resolve)),new Promise(resolve=>target.close(resolve))]);rmSync(folder,{recursive:true});}
});
