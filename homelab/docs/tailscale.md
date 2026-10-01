# Tailscale: Grafana and Prometheus from anywhere

The Tailscale Kubernetes operator gives each exposed service its own HTTPS address on your
tailnet (`https://grafana.<tailnet>.ts.net`). Nothing is opened to the internet or the LAN,
and only devices signed in to your Tailscale account can connect.

## One-time: admin console (about 5 minutes)

All at <https://login.tailscale.com/admin>:

1. **DNS** page: enable **MagicDNS**, then **HTTPS Certificates**.
2. **Access controls**: add these tag owners to the policy file and save:
   ```json
   "tagOwners": {
     "tag:k8s-operator": [],
     "tag:k8s": ["tag:k8s-operator"]
   }
   ```
   (If a `tagOwners` block already exists, add the two entries to it.)
3. **Settings → OAuth clients → Generate OAuth client**:
   - Scopes, **Write** on: *Devices → Core*, *Keys → Auth Keys*, *Services*
   - Tag: `tag:k8s-operator`
   - Copy the client ID and secret (the secret is shown only once).
4. On tiny-dgx, save them with an editor (keeps the secret out of shell history):
   ```bash
   nano ~/labzilla/secrets/tailscale.env
   ```
   ```
   TS_OAUTH_CLIENT_ID=...
   TS_OAUTH_CLIENT_SECRET=tskey-client-...
   ```

## Then

```bash
~/labzilla/homelab/scripts/install-tailscale.sh
```

The script checks the credentials with Tailscale (and that the scopes are right), stores
them in the cluster, hands the operator and ingresses to Argo CD, waits for everything to
come up, and prints the two URLs. Each step either succeeds with a ✓ or stops with a
specific reason.

Install the Tailscale app on your phone or laptop and sign in to the same account to use
the URLs.

| URL | Login |
|---|---|
| `https://grafana.<tailnet>.ts.net` | Grafana's own (repo-root `secrets/grafana.env`) |
| `https://prometheus.<tailnet>.ts.net` | **none**: see below |

**About the Prometheus URL.** Prometheus has no login, and it runs with
`--web.enable-lifecycle` (the operator's config reloader needs it). So anyone on your tailnet
can not only query it but also `POST /-/quit` to stop it until Kubernetes restarts it. That's
fine on a tailnet with only your own devices. If you ever share the tailnet, delete
`networking/tailscale/ingresses/prometheus.yaml` and use Grafana → **Explore**, which runs the
same queries behind Grafana's login.
