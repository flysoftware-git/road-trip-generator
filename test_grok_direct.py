#!/usr/bin/env python3
"""
Direct test of Grok API to diagnose 400 errors.
Run from: python test_grok_direct.py
"""
import os
import json
import requests
from dotenv import load_dotenv

# Load .env (try both locations)
load_dotenv()  # Current dir
load_dotenv(r"c:\Dev\Sandbox\.env")  # Sandbox dir

API_KEY = os.environ.get("XAI_API_KEY")
if not API_KEY:
    print("❌ ERROR: XAI_API_KEY not found in environment")
    print(f"   Checked: .env in current dir and c:\\Dev\\Sandbox\\.env")
    print(f"   Available env vars: {[k for k in os.environ.keys() if 'XAI' in k or 'API' in k]}")
    exit(1)

print(f"✓ API Key loaded: {API_KEY[:20]}...")
print(f"✓ API Key length: {len(API_KEY)}")

MODEL = os.environ.get("XAI_MODEL", "grok-2-latest")
print(f"✓ Model: {MODEL}")

# Test basic connectivity and format
endpoint = "https://api.x.ai/v1/chat/completions"
headers = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
}

payload = {
    "model": MODEL,
    "messages": [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Say 'Hello, world!'"},
    ],
    "temperature": 0.7,
}

print("\n" + "="*70)
print("TEST 1: Simple greeting (no JSON requirement)")
print("="*70)
print(f"Endpoint: {endpoint}")
print(f"Headers: {json.dumps(headers, indent=2)}")
print(f"Payload: {json.dumps(payload, indent=2)}")

try:
    resp = requests.post(endpoint, json=payload, headers=headers, timeout=15)
    print(f"\n✓ Response Status: {resp.status_code}")
    print(f"✓ Response Headers: {dict(resp.headers)}")
    print(f"✓ Response Body:\n{resp.text[:500]}")
    
    if resp.status_code == 200:
        data = resp.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        print(f"\n✓ Message Content: {content}")
    else:
        print(f"\n❌ Non-200 status: {resp.status_code}")
        print(f"Response: {resp.text}")
except Exception as e:
    print(f"\n❌ Exception: {e}")
    import traceback
    traceback.print_exc()

print("\n" + "="*70)
print("TEST 2: JSON-formatted search (with system prompt)")
print("="*70)

search_payload = {
    "model": MODEL,
    "messages": [
        {
            "role": "system",
            "content": (
                "You are a web search engine. Perform a web search for the user query and return results "
                "strictly in this JSON format:\n"
                '{"results": [{"title": "...", "url": "...", "snippet": "..."}]}\n'
                "Return only valid JSON. No commentary, no prose, no markdown. JSON only."
            ),
        },
        {"role": "user", "content": "best scenic drives near Moab Utah"},
    ],
    "temperature": 0.7,
}

print(f"Payload: {json.dumps(search_payload, indent=2)}")

try:
    resp = requests.post(endpoint, json=search_payload, headers=headers, timeout=15)
    print(f"\n✓ Response Status: {resp.status_code}")
    print(f"✓ Response Body:\n{resp.text[:800]}")
    
    if resp.status_code == 200:
        data = resp.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        print(f"\n✓ Message Content:\n{content[:400]}")
except Exception as e:
    print(f"\n❌ Exception: {e}")
    import traceback
    traceback.print_exc()

print("\n" + "="*70)
print("Done.")
print("="*70)
