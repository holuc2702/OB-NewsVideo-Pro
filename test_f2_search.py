import asyncio
from f2.apps.douyin.handler import DouyinHandler
import browser_cookie3
import os
import subprocess
import json

def get_comet_cookie_string():
    db_path = os.path.expanduser('~/Library/Application Support/Comet/Default/Cookies')
    original_get = browser_cookie3._get_osx_keychain_password
    cmd = ['/usr/bin/security', '-q', 'find-generic-password', '-w', '-a', 'Comet', '-s', 'Comet Safe Storage']
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, _ = proc.communicate()
    comet_pw = out.strip().decode('utf-8') if proc.returncode == 0 else ''
    browser_cookie3._get_osx_keychain_password = lambda s, u: comet_pw
    
    class Comet(browser_cookie3.ChromiumBased):
        def __init__(self, **kwargs):
            super().__init__(browser='Chrome', cookie_file=db_path, domain_name='douyin.com', key_file=None, **kwargs)
    
    cj = Comet().load()
    browser_cookie3._get_osx_keychain_password = original_get
    
    return "; ".join([f"{c.name}={c.value}" for c in cj])

async def main():
    kwargs = {
        "cookie": get_comet_cookie_string(),
        "headers": {}
    }
    handler = DouyinHandler(kwargs)
    try:
        # What is the search method?
        print([m for m in dir(handler) if "search" in m])
    except Exception as e:
        print("Error:", e)

if __name__ == "__main__":
    asyncio.run(main())
