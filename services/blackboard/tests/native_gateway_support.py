"""Pinned standalone gateway and synthetic provider in an isolated Docker network."""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
from bbx_contracts.native_gateway import TRUSTED_ATTRIBUTE_HEADERS
from testcontainers.core.container import DockerContainer
from testcontainers.core.network import Network

IMAGE = (
    "cr.agentgateway.dev/agentgateway@sha256:"
    "60216aafb03d37f94e952ff3d51d2f630a65b10ecaebdc0a864882f92610c45b"
)

PROVIDER = r"""import json,os
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
calls=[]
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args): pass
 def do_GET(self):
  raw=json.dumps(calls).encode()
  self.send_response(200);self.send_header('content-length',str(len(raw)))
  self.end_headers();self.wfile.write(raw)
 def do_POST(self):
  assert self.headers.get('authorization')=='Bearer '+os.environ['PROVIDER_KEY']
  value=json.loads(self.rfile.read(int(self.headers['content-length'])))
  calls.append(value)
  self.send_response(200);self.send_header('content-type','text/event-stream');self.end_headers()
  def event(choices,usage=None):
   result={'id':'native-fake','object':'chat.completion.chunk','created':1,'model':value['model'],'choices':choices}
   if usage is not None:result['usage']=usage
   self.wfile.write(b'data: '+json.dumps(result).encode()+b'\n\n');self.wfile.flush()
  use_tool=value.get('tools') and not any(m.get('role')=='tool' for m in value['messages'])
  delta={'role':'assistant','content':'synthetic'}
  if use_tool:
   function={'name':value['tools'][0]['function']['name'],'arguments':'{}'}
   delta={'role':'assistant','tool_calls':[{'index':0,'id':'native-tool-call',
          'type':'function','function':function}]}
  event([{'index':0,'delta':delta,'finish_reason':None}])
  event([{'index':0,'delta':{},'finish_reason':'tool_calls' if use_tool else 'stop'}])
  if not any(message.get('content')=='no_usage' for message in value['messages']):
   event([],{'prompt_tokens':100,'completion_tokens':100,'total_tokens':200})
  self.wfile.write(b'data: [DONE]\n\n');self.wfile.flush()
ThreadingHTTPServer(('0.0.0.0',8000),Handler).serve_forever()
"""


@asynccontextmanager
async def native_gateway(directory):
    provider_key = "synthetic-provider-" + uuid4().hex
    directory.mkdir()
    config = {
        "config": {
            "adminAddr": "0.0.0.0:15000",
            "storage": {"mode": "hybrid"},
            "database": {"url": "sqlite:///probe/data.db?mode=rwc"},
            "modelCatalog": [{"file": "/probe/catalog.json"}],
        },
        "frontendPolicies": {
            "accessLog": {
                "database": {
                    "llm": "metadata",
                    "add": {
                        name: f'request.headers["{header}"]'
                        for name, header in TRUSTED_ATTRIBUTE_HEADERS.items()
                    },
                }
            }
        },
        "llm": {
            "port": 4000,
            "policies": {"apiKey": {"mode": "strict", "keys": []}},
            "models": [
                {
                    "name": "bounded-fake",
                    "provider": "openAI",
                    "params": {
                        "apiKey": provider_key,
                        "baseUrl": "http://native-fee-provider:8000/v1",
                    },
                }
            ],
        },
    }
    catalog = {
        "providers": {
            "openai": {
                "models": {
                    "bounded-fake": {
                        "limits": {"context": 16384, "output": 128},
                        "modalities": {"input": ["text"], "output": ["text"]},
                        "capabilities": {"reasoning": False, "toolCall": True},
                        "rates": {"input": "10.0", "output": "20.0"},
                    }
                }
            }
        }
    }
    (directory / "config.json").write_text(json.dumps(config))
    (directory / "catalog.json").write_text(json.dumps(catalog))
    (directory / "provider.py").write_text(PROVIDER)
    network = Network()
    await asyncio.to_thread(network.create)
    provider = (
        DockerContainer("python:3.12-slim")
        .with_name("bbx-native-provider-" + uuid4().hex[:8])
        .with_network(network)
        .with_network_aliases("native-fee-provider")
        .with_exposed_ports(8000)
        .with_volume_mapping(str(directory), "/probe", "ro")
        .with_env("PROVIDER_KEY", provider_key)
        .with_command("python /probe/provider.py")
    )
    container = (
        DockerContainer(IMAGE)
        .with_name("bbx-native-fees-" + uuid4().hex[:8])
        .with_network(network)
        .with_exposed_ports(4000, 15000)
        .with_volume_mapping(str(directory), "/probe", "rw")
        .with_kwargs(user=f"{os.getuid()}:{os.getgid()}")
        .with_command("-f /probe/config.json")
    )
    try:
        await asyncio.to_thread(provider.start)
        await asyncio.to_thread(container.start)
        admin = f"http://127.0.0.1:{container.get_exposed_port(15000)}"
        data = f"http://127.0.0.1:{container.get_exposed_port(4000)}"
        provider_url = f"http://127.0.0.1:{provider.get_exposed_port(8000)}"
        async with httpx.AsyncClient(trust_env=False, timeout=2) as http:
            async with asyncio.timeout(20):
                while True:
                    try:
                        response = await http.get(admin + "/api/config/resources/llm.apiKey")
                        await http.get(provider_url)
                        if response.is_success:
                            break
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(0.1)

            async def calls():
                return (await http.get(provider_url)).json()

            yield {"admin": admin, "data": data, "calls": calls, "container": container}
    finally:
        await asyncio.to_thread(container.stop)
        await asyncio.to_thread(provider.stop)
        await asyncio.to_thread(network.remove)
