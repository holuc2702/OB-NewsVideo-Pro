import asyncio
import websockets
import json

connected = False

async def handler(websocket):
    global connected
    connected = True
    print("Extension connected!")
    
    # Request a video!
    req = {"type": "FETCH_AWEME", "vid": "7387342082260651275"}
    await websocket.send(json.dumps(req))
    print("Sent request for video detail...")
    
    try:
        async for message in websocket:
            data = json.loads(message)
            if data.get('type') == 'AWEME_DETAIL':
                print("Received Detail!")
                detail = data.get('detail')
                if detail:
                    urls = detail.get('video', {}).get('play_addr', {}).get('url_list', [])
                    print("URL LIST:", urls)
                else:
                    print("Detail is null")
                break
    except websockets.exceptions.ConnectionClosed:
        print("Connection closed")

async def main():
    async with websockets.serve(handler, "127.0.0.1", 8765):
        print("Server started on ws://127.0.0.1:8765")
        for _ in range(60):
            if connected:
                await asyncio.sleep(5)
                break
            await asyncio.sleep(1)

if __name__ == "__main__":
    asyncio.run(main())
