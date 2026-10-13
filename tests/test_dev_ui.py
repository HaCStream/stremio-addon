"""Execute the UI request helper against real Response objects in Node."""
from pathlib import Path
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
    assert "const config=await api('/api/auth-config')" in html
