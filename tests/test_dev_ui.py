"""Execute the UI request helper against real Response objects in Node."""
from pathlib import Path
import json
import shutil
import subprocess

import pytest


def test_dev_ui_handles_json_and_non_json_responses():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required to execute the Developer UI JavaScript')
    html = (Path(__file__).parents[1] / 'stremio_addon' / 'debug.html').read_text()
    helper = html.split('async function api(', 1)[1].split('\nfunction fmtBytes', 1)[0]
    script = """
const assert=require('node:assert/strict');
const ingressUrl=path=>path;
const apiKey='test-key';
""" + 'async function api(' + helper + """
(async()=>{
  let response;
  global.fetch=async(url, options)=>{
    assert.equal(options.headers['X-Debug-Key'],'test-key');
    return response;
  };
  response=new Response('{"videos":42}',{status:200});
  assert.deepEqual(await api('/api/overview'),{videos:42});
  response=new Response('Internal Server Error',{status:500});
  await assert.rejects(api('/api/overview?secret=hidden'),error=>{
    assert.match(error.message,/Server error.*HTTP 500, \\/api\\/overview/);
    assert.ok(!error.message.includes('hidden'));
    assert.ok(!error.message.includes('Unexpected token'));
    return true;
  });
  response=new Response('<html>proxy failed</html>',{status:502});
  await assert.rejects(api('/api/auth-config'),/Server error.*HTTP 502/);
  response=new Response('',{status:503});
  await assert.rejects(api('/api/channels'),/Server error.*HTTP 503/);
  response=new Response('<html>login</html>',{status:200});
  await assert.rejects(api('/api/overview'),/Invalid JSON response.*HTTP 200/);
  response=new Response('{"detail":"AI failed","gemini":{"sent":true}}',{status:502});
  await assert.rejects(api('/api/search'),error=>{
    assert.match(error.message,/AI failed.*HTTP 502/);
    assert.deepEqual(error.data.gemini,{sent:true});
    return true;
  });
  response=new Response('null',{status:500});
  await assert.rejects(api('/api/overview'),/Request failed.*HTTP 500/);
  response=new Response('Unauthorized',{status:401});
  await assert.rejects(api('/api/overview'),/Invalid Developer UI password/);
})().catch(error=>{console.error(error);process.exitCode=1});
"""
    subprocess.run([node, '-e', script], check=True, capture_output=True, text=True)
    assert "api('/api/auth-config')" not in html


@pytest.mark.parametrize('skip_auth', [True, False])
def test_dashboard_failure_does_not_change_auth_mode(skip_auth):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required to execute the Developer UI JavaScript')
    html = (Path(__file__).parents[1] / 'stremio_addon' / 'debug.html').read_text()
    startup = 'async function start()' + html.split('async function start()', 1)[1].split('\nstart();', 1)[0]
    script = r"""
const assert=require('node:assert/strict');
const skipAuth=SKIP_AUTH;
let apiKey=skipAuth?'':'valid-key',loads=0,storageWrites=0;
const nodes=new Map();
for(const id of ['#load-error','#load-error-message','#load-retry','#app','#login','#login-error']){
  const classes=new Set(id==='#login'?['hidden']:[]);
  nodes.set(id,{textContent:'',classList:{add:c=>classes.add(c),remove:c=>classes.delete(c),contains:c=>classes.has(c)}});
}
const document={querySelector:id=>nodes.get(id)};
const storedKey=()=>storageWrites++;
let failure=503;
async function load(){loads++;if(failure)throw Object.assign(Error('Request failed'),{status:failure})}
""" + startup + r"""
(async()=>{
  for(const status of [503,401]){
    failure=status;
    await start();
    if(!skipAuth&&status===401){
      assert.equal(apiKey,'');assert.equal(storageWrites,1);
      assert.ok(!nodes.get('#login').classList.contains('hidden'));
    }else{
      assert.ok(nodes.get('#login').classList.contains('hidden'));
      assert.ok(!nodes.get('#app').classList.contains('hidden'));
      assert.ok(!nodes.get('#load-error').classList.contains('hidden'));
      assert.equal(storageWrites,0);
    }
  }
  if(skipAuth){failure=0;await nodes.get('#load-retry').onclick();assert.equal(loads,3);
    assert.ok(nodes.get('#load-error').classList.contains('hidden'))}
})().catch(error=>{console.error(error);process.exitCode=1});
"""
    script = script.replace('SKIP_AUTH', json.dumps(skip_auth))
    subprocess.run([node, '-e', script], check=True, capture_output=True, text=True)


def test_channel_queue_renders_zero_and_unknown():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required to execute the Developer UI JavaScript')
    html = (Path(__file__).parents[1] / 'stremio_addon' / 'debug.html').read_text()
    render = 'function renderChannels(' + html.split('function renderChannels(', 1)[1].split('\nfunction renderActivity', 1)[0]
    script = r"""
const assert=require('node:assert/strict');
const output={innerHTML:''};
const document={querySelector:()=>output};
const esc=v=>String(v??'');
const phaseLabel=v=>v;
""" + render + r"""
renderChannels([{id:1,name:'Group',videos:42,queue:0,complete:true,phase:'ready'}]);
assert.match(output.innerHTML,/<td>42<\/td><td>0<\/td>/);
renderChannels([{id:1,name:'Group',videos:42,queue:null,phase:'checking'}]);
assert.match(output.innerHTML,/<td>Checking…<\/td>/);
renderChannels([]);
assert.match(output.innerHTML,/colspan="5"/);
"""
    subprocess.run([node, '-e', script], check=True, capture_output=True, text=True)
    assert '<th>Newest</th>' not in html
    assert '<th>Oldest</th>' not in html
    assert 'x.newest_message' not in html


def test_refresh_icon_stays_disabled_through_success_delay_and_recovers_on_error():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required to execute the Developer UI JavaScript')
    html = (Path(__file__).parents[1] / 'stremio_addon' / 'debug.html').read_text()
    handler = html.split("document.querySelector('#refresh').onclick=", 1)[1].split("document.querySelector('#sync').onclick=", 1)[0]
    script = r"""
const assert=require('node:assert/strict');
const classes=new Set();
const button={disabled:false,classList:{add:(...xs)=>xs.forEach(x=>classes.add(x)),remove:(...xs)=>xs.forEach(x=>classes.delete(x))},setAttribute(k,v){this[k]=v}};
const status={};
const document={querySelector:s=>s==='#refresh'?button:s==='#refresh-status'?status:{classList:{contains:()=>true}}};
let finishLoad,finishDelay,delay,loads=0,tmdbLoads=0;
let load=()=>{loads++;return new Promise(resolve=>finishLoad=resolve)};
const loadTMDB=async()=>{tmdbLoads++};
const setTimeout=(callback,ms)=>{finishDelay=callback;delay=ms};
const handler=HANDLER
(async()=>{
 const pending=handler();
 assert.equal(button.disabled,true);assert.ok(classes.has('refreshing'));
 await handler();assert.equal(loads,1);
 finishLoad();
 for(let i=0;i<5;i++)await Promise.resolve();
 assert.equal(tmdbLoads,1);assert.equal(button.disabled,true);
 assert.ok(!classes.has('refreshing'));assert.ok(classes.has('success'));
 assert.equal(delay,3000);await handler();assert.equal(loads,1);
 finishDelay();await pending;
 assert.equal(button.disabled,false);assert.equal(classes.size,0);
 assert.equal(button['aria-label'],'Refresh dashboard');
 load=async()=>{throw new Error('Server unavailable')};
 await handler();assert.equal(button.disabled,false);assert.equal(classes.size,0);
 assert.equal(status.textContent,'Server unavailable');assert.equal(status.className,'bad');
})().catch(error=>{console.error(error);process.exitCode=1});
""".replace('HANDLER', handler)
    subprocess.run([node, '-e', script], check=True, capture_output=True, text=True)



def test_sync_can_restart_and_old_poller_does_not_overwrite_new_status():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required to execute the Developer UI JavaScript')
    html = (Path(__file__).parents[1] / 'stremio_addon' / 'debug.html').read_text()
    handler = html.split("document.querySelector('#sync').onclick=", 1)[1].split("document.querySelectorAll('.tab')", 1)[0]
    script = r"""
const assert=require('node:assert/strict');
let syncAttempt=0,posts=0;
const button={disabled:false},status={},timers=[];
const document={querySelector:s=>s==='#sync'?button:status};
const setTimeout=callback=>timers.push(callback);
let api=async(path,options)=>({sync:options?{id:String(++posts),state:'queued'}:{id:String(posts),state:'done'}});
const load=async()=>{};
const handler=HANDLER
(async()=>{
 const first=handler();await Promise.resolve();
 assert.equal(button.disabled,false);
 const second=handler();await Promise.resolve();
 assert.equal(posts,2);assert.equal(button.disabled,false);
 timers.shift()();await first;
 assert.equal(status.textContent,'Syncing...');assert.equal(status.className,'muted');
 timers.shift()();await second;
 assert.equal(status.textContent,'Sync done!');assert.equal(status.className,'ok');
 assert.equal(button.disabled,false);
})().catch(error=>{console.error(error);process.exitCode=1});
""".replace('HANDLER', handler)
    subprocess.run([node, '-e', script], check=True, capture_output=True, text=True)


def test_download_controls_follow_job_state_and_escape_metadata():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required to execute the Developer UI JavaScript')
    html = (Path(__file__).parents[1] / 'stremio_addon' / 'debug.html').read_text()
    controls = 'function actionIcon(' + html.split('function actionIcon(', 1)[1].split('\nfunction renderSearchResults', 1)[0]
    esc = 'const esc=' + html.split('const esc=', 1)[1].split('\nconst ingressUrl', 1)[0]
    script = "const assert=require('node:assert/strict');\n" + esc + r'''
const fmtBytes=n=>n+' B',busyDownloads=new Set(),pendingDeletions=new Map();
let downloadRows=[];
''' + controls + r'''
const source={id:'tg:1:"<script>'};
let result=downloadControls(source);
assert.ok(result.includes('aria-label="Download"'));
assert.ok(!result.includes('<script>'));
const job={id:'job',source_id:source.id,status:'downloading',downloaded:5,size:10};
downloadRows=[job];
result=downloadControls(source);assert.ok(result.includes('aria-label="Pause"'));
assert.ok(result.includes('5 B / 10 B'));assert.ok(result.includes('Stop &amp; delete'));
job.status='paused';result=downloadControls(source);assert.ok(result.includes('aria-label="Resume"'));
job.status='failed';job.error='<script>error</script>';
result=downloadControls(source);assert.ok(result.includes('aria-label="Resume"'));
assert.ok(!result.includes('<script>error'));
job.status='completed';result=downloadControls(source);assert.ok(result.includes('aria-label="Delete"'));
assert.ok(!result.includes('aria-label="Resume"'));assert.ok(!result.includes('aria-label="Pause"'));
job.status='downloading';busyDownloads.add('job');
assert.ok(downloadControls(source).includes('disabled'));
pendingDeletions.set('job',job);result=downloadControls(source);
assert.ok(result.includes('deleting-icon'));assert.ok(result.includes('aria-busy="true"'));assert.ok(result.includes('Deleting video'));
'''
    subprocess.run([node, '-e', script], check=True, capture_output=True, text=True)
    search = html.split('<section id="search"', 1)[1].split('</section>', 1)[0]
    saved = html.split('<section id="downloads"', 1)[1].split('</section>', 1)[0]
    ai = html.split('<section id="ai"', 1)[1].split('</section>', 1)[0]
    assert 'Streaming may slow down while downloads are in progress.' in search
    assert 'debug Stremio / Nuvio search results' not in search
    assert 'ai-search-form' not in search
    assert 'ai-search-form' in ai and 'gemini-exchange' in ai
    assert 'search-form' not in saved
    assert 'Select all' in saved and 'Clear selection' in saved and 'Search for videos' in saved


def test_search_group_counts_toggle_from_icon_click():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required to execute the Developer UI JavaScript')
    html = (Path(__file__).parents[1] / 'stremio_addon' / 'debug.html').read_text()
    toggle = "document.querySelector('#search-summary').addEventListener('click',event=>{" + html.split(
        "document.querySelector('#search-summary').addEventListener('click',event=>{", 1)[1].split(
        "document.querySelector('#search-form').onsubmit=", 1)[0]
    script = r'''
const assert=require('node:assert/strict');
let handler,hidden=true;
const attributes={'aria-expanded':'false'};
const button={getAttribute:name=>attributes[name],setAttribute:(name,value)=>attributes[name]=value};
const document={querySelector:selector=>selector==='#search-summary'?
  {addEventListener:(name,fn)=>handler=fn}:{classList:{toggle:(name,value)=>hidden=value}}};
''' + toggle + r'''
const iconClick={target:{closest:selector=>button}};
handler(iconClick);
assert.equal(hidden,false);assert.equal(attributes['aria-expanded'],'true');
assert.equal(attributes['aria-label'],'Hide results by group');
handler(iconClick);
assert.equal(hidden,true);assert.equal(attributes['aria-expanded'],'false');
assert.equal(attributes['aria-label'],'Show results by group');
handler({target:{closest:()=>null}});
assert.equal(hidden,true);
'''
    subprocess.run([node, '-e', script], check=True, capture_output=True, text=True)


def test_search_sorting_and_combined_size_group_filters():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required to execute the Developer UI JavaScript')
    html = (Path(__file__).parents[1] / 'stremio_addon' / 'debug.html').read_text()
    filtering = 'function filteredSearchRows()' + html.split('function filteredSearchRows()', 1)[1].split(
        'function resetSearchFilters()', 1)[0]
    script = r'''
const assert=require('node:assert/strict');
const searchRows=[
  {id:'a',title:'Zulu',caption:'Bravo',channel:'One',channel_id:1,size:10},
  {id:'b',title:'Alpha',caption:'Charlie',channel:'Two',channel_id:2,size:2},
  {id:'c',title:'Beta',caption:'Alpha',channel:'One',channel_id:1,size:100}
];
let searchSort={key:null,direction:1},searchSize={min:0,max:100},selectedSearchGroups=new Set(['1','2']);
''' + filtering + r'''
const ids=()=>filteredSearchRows().map(row=>row.id);
assert.deepEqual(ids(),['a','b','c']);
for(const [key,expected] of [['title',['b','c','a']],['caption',['c','a','b']],['channel',['a','c','b']],['size',['b','a','c']]]){
  searchSort={key,direction:1};assert.deepEqual(ids(),expected);
  searchSort.direction=-1;
  if(key!=='channel')assert.deepEqual(ids(),[...expected].reverse());
}
searchSort={key:'size',direction:1};searchSize={min:10,max:100};
selectedSearchGroups=new Set(['1']);assert.deepEqual(ids(),['a','c']);
searchSize.max=99;assert.deepEqual(ids(),['a']);
searchSize.min=10;searchSize.max=10;assert.deepEqual(ids(),['a']);
selectedSearchGroups.clear();assert.deepEqual(ids(),[]);
assert.deepEqual(searchRows.map(row=>row.id),['a','b','c']);
'''
    subprocess.run([node, '-e', script], check=True, capture_output=True, text=True)



def test_dev_ui_pagination_serializes_requests_and_stops_on_warning():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required')
    html = (Path(__file__).parents[1] / 'stremio_addon' / 'debug.html').read_text()
    busy = 'function setSearchBusy(' + html.split('function setSearchBusy(', 1)[1].split('function appendSearchPage(', 1)[0]
    fetcher = 'async function fetchSearchPages(' + html.split('async function fetchSearchPages(', 1)[1].split("document.querySelector('#load-more-results')", 1)[0]
    script = r"""
const assert=require('node:assert/strict');
const nodes=new Map();
const document={querySelector:id=>{if(!nodes.has(id))nodes.set(id,{disabled:false,textContent:'',setAttribute(){}});return nodes.get(id)}};
let searchCursor=null,searchParams={q:'test',mode:'text'},searchBusy=false,searchHasMore=true,searchRows=[];
let calls=0,resolvePage;
let api=async()=>{calls++;return await new Promise(resolve=>resolvePage=resolve)};
function appendSearchPage(d){searchRows.push(...d.results);searchCursor=d.cursor;searchHasMore=d.has_more}
""" + busy + fetcher + r"""
(async()=>{
 const running=fetchSearchPages(true);
 assert.ok(searchBusy);
 assert.ok(document.querySelector('#load-more-results').disabled);
 assert.ok(document.querySelector('#load-all-results').disabled);
 await fetchSearchPages();assert.equal(calls,1);
 resolvePage({results:[{id:'1'}],cursor:'next',has_more:true});
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(calls,2);assert.ok(searchBusy);
 resolvePage({results:[],cursor:'retry',has_more:true,warning:'retry in 10 seconds'});
 await running;
 assert.equal(calls,2);assert.equal(searchRows.length,1);assert.equal(searchCursor,'retry');
 assert.ok(!searchBusy);assert.ok(!document.querySelector('#load-more-results').disabled);
 assert.equal(document.querySelector('#search-page-status').textContent,'retry in 10 seconds');
 api=async url=>{assert.ok(url.includes('cursor=retry'));return {results:[{id:'2'}],cursor:null,has_more:false}};
 await fetchSearchPages();
 assert.equal(searchRows.length,2);assert.ok(document.querySelector('#load-all-results').disabled);
 searchHasMore=true;api=async()=>{throw Error('network error')};
 await fetchSearchPages();assert.ok(!searchBusy);assert.equal(searchRows.length,2);
 assert.ok(!document.querySelector('#load-more-results').disabled);
})().catch(error=>{console.error(error);process.exitCode=1});
"""
    subprocess.run([node, '-e', script], check=True, capture_output=True, text=True)


def test_appending_search_pages_preserves_active_filters_sort_and_scroll():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required')
    html = (Path(__file__).parents[1] / 'stremio_addon' / 'debug.html').read_text()
    filters = 'function resetSearchFilters(' + html.split('function resetSearchFilters(', 1)[1].split('function renderGroupPicker(', 1)[0]
    append = 'function appendSearchPage(' + html.split('function appendSearchPage(', 1)[1].split('async function fetchSearchPages(', 1)[0]
    script = r"""
const assert=require('node:assert/strict');
const nodes=new Map();
const document={querySelector:id=>{if(!nodes.has(id))nodes.set(id,{textContent:'',innerHTML:'',scrollTop:90,scrollLeft:40,classList:{remove(){}},getAttribute(){return 'false'}});return nodes.get(id)}};
const esc=String,fmtBytes=String,actionButton=()=>'',renderSearchResults=()=>{};
let searchRows=[],searchGroups=[],selectedSearchGroups=new Set(),searchSize={min:0,max:0},searchCursor=null,searchHasMore=false;
let searchSort={key:'size',direction:-1};
""" + filters + append + r"""
const page=results=>({results,cursor:'next',has_more:true,channels_queried:[],normalized_query:'test'});
appendSearchPage(page([{id:'1',size:10,channel_id:1},{id:'2',size:20,channel_id:2}]));
assert.equal(selectedSearchGroups.size,2);assert.deepEqual(searchSize,{min:10,max:20});
appendSearchPage(page([{id:'3',size:100,channel_id:3}]));
assert.equal(selectedSearchGroups.size,3);assert.equal(searchSize.max,100);
selectedSearchGroups=new Set(['1']);searchSize={min:12,max:18};
appendSearchPage(page([{id:'4',size:200,channel_id:4},{id:'3',size:100,channel_id:3}]));
assert.deepEqual([...selectedSearchGroups],['1']);assert.deepEqual(searchSize,{min:12,max:18});
assert.equal(searchRows.length,4);assert.deepEqual(searchSort,{key:'size',direction:-1});
assert.equal(document.querySelector('#results').scrollTop,90);assert.equal(document.querySelector('#results').scrollLeft,40);
"""
    subprocess.run([node, '-e', script], check=True, capture_output=True, text=True)
