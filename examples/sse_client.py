"""Standard-library client for an already-running real gateway."""
import argparse,json,urllib.request

def main():
    p=argparse.ArgumentParser();p.add_argument('--endpoint',default='http://127.0.0.1:8000')
    p.add_argument('--prompt',default='Explain Prefill and Decode briefly.')
    p.add_argument('--request-id',default='demo-001');a=p.parse_args()
    body=json.dumps({'model':'pdserve-qwen','prompt':a.prompt,'max_tokens':64,'stream':True,'temperature':0}).encode()
    request=urllib.request.Request(a.endpoint+'/v1/completions',data=body,
        headers={'Content-Type':'application/json','X-Request-ID':a.request_id})
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request,timeout=180) as response:
        for line in response:print(line.decode().rstrip(),flush=True)
if __name__=='__main__':main()
