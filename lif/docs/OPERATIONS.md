# Operations runbook

## Access

| What | How |
|---|---|
| Control Center | `https://lif.tiny-dgx.lan` (accept the self-signed cert). Add `192.168.68.72 lif.tiny-dgx.lan ai.tiny-dgx.lan` to `/etc/hosts` on your client. Paste `secrets/lif-admin.key` when prompted (stored in browser localStorage; "Sign out" clears it) |
| Gateway | `https://ai.tiny-dgx.lan/v1` with a gateway key |
| CLI | `local-ai` (`pip install -e .` in `.venv`, or `python -m lif.cli.main`) |

The CLI defaults to `127.0.0.1:18080/18081/18082`. Either forward the ports or point it at the services:

```bash
kubectl -n ai-system port-forward svc/gateway 18080:8080 &
kubectl -n ai-system port-forward svc/controller 18081:8080 &
kubectl -n ai-system port-forward svc/decision-fabric 18082:8080 &
export LIF_ADMIN_KEY=$(cat ~/labzilla/secrets/lif-admin.key) LIF_API_KEY=$(cat ~/labzilla/secrets/lif-operator.key)
```

## Everyday commands

| Task | Command |
|---|---|
| Overall status | `local-ai status` |
| Health check (exit 1 on FAIL) | `local-ai doctor` |
| GPU/BLERBZ state | `local-ai gpu` |
| Models | `local-ai models list` · `local-ai models candidates` |
| Check for better models | `local-ai models refresh [--categories fast coding] [--wait]` (or the UI button) |
| Download / benchmark | `local-ai models download ID` · `local-ai models benchmark ID` |
| Canary / promote | `local-ai models canary ID [--alias local/default --percent 10]` · `local-ai models promote ID [--alias …]` |
| Rollback | `local-ai models rollback local/default [--to-version N]` |
| Pin / block | `local-ai models pin ID` · `local-ai models block ID` (plus `unpin`/`unblock`) |
| Load / unload | `local-ai models load ID [--force]` · `local-ai models unload ID [--force]` (unload refuses if an alias depends on it unless forced) |
| Decision fabric | `local-ai decision status` · `workflows` · `metrics` |
| Batch | `local-ai batch list` · `local-ai batch pause` · `local-ai batch resume` |
| Maintenance | `local-ai maintenance on` (pauses batch, refuses candidate benchmarks) · `off` |
| Audit trail | `local-ai activity --limit 100` |

All mutating actions go through the controller and appear in the activity timeline with the operator's key name.

## Operator controls (Settings page / `POST /v1/settings`)

| Setting | Effect | Default |
|---|---|---|
| `discovery_disabled` | refresh returns "disabled" | false |
| `automatic_discovery` | weekly scheduled refresh | false (`models.automatic_discovery`) |
| `automatic_download` | non-operator downloads allowed | false |
| `automatic_promotion` | non-operator promotions allowed | false |
| `jev_disabled` | Jev kill switch for every service (gateway, batch, controller, decision-fabric); takes effect within ~15 s | false |
| `maintenance` | pauses batch; blocks candidate benchmarks | false |
| `batch_paused` | pauses batch | false |
| `reserve_gpu_mib` | stored only, **not yet enforced** | 0 |

## Alerts: what to do

| Alert | Check | Fix |
|---|---|---|
| LIFUsefulAIUnavailable | `kubectl -n ai-serving get pods`, `local-ai doctor`, `kubectl -n ai-serving logs deploy/tier0` | OOMKilled → check host memory (`free -m`) and the guard events. Model file missing → re-run the seed Job |
| LIFGatewayDown | `kubectl -n ai-system get pods -l app=gateway`, events | rollout restart; check `lif-secrets` exists |
| LIFDefaultDegraded | `local-ai status`: is tier0 healthy? | tier0 restarting? Look at liveness failures under load (CPU saturation) |
| LIFJevCircuitOpen | `local-ai decision status`, `kubectl logs deploy/decision-fabric` | 401/402 → key or credits (rotate or top up; see SECURITY.md, private, local only). Otherwise internet/Jev outage: nothing to do, rules are serving |
| LIFHostHeadroomLow | `gpusched status`, the process list by anonymous memory (TROUBLESHOOTING.md) | The guard already sheds `tier0-small`. If non-LIF processes (desktop/Firefox) are the cause, close them; LIF can't fix that |

## Reading the activity timeline

| Event kinds | Source |
|---|---|
| `discovery_started/finished/failed` | discovery runs |
| `model_registered`, `state_changed` | registry |
| `download_started/complete` | downloads |
| `load_test_passed`, `benchmark_completed`, `comparison_complete` | benchmarks |
| `alias_changed`, `alias_rollback` | aliases |
| `blerbz_state`, `blerbz_takeover`, `blerbz_release` | the BLERBZ protection loop |
| `memory_guard_shed/restore` | the host memory guard |
| `setting_changed` | operator settings |
| `retention_gc` | daily artifact cleanup |
| `controller_started` | controller restarts |
