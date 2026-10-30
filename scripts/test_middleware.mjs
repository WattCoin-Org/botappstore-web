import assert from 'node:assert/strict';
import mw, { wantsMarkdown, pageKey } from '../middleware.js';
const req=(path,accept,method='GET')=>new Request('https://x.test'+path,{method,headers:accept?{accept}:{}});
const MD='text/markdown';
const rw=r=>r.headers.get('x-middleware-rewrite');
const nx=r=>r.headers.get('x-middleware-next')==='1';
for (const [p,t] of [['/','/index.md'],['/index.html','/index.md'],['/bots/agent-council','/bots/agent-council.md'],['/bots/agent-council.html','/bots/agent-council.md'],['/about','/about.md'],['/contact.html','/contact.md'],['/privacy/','/privacy.md']]) {
  const r=mw(req(p,MD)); assert.equal(rw(r),'https://x.test'+t,p);
  assert.equal(r.headers.get('vary'),'Accept'); assert.match(r.headers.get('content-type'),/^text\/markdown/);
  assert(nx(mw(req(p,'text/html,application/xhtml+xml,*/*;q=0.8'))),p+' html'); assert(nx(mw(req(p))),p+' none'); assert(nx(mw(req(p,'*/*'))));
}
assert(wantsMarkdown('text/html;q=0.9, text/markdown'));assert(wantsMarkdown('TEXT/Markdown;q=0.5'));assert(!wantsMarkdown('text/markdown;q=0'));assert(!wantsMarkdown('text/html'));assert(!wantsMarkdown(null));
const nf=mw(req('/nope',MD)); assert.equal(nf.status,404); assert.equal(nf.headers.get('vary'),'Accept'); assert.match(nf.headers.get('content-type'),/markdown/);
const b=await nf.text(); assert(b.includes('/llms.txt')&&b.includes('/catalog.json'));
assert.equal(mw(req('/bots/zzz.html',MD)).status,404);
assert.equal((await mw(req('/x`\n#h',MD)).text()).includes('\n#h'),false);
for (const p of ['/catalog.json','/llms.txt','/runtime/foo.py','/runtime/foo','/styles.css','/index.md','/bots/agent-council.md','/og-card.png','/robots.txt']) assert(nx(mw(req(p,MD))),p);
assert(nx(mw(req('/',MD,'POST'))));
assert.equal(mw(req('/nope',MD,'HEAD')).status,404);
console.log('all middleware tests passed');
