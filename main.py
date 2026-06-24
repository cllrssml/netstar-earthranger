import functions_framework
import requests
import uuid
import re
import os
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

# --- CONFIGURATION (From Environment Variables) ---
ER_SITE = os.environ.get("ER_SITE", "https://your-site.pamdas.org")
NETSTAR_URL = "https://profleet.netstar.co.za/avmportal/VehicleActivity.asmx"
NETSTAR_PROVIDER_KEY = 'netstar_fleet_api'

ER_TOKEN = os.environ.get("ER_TOKEN")
NETSTAR_USER = os.environ.get("NETSTAR_USER")
NETSTAR_PASS = os.environ.get("NETSTAR_PASS")

session = requests.Session()
session.headers.update({
    'Authorization': f'Bearer {ER_TOKEN}',
    'Content-Type': 'application/json'
})

@functions_framework.http
def netstar_sync_handler(request):
    """Entry point for the Cloud Run Function."""
    print(f"🚜 STARTING SYNC ({datetime.now().strftime('%H:%M:%S')})")
    
    try:
        run_sync_logic()
        return "OK", 200
    except Exception as e:
        print(f"❌ Critical Error: {e}")
        return f"Error: {e}", 500

def get_er_cache():
    url = f"{ER_SITE}/api/v1.0/sources/"
    params = {'provider': NETSTAR_PROVIDER_KEY, 'page_size': 1000}
    try:
        r = session.get(url, params=params)
        if r.status_code == 200:
            results = r.json().get('data', {}).get('results', []) or r.json().get('results', [])
            return {s['manufacturer_id']: s['id'] for s in results}
    except Exception as e:
        print(f"   ⚠️ Cache Error: {e}")
    return {}

def ensure_vehicle(imei, cache):
    if imei in cache: return cache[imei]
    print(f"   🛠️ Registering new: {imei}")
    src = {"manufacturer_id": imei, "type": "tracking-device", "model_name": "Netstar Unit", "provider": NETSTAR_PROVIDER_KEY}
    r = session.post(f"{ER_SITE}/api/v1.0/sources/", json=src)
    source_id = None
    if r.status_code in [200, 201]:
        source_id = r.json().get('data', r.json()).get('id')
    elif "already exists" in r.text:
        l = session.get(f"{ER_SITE}/api/v1.0/sources/", params={'provider': NETSTAR_PROVIDER_KEY, 'manufacturer_id': imei})
        if l.status_code == 200:
            res = l.json().get('data', {}).get('results', []) or l.json().get('results', [])
            if res: source_id = res[0]['id']
    if source_id:
        subj = {"name": f"Vehicle {imei[-4:]}", "subject_subtype": "vehicle", "is_active": True}
        r2 = session.post(f"{ER_SITE}/api/v1.0/subjects/", json=subj)
        if r2.status_code in [200, 201]:
            subj_id = r2.json().get('data', r2.json()).get('id')
            link = {"source": source_id, "subject": subj_id, "assigned_range": {"lower": datetime.now(timezone.utc).isoformat()}}
            session.post(f"{ER_SITE}/api/v1.0/subjectsources/", json=link)
            cache[imei] = source_id
    return source_id

def run_sync_logic():
    known_sources = get_er_cache()
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
    <soap:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
      <soap:Body>
        <GetVehicleLocations xmlns="PinpointComms.WebServices">
          <Request>
            <Credentials>
                <UserName>{NETSTAR_USER}</UserName>
                <Password>{NETSTAR_PASS}</Password>
            </Credentials>
            <RequestId>{str(uuid.uuid4())}</RequestId>
            <Compress>false</Compress>
            <Reset>false</Reset>
          </Request>
        </GetVehicleLocations>
      </soap:Body>
    </soap:Envelope>"""

    ns_headers = {'Content-Type': 'text/xml; charset=utf-8', 'SOAPAction': '"PinpointComms.WebServices/GetVehicleLocations"'}
    r = requests.post(NETSTAR_URL, data=xml, headers=ns_headers)
    if r.status_code != 200:
        print(f"   ❌ Netstar Error: {r.status_code}")
        return

    clean_xml = re.sub(' xmlns="[^"]+"', '', r.text, count=1)
    locations = ET.fromstring(clean_xml).findall(".//GpsLocationClass")
    
    if not locations:
        print("   💤 Inbox Empty.")
        return

    print(f"   📥 Processing {len(locations)} updates...")
    count = 0
    for loc in locations:
        imei = loc.find('VehicleCode').text
        sid = ensure_vehicle(imei, known_sources)
        if not sid: continue
        
        obs = {
            "source": sid,
            "location": {
                "latitude": float(loc.find('Latitude').text),
                "longitude": float(loc.find('Longitude').text)
            },
            "recorded_at": loc.find('LocTime').text + "Z",
            "additional": {
                "speed_kmh": float(loc.find('Speed').text),
                "ignition": loc.find('Ignition').text.lower() == 'true',
                "netstar_id": loc.find('VehicleId').text
            }
        }
        p = session.post(f"{ER_SITE}/api/v1.0/observations/", json=obs)
        if p.status_code in [200, 201]: count += 1
    print(f"   ✅ Processed {count} updates.")