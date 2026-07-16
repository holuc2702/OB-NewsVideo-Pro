import asyncio
import websockets
import json
import threading
import time

DOUYIN_WS_CLIENTS = set()
DOUYIN_RESPONSES = {}

async def ws_handler(websocket):
    DOUYIN_WS_CLIENTS.add(websocket)
    print("[Douyin WS] Extension connected!")
    try:
        async for message in websocket:
            try:
                data = json.loads(message)
                if data.get("type") == "AWEME_DETAIL":
                    vid = data.get("vid")
                    DOUYIN_RESPONSES[vid] = data.get("detail")
                    print(f"[Douyin WS] Received detail for {vid}")
            except Exception as e:
                print("[Douyin WS] Message error:", e)
    finally:
        DOUYIN_WS_CLIENTS.remove(websocket)
        print("[Douyin WS] Extension disconnected.")

async def ws_main():
    async with websockets.serve(ws_handler, "127.0.0.1", 8765):
        await asyncio.Future()  # run forever

def start_ws_server():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(ws_main())

def start_ws_thread():
    t = threading.Thread(target=start_ws_server, daemon=True)
    t.start()

def request_douyin_url(vid, timeout=20):
    if not DOUYIN_WS_CLIENTS:
        print("[Douyin WS] No extension connected! Please reload Comet extension.")
        return None
    
    client = next(iter(DOUYIN_WS_CLIENTS))
    req = json.dumps({"type": "FETCH_AWEME", "vid": vid})
    asyncio.run_coroutine_threadsafe(client.send(req), client.loop)
    
    start_time = time.time()
    while time.time() - start_time < timeout:
        if vid in DOUYIN_RESPONSES:
            detail = DOUYIN_RESPONSES.pop(vid)
            if detail and "video" in detail:
                urls = detail["video"].get("play_addr", {}).get("url_list", [])
                if urls:
                    return urls[0]
            return None
        time.sleep(0.5)
    print(f"[Douyin WS] Timeout waiting for detail for {vid}")
    return None
