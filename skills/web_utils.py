import requests
from bs4 import BeautifulSoup
from ddgs import DDGS

import os

VPN_DOWN_FLAG   = "/run/chip/vpn_down"
VPN_OVERRIDE    = "/run/chip/home_ok"
VPN_REFUSAL     = ("Mullvad is down, so I'm not sending this search out over the "
                   "home IP. Reply 'home ok' to allow home routing for 30 minutes.")


def vpn_blocked():
    """True if outbound web should refuse: VPN is down and no override is live.

    The override is a timestamped file; it expires on its own so a one-off
    'home ok' can't silently become permanent.
    """
    if not os.path.exists(VPN_DOWN_FLAG):
        return False
    try:
        import time
        if os.path.exists(VPN_OVERRIDE):
            if time.time() - os.path.getmtime(VPN_OVERRIDE) < 1800:
                return False
            os.remove(VPN_OVERRIDE)
    except Exception:
        pass
    return True


def quick_search(query, max_results=3):
    """Executes a silent DuckDuckGo search and returns results."""
    if vpn_blocked():
        return VPN_REFUSAL
    try:
        with DDGS() as ddgs:
            return list(ddgs.text(query, max_results=max_results))
    except Exception as e:
        return f"Search Error: {e}"

def scrape_url(url):
    """Scrapes and cleans text from a target URL, stripping out web-junk."""
    if vpn_blocked():
        return VPN_REFUSAL
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        # Strip out the noisy elements
        for element in soup(["script", "style", "nav", "footer", "header"]):
            element.extract()
            
        text = soup.get_text(separator=' ', strip=True)
        # Cap length to avoid blowing up the LLM context window
        return text[:8000] 
    except Exception as e:
        return f"Scrape Error: {e}"

if __name__ == "__main__":
    # A quick self-test if you run this script directly
    print("🟢 Testing DuckDuckGo Search...")
    results = quick_search("Maine Lobster Festival 2026 dates")
    for r in results:
        print(f"- {r.get('title')}: {r.get('href')}")
