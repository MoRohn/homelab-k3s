#!/usr/bin/env python3
"""Generate the Homelab Overview dashboard (Grafana home page).

    python3 monitoring/dashboards/src/homelab_overview.py

Writes monitoring/dashboards/homelab/homelab-overview.json. Edit this file, not the JSON.
"""
import json
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "homelab" / "homelab-overview.json"
DS = {"type": "prometheus", "uid": "${datasource}"}
GIB = 1024 ** 3

# Palette: calm by default, colour only where it carries meaning
GREEN, YELLOW, ORANGE, RED, BLUE, PURPLE, GREY = (
    "#73BF69", "#FADE2A", "#FF9830", "#F2495C", "#5794F2", "#B877D9", "#8e8e8e")

_next_id = 0


def _id():
    global _next_id
    _next_id += 1
    return _next_id


def target(expr, legend="", ref="A", instant=False):
    return {"datasource": DS, "expr": expr, "legendFormat": legend, "refId": ref,
            "range": not instant, "instant": instant}


def thresholds(*steps):
    """steps: (value or None, colour) pairs, first value None (base)."""
    return {"mode": "absolute", "steps": [{"color": c, "value": v} for v, c in steps]}


def stat(title, expr, x, y, w=3, h=4, unit="none", steps=((None, GREEN),), decimals=None,
         mappings=None, no_value="—", description="", spark=True, color_mode="value", links=None):
    p = {
        "id": _id(), "type": "stat", "title": title, "description": description,
        "datasource": DS, "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [target(expr)],
        "fieldConfig": {"defaults": {
            "unit": unit, "noValue": no_value, "thresholds": thresholds(*steps),
            "color": {"mode": "thresholds"}, "mappings": mappings or []},
            "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "colorMode": color_mode, "graphMode": "area" if spark else "none",
                    "justifyMode": "center", "textMode": "value", "wideLayout": True,
                    "showPercentChange": False, "orientation": "auto"},
    }
    if decimals is not None:
        p["fieldConfig"]["defaults"]["decimals"] = decimals
    if links:
        p["links"] = links
    return p


def timeseries(title, targets, x, y, w=12, h=8, unit="none", stack=False, fill=18,
               steps=None, threshold_style="off", min_=None, max_=None, description="",
               overrides=None, legend_calcs=("mean", "max", "lastNotNull")):
    defaults = {
        "unit": unit, "color": {"mode": "palette-classic"},
        "custom": {"drawStyle": "line", "lineInterpolation": "smooth", "lineWidth": 2,
                   "fillOpacity": fill, "gradientMode": "opacity", "showPoints": "never",
                   "spanNulls": True, "axisBorderShow": False, "axisSoftMin": 0,
                   "stacking": {"mode": "normal" if stack else "none", "group": "A"},
                   "thresholdsStyle": {"mode": threshold_style}},
    }
    if steps:
        defaults["thresholds"] = thresholds(*steps)
    if min_ is not None:
        defaults["min"] = min_
    if max_ is not None:
        defaults["max"] = max_
    return {
        "id": _id(), "type": "timeseries", "title": title, "description": description,
        "datasource": DS, "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": targets,
        "fieldConfig": {"defaults": defaults, "overrides": overrides or []},
        "options": {"legend": {"displayMode": "table", "placement": "bottom", "showLegend": True,
                               "calcs": list(legend_calcs), "sortBy": "Last *", "sortDesc": True},
                    "tooltip": {"mode": "multi", "sort": "desc"}},
    }


def bargauge(title, expr, legend, x, y, w=8, h=8, unit="none", steps=((None, BLUE),),
             max_=None, description="", instant=True, name_top=False):
    defaults = {"unit": unit, "thresholds": thresholds(*steps), "displayName": "${__series.name}",
                "color": {"mode": "continuous-BlPu"}, "min": 0}
    if max_ is not None:
        defaults["max"] = max_
    return {
        "id": _id(), "type": "bargauge", "title": title, "description": description,
        "datasource": DS, "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [target(expr, legend, instant=instant)],
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {"displayMode": "gradient", "orientation": "horizontal",
                    "valueMode": "color", "namePlacement": "top" if name_top else "left",
                    "showUnfilled": True,
                    "sizing": "manual", "minVizHeight": 16, "maxVizHeight": 20,
                    "text": {"titleSize": 12, "valueSize": 13},
                    "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}},
    }


def row(title, y, collapsed=False, panels=None):
    return {"id": _id(), "type": "row", "title": title, "collapsed": collapsed,
            "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}, "panels": panels or []}


def text(content, x, y, w, h):
    return {"id": _id(), "type": "text", "title": "", "transparent": True,
            "gridPos": {"x": x, "y": y, "w": w, "h": h},
            "options": {"mode": "markdown", "content": content,
                        "code": {"language": "plaintext", "showLineNumbers": False}}}


UP_DOWN = [{"type": "value", "options": {
    "0": {"text": "DOWN", "color": RED, "index": 1},
    "1": {"text": "UP", "color": GREEN, "index": 0}}}]
AVAILABLE = [{"type": "value", "options": {
    "0": {"text": "Unavailable", "color": RED, "index": 1},
    "1": {"text": "Available", "color": GREEN, "index": 0}}}]
HEALTHY = [{"type": "value", "options": {
    "0": {"text": "Unhealthy", "color": RED, "index": 1},
    "1": {"text": "Healthy", "color": GREEN, "index": 0}}}]

NODE = 'job="node-exporter"'
NET_DEV = 'device!~"lo|veth.*|cni.*|flannel.*|docker.*|br-.*|tailscale.*"'

panels = []
y = 0

# ---- Header ---------------------------------------------------------------------------
panels.append(text(
    "### 🏔️ The Last Node Standing\n"
    "**tiny-dgx** · single-node k3s · Longhorn → MinIO backups · "
    "memory is shared by CPU **and** GPU, so every gigabyte counts.",
    0, y, 24, 2))
y += 2

# ---- At a glance ----------------------------------------------------------------------
panels.append(row("At a glance", y)); y += 1
panels += [
    stat("Node", f'max_over_time(up{{{NODE}}}[2m])', 0, y, mappings=UP_DOWN, spark=False, color_mode="background",
         steps=((None, RED), (1, GREEN)), description="node-exporter reachable in the last 2 minutes"),
    stat("CPU busy",
         f'100 * (1 - avg(rate(node_cpu_seconds_total{{{NODE},mode="idle"}}[$__rate_interval])))',
         3, y, unit="percent", decimals=1, steps=((None, GREEN), (70, YELLOW), (90, RED))),
    stat("Memory available", f'node_memory_MemAvailable_bytes{{{NODE}}}', 6, y, unit="bytes",
         decimals=1, steps=((None, RED), (4 * GIB, ORANGE), (8 * GIB, GREEN)),
         description="Unified memory (CPU + GPU) still available to new work"),
    stat("GPU admissible", 'max(gpusched_capacity_admissible_mib) * 1024^2 '
         'or homelab:gpu_reserve_headroom_bytes', 9, y, unit="bytes",
         decimals=1, steps=((None, RED), (1, ORANGE), (4 * GIB, GREEN)),
         description="Memory bnn gpusched can grant to background GPU jobs right now "
                     "(falls back to MemAvailable minus the 8 GiB reserve)."),
    stat("Root disk free",
         f'100 * node_filesystem_avail_bytes{{{NODE},mountpoint="/",fstype!="rootfs"}} '
         f'/ node_filesystem_size_bytes{{{NODE},mountpoint="/",fstype!="rootfs"}}',
         12, y, unit="percent", decimals=1, steps=((None, RED), (10, ORANGE), (20, GREEN))),
    stat("Pods running", 'sum(kube_pod_status_phase{phase="Running"})', 15, y,
         steps=((None, BLUE),)),
    stat("Pods not healthy",
         'sum(kube_pod_status_phase{phase=~"Pending|Failed|Unknown"}) or vector(0)', 18, y,
         steps=((None, GREEN), (1, RED)), color_mode="background", spark=False),
    stat("Alerts firing",
         'count(ALERTS{alertstate="firing",alertname!~"Watchdog|InfoInhibitor",severity!="info"}) '
         'or vector(0)',
         21, y, steps=((None, GREEN), (1, RED)), color_mode="background", spark=False,
         description="Warning and critical alerts (info-level alerts are listed below)"),
]
y += 4

# ---- Unified memory -------------------------------------------------------------------
panels.append(row("Unified memory (CPU + GPU)", y)); y += 1
panels.append(timeseries(
    "Memory available vs GPU reserve",
    [target(f'node_memory_MemAvailable_bytes{{{NODE}}}', "Available"),
     target(f'node_memory_SwapTotal_bytes{{{NODE}}} - node_memory_SwapFree_bytes{{{NODE}}}',
            "Swap used", "B")],
    0, y, w=16, h=9, unit="bytes",
    steps=((None, RED), (4 * GIB, ORANGE), (8 * GIB, "transparent")),
    threshold_style="line+area",
    description="Red band: below 4 GiB (OOM risk). Orange band: below the 8 GiB gpusched reserve.",
    overrides=[{"matcher": {"id": "byName", "options": "Available"},
                "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": BLUE}}]},
               {"matcher": {"id": "byName", "options": "Swap used"},
                "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": PURPLE}},
                               {"id": "custom.fillOpacity", "value": 0}]}]))
panels.append(bargauge(
    "Memory by namespace",
    'sort_desc(sum by (namespace) (container_memory_working_set_bytes{container!="",pod!=""}))',
    "{{namespace}}", 16, y, w=8, h=9, unit="bytes",
    description="Working set of Kubernetes pods only; Docker containers and host processes "
                "(model servers) are not included."))
y += 9

# ---- GPU scheduler (bnn gpusched) -----------------------------------------------------
MODE = [{"type": "value", "options": {
    "0": {"text": "Observe", "color": BLUE, "index": 1},
    "1": {"text": "Enforce", "color": PURPLE, "index": 0}}}]
YES_NO = [{"type": "value", "options": {
    "0": {"text": "Holding", "color": ORANGE, "index": 1},
    "1": {"text": "Admitting", "color": GREEN, "index": 0}}}]
panels.append(row("GPU scheduler (bnn gpusched)", y)); y += 1
panels += [
    stat("Scheduler", 'max(up{job="gpusched"}) * max(gpusched_up)', 0, y, w=4, mappings=UP_DOWN,
         spark=False, color_mode="background", steps=((None, RED), (1, GREEN))),
    stat("Mode", 'max(gpusched_enforce)', 4, y, w=4, mappings=MODE, spark=False,
         color_mode="background", steps=((None, BLUE),)),
    stat("Admissions", 'max(gpusched_admitting)', 8, y, w=4, mappings=YES_NO, spark=False,
         color_mode="background", steps=((None, ORANGE), (1, GREEN))),
    stat("GPU utilisation", 'max(gpusched_gpu_util_percent)', 12, y, w=4, unit="percent",
         steps=((None, GREEN), (70, YELLOW), (90, RED))),
    stat("Queue depth", 'max(gpusched_queue_depth)', 16, y, w=4,
         steps=((None, GREEN), (1, YELLOW), (5, ORANGE))),
    stat("P(demand next hour)", 'max(gpusched_forecast_p_arrival_next_hour)', 20, y, w=4,
         unit="percentunit", decimals=0, steps=((None, BLUE),),
         description="Forecast probability that production GPU work arrives in the next hour"),
]
y += 4
panels.append(timeseries(
    "Unified memory accounting",
    [target('max(gpusched_capacity_residents_mib) * 1024^2', "Resident models"),
     target('max(gpusched_capacity_leases_mib) * 1024^2', "Leased to jobs", "B"),
     target('max(gpusched_capacity_unmanaged_mib) * 1024^2', "Unmanaged GPU", "C"),
     target('max(gpusched_capacity_admissible_mib) * 1024^2', "Admissible", "D")],
    0, y, w=16, h=8, unit="bytes", stack=True, fill=40,
    description="How gpusched accounts for unified memory: resident model servers, active "
                "leases, GPU memory it doesn't manage, and what it can still admit.",
    overrides=[{"matcher": {"id": "byName", "options": n},
                "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": c}}]}
               for n, c in (("Resident models", PURPLE), ("Leased to jobs", BLUE),
                            ("Unmanaged GPU", RED), ("Admissible", GREEN))]))
panels.append(bargauge(
    "Resident models (measured)",
    'sort_desc(max by (resident) (gpusched_resident_measured_mib) * 1024^2)',
    "{{resident}}", 16, y, w=8, h=8, unit="bytes",
    description="Memory each resident model server actually uses"))
y += 8

# ---- Compute --------------------------------------------------------------------------
panels.append(row("Compute", y)); y += 1
panels.append(timeseries(
    "CPU by mode",
    [target(f'sum by (mode) (rate(node_cpu_seconds_total{{{NODE},mode!="idle"}}[$__rate_interval]))'
            f' / scalar(count(node_cpu_seconds_total{{{NODE},mode="idle"}}))', "{{mode}}")],
    0, y, w=8, h=8, unit="percentunit", stack=True, fill=35, max_=1))
panels.append(bargauge(
    "Top pods by CPU",
    'topk(10, sum by (namespace, pod) (rate(container_cpu_usage_seconds_total{container!="",pod!=""}[5m])))',
    "{{namespace}}/{{pod}}", 8, y, w=8, h=8, unit="cores"))
panels.append(bargauge(
    "Top pods by memory",
    'topk(10, sum by (namespace, pod) (container_memory_working_set_bytes{container!="",pod!=""}))',
    "{{namespace}}/{{pod}}", 16, y, w=8, h=8, unit="bytes"))
y += 8

# ---- Network & disk -------------------------------------------------------------------
panels.append(row("Network and disk", y)); y += 1
panels.append(timeseries(
    "Network throughput",
    [target(f'sum by (device) (rate(node_network_receive_bytes_total{{{NODE},{NET_DEV}}}[$__rate_interval]))',
            "{{device}} in"),
     target(f'-sum by (device) (rate(node_network_transmit_bytes_total{{{NODE},{NET_DEV}}}[$__rate_interval]))',
            "{{device}} out", "B")],
    0, y, w=12, h=8, unit="Bps", description="Inbound above the axis, outbound below."))
panels.append(timeseries(
    "Disk I/O",
    [target(f'sum by (device) (rate(node_disk_read_bytes_total{{{NODE},device=~"nvme.*"}}[$__rate_interval]))',
            "{{device}} read"),
     target(f'-sum by (device) (rate(node_disk_written_bytes_total{{{NODE},device=~"nvme.*"}}[$__rate_interval]))',
            "{{device}} write", "B")],
    12, y, w=12, h=8, unit="Bps", description="Reads above the axis, writes below."))
y += 8

# ---- Storage & backups ----------------------------------------------------------------
panels.append(row("Storage and backups", y)); y += 1
panels += [
    stat("Backup target", '1 - max(longhorn_backup_target_status{condition="unavailable"})',
         0, y, w=4, mappings=AVAILABLE, spark=False, color_mode="background",
         steps=((None, RED), (1, GREEN)), description="Longhorn → s3://longhorn-backups"),
    stat("Last backup", 'time() - max(longhorn_volume_last_backup_at > 0)', 4, y, w=4,
         unit="s", decimals=0, no_value="No backups yet", spark=False,
         steps=((None, GREEN), (86400, YELLOW), (2 * 86400, RED))),
    stat("Longhorn volumes", 'count(count by (volume) (longhorn_volume_robustness)) or vector(0)', 8, y, w=4,
         steps=((None, BLUE),), spark=False),
    stat("Volumes degraded / faulted", 'sum(longhorn_volume_robustness{state=~"degraded|faulted"}) or vector(0)',
         12, y, w=4, steps=((None, GREEN), (1, RED)), color_mode="background", spark=False),
    stat("MinIO", 'max(minio_cluster_health_status)', 16, y, w=4, mappings=HEALTHY,
         spark=False, color_mode="background", steps=((None, RED), (1, GREEN)),
         description="bnn-minio (Docker, outside the cluster)"),
    stat("Off-site copy age", 'time() - max(homelab_offsite_last_success_timestamp_seconds > 0)',
         20, y, w=4, unit="s", decimals=0, no_value="Not set up", spark=False,
         steps=((None, GREEN), (86400 * 1.5, YELLOW), (86400 * 2, RED)),
         description="Time since the last successful sync of longhorn-backups to the "
                     "off-site machine (backup/offsite-sync.sh)"),
]
y += 4
panels.append(bargauge(
    "Longhorn volume usage",
    'longhorn_volume_actual_size_bytes / longhorn_volume_capacity_bytes',
    "{{pvc_namespace}} / {{pvc}}", 0, y, w=8, h=8, unit="percentunit", max_=1,
    steps=((None, GREEN), (0.75, YELLOW), (0.9, RED)),
    description="Actual data written as a share of each volume's size"))
panels.append(timeseries(
    "Longhorn throughput",
    [target('sum by (pvc_namespace, pvc) (longhorn_volume_read_throughput)', "{{pvc_namespace}}/{{pvc}} read"),
     target('-sum by (pvc_namespace, pvc) (longhorn_volume_write_throughput)', "{{pvc_namespace}}/{{pvc}} write", "B")],
    8, y, w=8, h=8, unit="Bps", description="Reads above the axis, writes below."))
panels.append(timeseries(
    "MinIO S3 traffic",
    [target('sum(rate(minio_s3_traffic_received_bytes[$__rate_interval]))', "received"),
     target('-sum(rate(minio_s3_traffic_sent_bytes[$__rate_interval]))', "sent", "B")],
    16, y, w=8, h=8, unit="Bps", description="Backups written above the axis, restores below."))
y += 8

# ---- Alerts & targets -----------------------------------------------------------------
panels.append(row("Alerts and scrape health", y)); y += 1
SEVERITY = [{"type": "value", "options": {
    "critical": {"text": "critical", "color": RED, "index": 0},
    "warning": {"text": "warning", "color": ORANGE, "index": 1},
    "info": {"text": "info", "color": BLUE, "index": 2}}}]
panels.append({
    "id": _id(), "type": "table", "title": "Active alerts", "datasource": DS,
    "description": "Firing and pending Prometheus alerts (Watchdog excluded). Empty means all clear.",
    "gridPos": {"x": 0, "y": y, "w": 12, "h": 8},
    "targets": [dict(target('ALERTS{alertname!~"Watchdog|InfoInhibitor"}', "", instant=True),
                     format="table")],
    "transformations": [{"id": "organize", "options": {
        "excludeByName": {"Time": True, "Value": True, "__name__": True, "container": True,
                          "endpoint": True, "instance": True, "job": True, "pod": True,
                          "service": True, "prometheus": True},
        "indexByName": {"severity": 0, "alertname": 1, "alertstate": 2, "namespace": 3},
        "renameByName": {"severity": "Severity", "alertname": "Alert", "alertstate": "State",
                         "namespace": "Namespace"}}}],
    "fieldConfig": {"defaults": {"noValue": "All clear", "custom": {"align": "left",
                                                                     "cellOptions": {"type": "auto"}}},
                    "overrides": [{"matcher": {"id": "byName", "options": "Severity"},
                                   "properties": [{"id": "mappings", "value": SEVERITY},
                                                  {"id": "custom.cellOptions",
                                                   "value": {"type": "color-text"}},
                                                  {"id": "custom.width", "value": 100}]}]},
    "options": {"showHeader": True, "cellHeight": "sm"},
})
panels.append({
    "id": _id(), "type": "table", "title": "Scrape targets", "datasource": DS,
    "gridPos": {"x": 12, "y": y, "w": 12, "h": 8},
    "targets": [dict(target('min by (job) (up)', "", instant=True), format="table")],
    "transformations": [{"id": "organize", "options": {
        "excludeByName": {"Time": True},
        "renameByName": {"job": "Job", "Value": "Status"}}}],
    "fieldConfig": {"defaults": {"custom": {"align": "left", "cellOptions": {"type": "auto"}}},
                    "overrides": [{"matcher": {"id": "byName", "options": "Status"},
                                   "properties": [
                                       {"id": "mappings", "value": UP_DOWN},
                                       {"id": "custom.cellOptions",
                                        "value": {"type": "color-background", "mode": "basic"}},
                                       {"id": "custom.width", "value": 110}]}]},
    "options": {"showHeader": True, "cellHeight": "sm",
                "sortBy": [{"displayName": "Status", "desc": False}]},
})
y += 8

dashboard = {
    "uid": "homelab-overview",
    "title": "Homelab Overview",
    "description": "Home dashboard for tiny-dgx: node, unified memory, Kubernetes, Longhorn and MinIO.",
    "tags": ["homelab"],
    "timezone": "browser",
    "editable": True,
    "graphTooltip": 1,
    "refresh": "30s",
    "time": {"from": "now-6h", "to": "now"},
    "timepicker": {"refresh_intervals": ["30s", "1m", "5m", "15m"]},
    "schemaVersion": 41,
    "version": 1,
    "fiscalYearStartMonth": 0,
    "liveNow": False,
    "links": [{"title": "Dashboards", "type": "dashboards", "tags": ["homelab"], "asDropdown": True,
               "includeVars": False, "keepTime": True, "icon": "external link", "targetBlank": False}],
    "templating": {"list": [{
        "name": "datasource", "label": "Data source", "type": "datasource", "query": "prometheus",
        "current": {"text": "Prometheus", "value": "prometheus"}, "hide": 2, "refresh": 1,
        "regex": "", "options": []}]},
    "annotations": {"list": [{"builtIn": 1, "datasource": {"type": "grafana", "uid": "-- Grafana --"},
                              "enable": True, "hide": True, "iconColor": "rgba(0, 211, 255, 1)",
                              "name": "Annotations & Alerts", "type": "dashboard"}]},
    "panels": panels,
}

OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(json.dumps(dashboard, indent=2) + "\n")
print(f"wrote {OUT.relative_to(Path.cwd()) if OUT.is_relative_to(Path.cwd()) else OUT} ({len(panels)} panels)")
