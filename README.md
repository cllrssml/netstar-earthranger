# netstar-earthranger

A lightweight Cloud Run integration that polls the [Netstar](https://www.netstar.co.za/) fleet tracking API and pushes vehicle locations into [EarthRanger](https://www.earthranger.com/) as observations in near real-time.

Built for conservation operations where game vehicles, ranger vehicles, and support vehicles are tracked via Netstar and need to appear on the EarthRanger situational awareness map.

## What it does

1. Calls the Netstar SOAP API to retrieve current vehicle locations
2. Auto-registers new vehicles in EarthRanger as Sources + Subjects (linked)
3. Posts each location as an EarthRanger observation (lat/lon, speed, ignition state)
4. Designed to be triggered on a schedule (e.g. every 2 minutes via Cloud Scheduler)

## Architecture

```
Cloud Scheduler → Cloud Run (HTTP) → Netstar SOAP API
                                   → EarthRanger REST API
```

Deployed as a Google Cloud Run service using the Functions Framework.

## Setup

### 1. Clone and configure

```bash
git clone https://github.com/cllrssml/netstar-earthranger.git
cd netstar-earthranger
cp .env.example .env
# Edit .env with your credentials
```

### 2. Deploy to Cloud Run

```bash
gcloud run deploy netstar-sync \
  --source . \
  --region us-central1 \
  --set-env-vars ER_SITE=https://your-site.pamdas.org \
  --set-env-vars ER_TOKEN=your_token \
  --set-env-vars NETSTAR_USER=your_user \
  --set-env-vars NETSTAR_PASS=your_pass \
  --no-allow-unauthenticated
```

### 3. Schedule it

Create a Cloud Scheduler job to POST to the Cloud Run URL every 2 minutes.

## Environment Variables

| Variable | Description |
|---|---|
| `ER_SITE` | EarthRanger base URL (e.g. `https://your-site.pamdas.org`) |
| `ER_TOKEN` | EarthRanger bearer token |
| `NETSTAR_USER` | Netstar SOAP API username |
| `NETSTAR_PASS` | Netstar SOAP API password |
| `DEFAULT_SUBTYPE` | Optional. EarthRanger `subject_subtype` given to newly-discovered vehicles. Defaults to `truck_3`. Must be a subtype that already exists on your site. |
| `VEHICLE_OVERRIDES` | Optional. JSON object keyed by tracker IMEI, overriding the name and/or subtype used when a vehicle is first created — e.g. `{"860000000000000": {"name": "ABC123 - Truck", "subtype": "car"}}`. Useful when the tracking provider holds a stale registration for a vehicle. Applied at creation time only. |

## EarthRanger data model

- **Provider key**: `netstar_fleet_api`
- **Source**: one per tracker, `source_type` `tracking-device`, `manufacturer_id` = the tracker IMEI
- **Subject subtype**: `DEFAULT_SUBTYPE` (`truck_3` unless overridden). `subject_type` is read-only in EarthRanger and is derived from the subtype, so the subtype must be one your site already defines.
- **Observation additional fields**: `speed_kmh`, `ignition`, `netstar_id`, `status`, `emergencydistressalarm`, `rolloveralert`

### Automatic registration

A vehicle that appears in the Netstar feed for the first time gets its Source,
Subject and subject-source link created automatically. It is named from the
feed's `VehicleName` — the vehicle's name in Profleet — falling back to
`Vehicle <last 4 of IMEI>` if the feed carries no name.

The feed polled is `GetVehicleLocationsWithAlarms`, which returns
`GpsLocationWithAlarm`: a strict superset of the `GpsLocationClass` returned by
`GetVehicleLocations`, adding `VehicleName`, `TrackerID`, `Status` and the alarm
flags.

**A vehicle must belong to a group in Profleet to appear in the feed at all.**
An ungrouped vehicle is not returned, no matter how healthy its tracker, so it
will never reach EarthRanger. Check group membership first if a vehicle is
missing.

## Gundi

This integration is being developed with the goal of eventual adoption into the [Gundi](https://github.com/PADAS) open conservation data platform. Contributions and feedback from the conservation tech community are welcome.

## License

MIT
