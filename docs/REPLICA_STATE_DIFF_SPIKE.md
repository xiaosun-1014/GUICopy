# Replica State Diff Spike

## Fixture result

- `live_spike`: `blocked_by_environment` — this workspace has no authorised, active
  hospital session; no patient screenshots or DOM snapshots were captured.
- Test fixture: 30 masked spinner/timestamp samples created no visual state.
- Critical fixture changes: series selection, Metadata panel, and WL/WW confirmation
  all crossed the regional threshold.

## Calibrated MVP profile

```json
{
  "pixel_channel_threshold": 12,
  "regional_changed_ratio": 0.02,
  "regional_mean_abs_diff": 3.5,
  "global_changed_ratio": 0.08,
  "stability_interval_ms": 200,
  "stability_rounds": 2
}
```

Dynamic fixture selectors are `.spinner`, `.clock`, and `[aria-busy="true"]`.
The comparison input is CSS-scale PNG bytes; JPEG is reserved for generated visual
assets. A production live-spike run remains required before hospital deployment.
