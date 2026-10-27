import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const events = {}, deleted = [], navigated = [];
const source = fs.readFileSync(new URL('../ui-controls/sw.js', import.meta.url), 'utf8');
const makeClient = url => ({ url, navigate(next) { navigated.push(next); return new Promise(() => {}); } });
const self = {
  location: { origin: 'http://127.0.0.1:8793' },
  addEventListener(name, fn) { events[name] = fn; },
  skipWaiting: async () => {},
  clients: { claim: async () => {}, matchAll: async () => [
    makeClient('http://127.0.0.1:8793/?c390-managed=0.2.0'),
    makeClient('http://127.0.0.1:8793/#/chat/old-draft'),
  ] },
};
vm.runInNewContext(source, { self, URL, caches: {
  keys: async () => ['workbox-precache-v2-test', 'api-cache', 'unrelated-cache'],
  delete: async key => { deleted.push(key); },
} });
let activation;
events.activate({ waitUntil(promise) { activation = promise; } });
let timer;
try {
  await Promise.race([activation, new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error('Activation must not await navigation')), 500);
  })]);
} finally { clearTimeout(timer); }
assert.deepEqual(deleted, ['workbox-precache-v2-test', 'api-cache']);
assert.equal(navigated.length, 1);
assert.equal(new URL(navigated[0]).searchParams.get('c390-sw'), '0.2.0');
console.log('Service-worker activation completes without navigation deadlock; unrelated cache and old draft tab preserved.');
