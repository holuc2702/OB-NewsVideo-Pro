import asyncio
from f2.apps.douyin.handler import DouyinHandler
import browser_cookie3
import os
import subprocess

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
    vid = "7387342082260651275"
    kwargs = {
        "url": f"https://www.douyin.com/video/{vid}",
        "cookie": get_comet_cookie_string(),
        "headers": {}
    }
    handler = DouyinHandler(kwargs)
    try:
        data = await handler.fetch_one_video(vid)
        if data and data.video and data.video.play_addr and data.video.play_addr.url_list:
            print("Success:", data.video.play_addr.url_list[0])
        else:
            print("Got data but no URL:", data)
    except Exception as e:
        print("Error:", e)

if __name__ == "__main__":
    asyncio.run(main())
