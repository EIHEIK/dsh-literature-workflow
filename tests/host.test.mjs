import test from 'node:test';
import assert from 'node:assert/strict';
import {actorOf,publicValue,register} from '../lib/host.js';
import {fileURLToPath} from 'node:url';
const base=fileURLToPath(new URL('../',import.meta.url));
test('review identity cannot be supplied via tool arguments',()=>{
  assert.throws(()=>actorOf({}),/real session/);
  assert.equal(actorOf({agent:{session:{id:'session-actual'}}}),'session-actual');
});
test('lease is not exposed in tool output',()=>assert.deepEqual(publicValue({lease:'secret',nested:{lease:'x',x:1}}),{nested:{x:1}}));
test('tool and bundled skills register using documented host interfaces',async()=>{
  let tool,provider;
  const scope={skills:{registerProvider:p=>{provider=p;return()=>{};}},effect:f=>f()};
  const ctx={tools:{register:t=>{tool=t;return()=>{};}},effect:f=>f(),inject:(_deps,fn)=>fn(scope)};
  register(ctx,{allowApiCalls:false,allowZoteroWrite:false},base);
  assert.equal(tool.name,'literature_workflow');assert.equal((await provider.list()).length,3);
  const item=(await provider.list())[0];assert.match((await provider.get(item)).content,/Zotero/);
  assert.equal(await provider.get({locator:'/etc/passwd'}),undefined);
  assert.equal(tool.parameters.properties.action.enum.includes('claim'),false);
  await assert.rejects(()=>tool.execute({action:'start',job:'x'},{agent:{session:{id:'a'}}}),/disabled/);
  await assert.rejects(()=>tool.execute({action:'archive',job:'x'},{agent:{session:{id:'a'}}}),/disabled/);
});
