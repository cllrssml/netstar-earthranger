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

## EarthRanger data model

- **Provider key**: `netstar_fleet_api`
- **Subject subtype**: `vehicle`
- **Observation additional fields**: `speed_kmh`, `ignition`, `netstar_id`

## Gundi

This integration is being developed with the goal of eventual adoption into the [Gundi](https://github.com/PADAS) open conservation data platform. Contributions and feedback from the conservation tech community are welcome.

## License

MIT
