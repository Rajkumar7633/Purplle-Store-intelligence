#!/usr/bin/env python3
"""
Live terminal dashboard using Rich.
Polls the API every 5 seconds and renders real-time store metrics.

Usage:
    python dashboard/terminal_dashboard.py --store STORE_BLR_002 --api http://localhost:8000
"""
import argparse
import time
import sys
from datetime import datetime

import requests

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.layout import Layout
    from rich.live import Live
    from rich.text import Text
    from rich import box
    RICH_AVAILABLE = True
except ImportError:
    print("Install rich: pip install rich")
    RICH_AVAILABLE = False

console = Console()


def fetch_metrics(api_url: str, store_id: str) -> dict:
    try:
        r = requests.get(f"{api_url}/stores/{store_id}/metrics", timeout=5)
        return r.json() if r.status_code == 200 else {}
    except Exception:
        return {}


def fetch_funnel(api_url: str, store_id: str) -> dict:
    try:
        r = requests.get(f"{api_url}/stores/{store_id}/funnel", timeout=5)
        return r.json() if r.status_code == 200 else {}
    except Exception:
        return {}


def fetch_anomalies(api_url: str, store_id: str) -> list:
    try:
        r = requests.get(f"{api_url}/stores/{store_id}/anomalies", timeout=5)
        return r.json().get("active_anomalies", []) if r.status_code == 200 else []
    except Exception:
        return []


def build_dashboard(metrics: dict, funnel: dict, anomalies: list, store_id: str) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body"),
        Layout(name="footer", size=6),
    )
    layout["body"].split_row(
        Layout(name="left"),
        Layout(name="right"),
    )

    # ── Header ──────────────────────────────────────────────────────────────
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    layout["header"].update(Panel(
        f"[bold cyan]Store Intelligence Dashboard[/] — {store_id}  [dim]{ts}[/]",
        style="bold",
    ))

    # ── Metrics panel ────────────────────────────────────────────────────────
    metrics_table = Table(box=box.ROUNDED, show_header=False, padding=(0, 1))
    metrics_table.add_column("Metric", style="cyan")
    metrics_table.add_column("Value", style="bold green")

    metrics_table.add_row("Unique Visitors", str(metrics.get("unique_visitors", "—")))
    conv_rate = metrics.get("conversion_rate", 0)
    metrics_table.add_row("Conversion Rate", f"{conv_rate:.1%}")
    metrics_table.add_row("Queue Depth", str(metrics.get("current_queue_depth", "—")))
    abandon = metrics.get("abandonment_rate", 0)
    metrics_table.add_row("Abandonment Rate", f"{abandon:.1%}")
    metrics_table.add_row("Total Entries", str(metrics.get("total_entries", "—")))
    metrics_table.add_row("Total Exits", str(metrics.get("total_exits", "—")))

    layout["left"].update(Panel(metrics_table, title="[bold]Live Metrics[/]", border_style="green"))

    # ── Funnel panel ─────────────────────────────────────────────────────────
    funnel_table = Table(box=box.SIMPLE, padding=(0, 1))
    funnel_table.add_column("Stage", style="cyan")
    funnel_table.add_column("Count", style="bold")
    funnel_table.add_column("Drop-off", style="red")

    for stage in funnel.get("stages", []):
        drop = f"{stage['drop_off_pct']:.1f}%" if stage.get("drop_off_pct", 0) > 0 else "—"
        funnel_table.add_row(stage["stage"], str(stage["count"]), drop)

    layout["right"].update(Panel(funnel_table, title="[bold]Conversion Funnel[/]", border_style="yellow"))

    # ── Anomalies footer ──────────────────────────────────────────────────────
    if anomalies:
        severity_colors = {"INFO": "blue", "WARN": "yellow", "CRITICAL": "red bold"}
        lines = []
        for a in anomalies[:4]:
            color = severity_colors.get(a["severity"], "white")
            lines.append(f"[{color}][{a['severity']}] {a['anomaly_type']}:[/] {a['description'][:80]}")
        content = "\n".join(lines)
    else:
        content = "[green]No active anomalies[/]"

    layout["footer"].update(Panel(content, title="[bold]Active Anomalies[/]", border_style="red"))

    return layout


def run_dashboard(store_id: str, api_url: str, refresh_sec: int = 5) -> None:
    if not RICH_AVAILABLE:
        sys.exit(1)

    console.print(f"[cyan]Connecting to {api_url}...[/]")

    with Live(console=console, refresh_per_second=1, screen=True) as live:
        while True:
            metrics = fetch_metrics(api_url, store_id)
            funnel = fetch_funnel(api_url, store_id)
            anomalies = fetch_anomalies(api_url, store_id)
            layout = build_dashboard(metrics, funnel, anomalies, store_id)
            live.update(layout)
            time.sleep(refresh_sec)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Store Intelligence Terminal Dashboard")
    parser.add_argument("--store", default="STORE_BLR_002")
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--refresh", type=int, default=5)
    args = parser.parse_args()
    run_dashboard(args.store, args.api, args.refresh)
