// Exercise the actual extracted DSH registerProvider implementation; surrounding
// scope storage is simulated. This is not a live DSH install/cold-start test.
import {readFileSync} from 'node:fs';
import {createHash} from 'node:crypto';
import {fileURLToPath} from 'node:url';
import assert from 'node:assert/strict';
import {register} from '../lib/host.js';

const source=readFileSync(process.argv[2],'utf8');
const start=source.indexOf('registerProvider(create) {');
const end=source.indexOf('\n\t/**',start);
assert(start>=0&&end>start,'Expected actual host factory implementation');
const actual=new Function('RUNTIME_PROVIDER','return ({'+source.slice(start,end).trim()+'}).registerProvider;')('runtime');
const entries=new Map();
let invalidations=0;
const layer={providers:{
  get:name=>entries.get(name),
  insert:(name,value)=>{assert(!entries.has(name));entries.set(name,value);return()=>entries.delete(name);}
}};
const registry={registerProvider:actual,nextProviderOrder:0,ctx:{},
  invalidateCache:()=>invalidations++,layers:{effect:(_ctx,run)=>run(layer)}};
const disposers=[];
const scope={skills:registry,effect:run=>{const dispose=run();disposers.push(dispose);return dispose;}};
const ctx={tools:{register:()=>()=>{}},effect:run=>run(),inject:(_deps,run)=>run(scope)};
register(ctx,{},fileURLToPath(new URL('../',import.meta.url)));
const provider=entries.get('dsh-literature-workflow').provider;
const items=await provider.list();
assert.deepEqual(items.map(item=>item.name),['dsh-literature-batch']);
for(const item of items)assert((await provider.get(item)).content.length>100);
assert.equal(await provider.get({locator:'C:/not-a-bundled-skill'}),undefined);
for(const dispose of disposers)dispose();
assert.equal(entries.size,0);
await assert.rejects(()=>provider.list(),/disposed/);
console.log(JSON.stringify({status:'passed',sourceSha256:createHash('sha256').update(source).digest('hex'),
  skills:items.map(x=>x.name),disposalVerified:true,
  boundary:'Actual extracted registerProvider method; surrounding host simulated; live installation not tested.'}));
