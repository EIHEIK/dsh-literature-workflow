import Schema from '@deepseek-ai/schemastery';
import { existsSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname,join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { register } from './lib/host.js';

export const name='dsh-literature-workflow';
export const inject=['tools'];
const bundledPython=join(homedir(),'.dsh','dsh-runtimes','dsh-primary-runtime','dependencies','python',process.platform==='win32'?'python.exe':'bin/python3');
export const Config=Schema.object({
  mode:Schema.union(['service','legacy']).default('service').description('Use the installed background controller. Legacy is explicit compatibility only; no automatic fallback.'),
  controllerConfig:Schema.string().default('').description('Private DSH adapter JSON path containing only loopback URL and DSH credential, provisioned by the installer.'),
  dataDir:Schema.string().default(join(homedir(),'.dsh','storages','dsh-literature-workflow')).description('Durable jobs/output directory; separate from old production workflow.'),
  python:Schema.string().default(existsSync(bundledPython)?bundledPython:'python').description('Python 3.10+ with PyMuPDF. Prefer a dedicated virtual environment.'),
  engine:Schema.string().default('').description('Absolute path to pdf2zh_next executable.'),
  expectedEngineVersion:Schema.string().default('').description('Optional required version substring, pinned after first doctor probe.'),
  model:Schema.string().default('deepseek-flash'),
  allowApiCalls:Schema.boolean().default(false).description('Enable only for an authorized API translation test.'),
  allowZoteroWrite:Schema.boolean().default(false).description('Allow post-acceptance import of the two PDFs.'),
  requestLimit:Schema.number().min(1).max(10000).step(1).default(100).description('Hard maximum API attempts per worker attempt (not a currency budget).'),
  qps:Schema.number().min(0.1).max(10).default(1),
  apiConcurrency:Schema.number().min(1).max(8).step(1).default(2),
  timeoutSeconds:Schema.number().min(60).max(86400).step(1).default(3600),
  zoteroApi:Schema.string().default('http://127.0.0.1:23119/api'),
  zoteroMcp:Schema.string().default('http://127.0.0.1:23120/mcp')
});
export function apply(ctx,config){return register(ctx,config,dirname(fileURLToPath(import.meta.url)));}
