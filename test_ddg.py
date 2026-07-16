from duckduckgo_search import DDGS
import json

def test():
    with DDGS() as ddgs:
        res = list(ddgs.text("AI video generation site:douyin.com", max_results=5))
        print("Search 1:", json.dumps(res, indent=2))
        
        res2 = list(ddgs.text("AI video generation douyin", max_results=5))
        print("Search 2:", json.dumps(res2, indent=2))

test()
