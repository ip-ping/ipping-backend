"""A finished measurement as plain text, in the spirit of what `ping` or `traceroute` would print.

For a terminal: `curl` against the text endpoint prints this. The interface has its own version for pasting
(`export.ts`); both say the same things in the same order.
"""

from __future__ import annotations

from datetime import UTC, datetime

from .schemas import MeasurementOut, TaskOut

LABELS = {
    "ping": "Ping",
    "tcp": "TCP",
    "http": "HTTP",
    "traceroute": "Traceroute",
    "dns": "DNS",
    "tls": "Certificate",
}


def ms(value: float | None) -> str:
    if value is None:
        return "–"
    return f"{value:.0f}" if value >= 100 else f"{value:.1f}"


def _us(value: int | None) -> float | None:
    return None if value is None else value / 1000


def _line(kind: str, task: TaskOut, name: str) -> list[str]:
    summary = task.summary
    first = task.samples[0] if task.samples else None
    if kind == "http":
        http = first.http if first else None
        if http is not None and http.status_code is not None:
            return [
                f"{name}  {http.status_code}  {ms(_us(http.total_us))} ms  "
                f"(dns {ms(_us(http.dns_us))}, connect {ms(_us(http.connect_us))}, "
                f"tls {ms(_us(http.tls_us))}, first byte {ms(_us(http.ttfb_us))})"
            ]
        why = (
            "timed out"
            if first and first.status == "timeout"
            else (first.error_code if first and first.error_code else task.status)
        )
        return [f"{name}  {why}"]
    if kind == "tls":
        detail = first.tls if first else None
        cert = detail.tls if detail else None
        if detail is not None and cert is not None:
            trust = (
                "trusted" if cert.verified else f"NOT trusted ({cert.verify_error or 'unknown'})"
            )
            expiry = ""
            if cert.not_after is not None:
                days = (cert.not_after - datetime.now(UTC)).total_seconds() / 86400
                when = f"in {days:.0f} days" if days >= 0 else f"{-days:.0f} days ago"
                expiry = f"  expires {cert.not_after.astimezone(UTC):%Y-%m-%d} ({when})"
            return [
                f"{name}  {trust}  {cert.version or '?'}{expiry}  {ms(_us(detail.total_us))} ms"
            ]
        why = (
            "timed out"
            if first and first.status == "timeout"
            else (first.error_code if first and first.error_code else task.status)
        )
        return [f"{name}  {why}"]
    if kind == "traceroute":
        hops = [s.hop for s in task.samples if s.hop is not None]
        if summary and summary.reached:
            head = f"{name}  reached in {hops[-1].ttl if hops else '?'} hops, {ms(summary.median_ms)} ms"
        else:
            head = f"{name}  destination not reached"
        lines = [head]
        for hop in hops:
            who = hop.ip or ("(private network)" if hop.private else "*")
            if hop.sent is not None:
                # A repeated trace: loss and the spread, not every reply.
                got = len(hop.rtts_us)
                loss = f"{(hop.sent - got) * 100 / hop.sent:g}% loss"
                if hop.rtts_us:
                    best, worst = min(hop.rtts_us) / 1000, max(hop.rtts_us) / 1000
                    avg = sum(hop.rtts_us) / got / 1000
                    spread = f"best/avg/worst = {ms(best)}/{ms(avg)}/{ms(worst)} ms"
                else:
                    spread = ""
                lines.append(f"  {hop.ttl:>2}  {who:<40}  {loss:<10}  {spread}".rstrip())
                continue
            times = "  ".join(ms(us / 1000) for us in hop.rtts_us)
            lines.append(f"  {hop.ttl:>2}  {who:<40}  {times}{' ms' if times else ''}")
        return lines
    if kind == "dns":
        dns = first.dns if first else None
        if dns is None:
            return [f"{name}  {'no answer' if first else 'waiting'}"]
        answers = [
            a.data if a.type == dns.record_type else f"{a.type} {a.data}" for a in dns.answers
        ]
        what = ", ".join(answers) or (
            "no such name" if dns.rcode == "NXDOMAIN" else dns.rcode.lower()
        )
        rtt = f"  {ms(first.rtt_ms)} ms" if first and first.rtt_ms is not None else ""
        via = "  (DoH)" if dns.transport == "doh" else ""
        return [f"{name}  {what}{rtt}{via}"]
    if summary and summary.received > 0:
        return [
            f"{name}  {summary.received}/{summary.sent} received  "
            f"min/median/avg/max = {ms(summary.min_ms)}/{ms(summary.median_ms)}/"
            f"{ms(summary.avg_ms)}/{ms(summary.max_ms)} ms  loss {summary.loss_pct:g}%"
        ]
    if summary:
        return [f"{name}  0/{summary.sent} received"]
    return [f"{name}  {task.error_message or task.status}"]


def render_text(m: MeasurementOut) -> str:
    when = m.created_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    port = f":{m.options.port}" if m.options.port else ""
    extra = ""
    if m.kind == "dns":
        extra = f", {m.options.record_type or 'A'} records via {m.options.resolver or 'system'}"
    elif m.kind in ("ping", "tcp"):
        unit = "echo requests" if m.kind == "ping" else "connections"
        extra = f", {m.options.count} {unit} per node"
    lines = [f"{LABELS.get(m.kind, m.kind)} {m.target.host}{port} ({when}{extra})", ""]
    names = [f"{t.node.name}, {t.node.country or '?'}" for t in m.tasks]
    width = max([10, *(len(n) for n in names)])
    for task, name in zip(m.tasks, names, strict=True):
        lines.extend(_line(m.kind, task, name.ljust(width)))
    lines.append("")
    state = "" if m.status != "running" else " (still running: these are the results so far)"
    lines.append(
        f"{m.stats.nodes_responding} of {m.stats.nodes_total} nodes answered{state}. Measured with ipping."
    )
    return "\n".join(lines) + "\n"
