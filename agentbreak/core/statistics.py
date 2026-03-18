"""Statistics tracking and scorecard generation for AgentBreak services."""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ServiceStatistics:
    """Statistics for a single service."""

    total_requests: int = 0
    injected_faults: int = 0
    latency_injections: int = 0
    upstream_successes: int = 0
    upstream_failures: int = 0
    duplicate_requests: int = 0
    suspected_loops: int = 0
    method_counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    seen_fingerprints: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    recent_requests: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=20))
    total_processing_time_ms: float = 0.0
    session_start_time: float = field(default_factory=time.monotonic)
    # Lock for thread-safe updates to shared state
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class StatisticsTracker:
    """Track statistics across all services."""

    def __init__(self) -> None:
        self.services: dict[str, ServiceStatistics] = {}

    def get_service_stats(self, service_name: str) -> ServiceStatistics:
        """Get or create statistics for a service."""
        if service_name not in self.services:
            self.services[service_name] = ServiceStatistics()
        return self.services[service_name]

    async def record_request(self, service_name: str, raw_body: bytes, method: str = "unknown") -> None:
        """Record an incoming request and detect duplicates/loops."""
        stats = self.get_service_stats(service_name)
        async with stats._lock:
            stats.total_requests += 1

            fingerprint = hashlib.sha256(raw_body).hexdigest()
            stats.seen_fingerprints[fingerprint] += 1
            seen = stats.seen_fingerprints[fingerprint]

            if seen > 1:
                stats.duplicate_requests += 1
            if seen > 2:
                stats.suspected_loops += 1

            stats.method_counts[method] += 1
            stats.recent_requests.append(
                {"fingerprint": fingerprint, "count": seen, "method": method}
            )

    async def record_fault(self, service_name: str) -> None:
        """Record an injected fault."""
        stats = self.get_service_stats(service_name)
        async with stats._lock:
            stats.injected_faults += 1
            stats.upstream_failures += 1

    async def record_latency(self, service_name: str) -> None:
        """Record a latency injection."""
        stats = self.get_service_stats(service_name)
        async with stats._lock:
            stats.latency_injections += 1

    async def record_success(self, service_name: str) -> None:
        """Record a successful upstream call."""
        stats = self.get_service_stats(service_name)
        async with stats._lock:
            stats.upstream_successes += 1

    async def record_failure(self, service_name: str) -> None:
        """Record an upstream failure (non-injected)."""
        stats = self.get_service_stats(service_name)
        async with stats._lock:
            stats.upstream_failures += 1

    def generate_scorecard(self, service_name: str) -> dict[str, Any]:
        """Generate a resilience scorecard for a service."""
        stats = self.get_service_stats(service_name)

        score = 100
        score -= stats.injected_faults * 3
        score -= stats.upstream_failures * 12
        score -= stats.duplicate_requests * 2
        score -= stats.suspected_loops * 10
        score = max(0, min(100, score))

        if stats.upstream_failures == 0 and stats.suspected_loops == 0:
            outcome = "PASS"
        elif stats.upstream_successes > 0:
            outcome = "DEGRADED"
        else:
            outcome = "FAIL"

        elapsed = time.monotonic() - stats.session_start_time
        rps = round(stats.total_requests / elapsed, 2) if elapsed > 0 else 0.0
        avg_processing_ms = (
            round(stats.total_processing_time_ms / stats.total_requests, 2)
            if stats.total_requests > 0
            else 0.0
        )

        return {
            "service_name": service_name,
            "requests_seen": stats.total_requests,
            "injected_faults": stats.injected_faults,
            "latency_injections": stats.latency_injections,
            "upstream_successes": stats.upstream_successes,
            "upstream_failures": stats.upstream_failures,
            "duplicate_requests": stats.duplicate_requests,
            "suspected_loops": stats.suspected_loops,
            "method_counts": dict(stats.method_counts),
            "recent_requests": list(stats.recent_requests),
            "run_outcome": outcome,
            "resilience_score": score,
            "avg_processing_ms": avg_processing_ms,
            "requests_per_second": rps,
        }
