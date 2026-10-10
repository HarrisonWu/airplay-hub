const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../../src/airplay_hub/templates/index.html'), 'utf8');
const source = html.match(/<script>([\s\S]*?)<\/script>/)[1]
  .replace('refresh();loadShelf();setInterval(refresh,1000);', '');

function page() {
  const elements = new Map();
  function element(id) {
    if (!elements.has(id)) elements.set(id, {
      textContent: '', innerHTML: '', style: {}, value: '',
      classList: {add(){}, remove(){}, toggle(){}, contains(){return false;}},
      addEventListener(){}, querySelectorAll(){return [];}
    });
    return elements.get(id);
  }
  const context = vm.createContext({
    document: {getElementById: element, activeElement: null},
    setTimeout(){return 0;}, clearTimeout(){}, setInterval(){},
    AbortController, console, fetch: async () => ({ok: true, json: async () => ({})})
  });
  vm.runInContext(source, context);
  return {context, element};
}

test('album attributes escape untrusted directory names', () => {
  const {context} = page();
  const hostile = 'Album" onclick="alert(1)';
  const card = context.albumCard(hostile, '', '', 'Album', '', '', 'play_album:' + hostile, '');
  assert.ok(card.includes('data-key="Album&quot; onclick=&quot;alert(1)"'));
  assert.ok(card.includes('data-act="play_album:Album&quot; onclick=&quot;alert(1)"'));
  assert.ok(!card.includes(' onclick="alert'));
});

test('shelf updates when tracks change within the same album', () => {
  const {context, element} = page();
  vm.runInContext('expanded.Album=true', context);
  const data = {cd: null, albums: [{dir: 'Album', tracks: [{n:1, title:'Old', file:'Album/1.flac'}]}]};
  context.renderShelf(data);
  assert.ok(element('shelf').innerHTML.includes('Old'));
  data.albums[0].tracks[0].title = 'New';
  context.renderShelf(data);
  assert.ok(element('shelf').innerHTML.includes('New'));
});

test('action failure is displayed without inserting HTML', async () => {
  const {context, element} = page();
  context.fetch = async () => ({ok:true, json:async () => ({rc:1,msg:'<b>MPD offline</b>'})});
  assert.equal(await context.api('/api/action', {action:'play'}), null);
  assert.equal(element('actionStatus').textContent, '⚠ <b>MPD offline</b>');
  assert.equal(element('actionStatus').innerHTML, '');
});

test('polling does not overlap and releases admission on failure', async () => {
  const {context} = page();
  let count=0, release;
  context.refreshStatus = async () => {count++; await new Promise(resolve => {release=resolve;});};
  const first = context.refresh();
  await context.refresh();
  assert.equal(count, 1);
  release(); await first;
  context.refreshStatus = async () => {throw new Error('offline');};
  await assert.rejects(context.refresh(), /offline/);
  context.refreshStatus = async () => {count++;};
  await context.refresh();
  assert.equal(count, 2);
});

test('zero volume remains zero', async () => {
  const {context, element} = page();
  context.fetch = async () => ({ok:true, json:async () => ({state:'stopped', volume:0})});
  await context.refresh();
  assert.equal(element('vol').value, 0);
});

test('offline status does not replace the last displayed volume', async () => {
  const {context, element} = page();
  element('vol').value = 25;
  context.fetch = async () => {throw new Error('offline');};
  await context.refresh();
  assert.equal(element('vol').value, 25);
  assert.equal(element('state').textContent, 'Disconnected');
});
