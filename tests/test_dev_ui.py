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
  await assert.rejects(api('/api/overview'),/Invalid API key/);
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
