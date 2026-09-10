import functions_framework
import requests
import uuid
import json
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

# Which SOAP feed to poll. GetVehicleLocationsWithAlarms returns
# GpsLocationWithAlarm, which extends GpsLocationClass -- every field the old
# GetVehicleLocations feed had, plus VehicleName (the vehicle's name in
# Profleet, e.g. "ABC123 - Reserve"), TrackerID, Status and the alarm flags.
# VehicleName is what lets a newly-seen vehicle name its own ER subject.
# To roll back, set these three constants to the GetVehicleLocations trio.
SOAP_OP = "GetVehicleLocationsWithAlarms"
SOAP_ELEMENT = "GpsLocationWithAlarm"
HTTP_TIMEOUT = 30

# ER rejects a subject_subtype it does not know, and "vehicle" is a subject
# TYPE, not a subtype -- that is why the old auto-create never produced a
# subject. Valid subtypes in use on this site: truck_3, car,
# maintenance_dump_truck.
DEFAULT_SUBTYPE = os.environ.get("DEFAULT_SUBTYPE", "truck_3")

# Optional per-vehicle overrides, applied at creation time only (renaming an
# existing subject is a PATCH, which this function deliberately does not do).
#
# Set VEHICLE_OVERRIDES to a JSON object keyed by tracker IMEI, e.g.
#   {"860000000000000": {"name": "ABC123 - Truck", "subtype": "truck_3"}}
#
# "name" is only needed where the tracking provider's own VehicleName is wrong.
# Netstar owns the registration field and does not let the customer edit it, so
# a vehicle can sit under a previous owner's plate. Drop the override once the
# provider corrects it and the feed will name the vehicle itself.
# Kept out of this file on purpose: it is fleet data, not configuration.
try:
    VEHICLE_OVERRIDES = json.loads(os.environ.get("VEHICLE_OVERRIDES", "{}"))
except ValueError as e:
    print(f"⚠️ VEHICLE_OVERRIDES is not valid JSON, ignoring it: {e}")
    VEHICLE_OVERRIDES = {}

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

def _json_body(r):
    """ER wraps payloads in {"data": ...} on some endpoints and not others."""
    try:
        body = r.json()
    except ValueError:
        return {}
    return body.get('data', body) if isinstance(body, dict) else body

def _results(r):
    body = _json_body(r)
    if isinstance(body, list):
        return body
    return body.get('results', []) if isinstance(body, dict) else []

def get_er_cache():
    url = f"{ER_SITE}/api/v1.0/sources/"
    params = {'provider': NETSTAR_PROVIDER_KEY, 'page_size': 1000}
    try:
        r = session.get(url, params=params, timeout=HTTP_TIMEOUT)
        if r.status_code == 200:
            return {s['manufacturer_id']: s['id'] for s in _results(r)}
        print(f"   ⚠️ Cache lookup HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"   ⚠️ Cache Error: {e}")
    return {}

def find_source(imei):
    """Look a source up by IMEI. Used both as the duplicate-POST recovery and
    as a guard against a cache that came back empty."""
    try:
        r = session.get(f"{ER_SITE}/api/v1.0/sources/",
                        params={'provider': NETSTAR_PROVIDER_KEY,
                                'manufacturer_id': imei},
                        timeout=HTTP_TIMEOUT)
        if r.status_code == 200:
            res = _results(r)
            if res:
                return res[0]['id']
    except Exception as e:
        print(f"   ⚠️ Source lookup failed for {imei}: {e}")
    return None

def ensure_vehicle(imei, name, cache):
    """Return the ER source id for this tracker, creating source + subject +
    link the first time we ever see it. Returns None if it could not be
    resolved, in which case this cycle's observation is skipped."""
    if imei in cache:
        return cache[imei]

    # Never create before checking. get_er_cache() returning empty (a timeout,
    # a bad page) used to send every vehicle down the create path at once.
    source_id = find_source(imei)
    if source_id:
        cache[imei] = source_id
        return source_id

    print(f"   🛠️ Registering new vehicle: {name or imei}")
    try:
        r = session.post(f"{ER_SITE}/api/v1.0/sources/", json={
            "manufacturer_id": imei,
            "source_type": "tracking-device",
            "model_name": "Netstar Unit",
            "provider": NETSTAR_PROVIDER_KEY,
            "additional": {},
        }, timeout=HTTP_TIMEOUT)
    except Exception as e:
        print(f"   ❌ Source create failed for {imei}: {e}")
        return None

    if r.status_code in (200, 201):
        source_id = _json_body(r).get('id')
    else:
        # Almost always a duplicate that the cache missed. Re-read rather than
        # string-matching the error text, which has bitten this before.
        print(f"   ⚠️ Source create HTTP {r.status_code}: {r.text[:200]}")
        source_id = find_source(imei)

    if not source_id:
        return None

    # Cache the source immediately. If the subject work below fails we still
    # want this cycle's observations to land, and we do not want to re-run the
    # create path every minute forever.
    cache[imei] = source_id

    subject_id = ensure_subject(imei, name)
    if subject_id:
        link_source(source_id, subject_id, name or imei)
    return source_id

def ensure_subject(imei, name):
    override = VEHICLE_OVERRIDES.get(imei, {})
    # subject_type is readOnly in ER -- it is derived from subject_subtype.
    payload = {
        "name": override.get("name") or name or f"Vehicle {imei[-4:]}",
        "subject_subtype": override.get("subtype", DEFAULT_SUBTYPE),
        "is_active": True,
    }
    print(f"   👤 Creating subject {payload['name']} ({payload['subject_subtype']})")
    try:
        r = session.post(f"{ER_SITE}/api/v1.0/subjects/", json=payload,
                         timeout=HTTP_TIMEOUT)
    except Exception as e:
        print(f"   ❌ Subject create failed for {imei}: {e}")
        return None
    if r.status_code in (200, 201):
        return _json_body(r).get('id')
    print(f"   ❌ Subject create HTTP {r.status_code}: {r.text[:200]}")
    return None

def link_source(source_id, subject_id, label):
    try:
        r = session.post(f"{ER_SITE}/api/v1.0/subject/{subject_id}/sources/", json={
            "source": source_id,
            "assigned_range": {"lower": "2000-01-01T00:00:00Z",
                               "upper": "2099-01-01T00:00:00Z"},
            "additional": {},
        }, timeout=HTTP_TIMEOUT)
    except Exception as e:
        print(f"   ❌ Link failed for {label}: {e}")
        return
    if r.status_code in (200, 201):
        print(f"   ✅ Registered {label} (subject {subject_id})")
    else:
        print(f"   ❌ Link HTTP {r.status_code}: {r.text[:200]}")

def run_sync_logic():
    known_sources = get_er_cache()
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
    <soap:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
      <soap:Body>
        <{SOAP_OP} xmlns="PinpointComms.WebServices">
          <Request>
            <Credentials>
                <UserName>{NETSTAR_USER}</UserName>
                <Password>{NETSTAR_PASS}</Password>
            </Credentials>
            <RequestId>{str(uuid.uuid4())}</RequestId>
            <Compress>false</Compress>
            <Reset>false</Reset>
          </Request>
        </{SOAP_OP}>
      </soap:Body>
    </soap:Envelope>"""

    ns_headers = {'Content-Type': 'text/xml; charset=utf-8',
                  'SOAPAction': f'"PinpointComms.WebServices/{SOAP_OP}"'}
    r = requests.post(NETSTAR_URL, data=xml, headers=ns_headers,
                      timeout=HTTP_TIMEOUT)
    if r.status_code != 200:
        print(f"   ❌ Netstar Error: {r.status_code}")
        return

    clean_xml = re.sub(' xmlns="[^"]+"', '', r.text, count=1)
    locations = ET.fromstring(clean_xml).findall(f".//{SOAP_ELEMENT}")

    if not locations:
        print("   💤 Inbox Empty.")
        return

    print(f"   📥 Processing {len(locations)} updates...")
    count = 0
    for loc in locations:
        imei = _tag(loc, 'VehicleCode')
        if not imei:
            continue
        # Profleet's vehicle name already follows the "<PLATE> - <desc>"
        # convention the ER subjects use, so a new vehicle names itself.
        name = _tag(loc, 'VehicleName')
        sid = ensure_vehicle(imei, name, known_sources)
        if not sid:
            continue

        # LocTime is UTC -- verified 2026-09-10 against a snapshot whose
        # capture time was known in both zones. The "Z" is correct.
        obs = {
            "source": sid,
            "location": {
                "latitude": float(_tag(loc, 'Latitude')),
                "longitude": float(_tag(loc, 'Longitude'))
            },
            "recorded_at": _tag(loc, 'LocTime') + "Z",
            "additional": {
                "speed_kmh": float(_tag(loc, 'Speed') or 0),
                "ignition": (_tag(loc, 'Ignition') or '').lower() == 'true',
                "netstar_id": _tag(loc, 'VehicleId')
            }
        }
        for extra in ('Status', 'EmergencyDistressAlarm', 'RolloverAlert'):
            val = _tag(loc, extra)
            if val is not None:
                obs['additional'][extra.lower()] = val

        try:
            p = session.post(f"{ER_SITE}/api/v1.0/observations/", json=obs,
                             timeout=HTTP_TIMEOUT)
        except Exception as e:
            print(f"   ⚠️ Observation POST failed for {name or imei}: {e}")
            continue
        if p.status_code in (200, 201):
            count += 1
        else:
            print(f"   ⚠️ Observation HTTP {p.status_code} for {name or imei}: {p.text[:200]}")
    print(f"   ✅ Processed {count} updates.")

def _tag(el, name):
    node = el.find(name)
    return node.text if node is not None else None
