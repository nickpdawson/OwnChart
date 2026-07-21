"""P1-8a: trusted-proxy client-IP resolution.

The old limiter took X-Forwarded-For's leftmost value unconditionally, so any
client could spoof `X-Forwarded-For: 1.2.3.4` and dodge a per-IP throttle.
These pin the new rule: forwarded headers are believed ONLY when the direct
socket peer is a configured trusted proxy; otherwise the peer wins and the
headers are ignored.
"""

from __future__ import annotations

from ownchart.core.rate_limit import extract_client_ip

NPM = "10.25.1.182"
NPM2 = "10.25.1.183"
CLIENT = "203.0.113.7"


def test_no_peer_is_unknown():
    assert extract_client_ip(
        peer=None, xff="1.2.3.4", cf_connecting_ip=None, trusted_cidrs=[],
    ) == "unknown"


def test_untrusted_peer_ignores_spoofed_xff():
    # Direct client sends a spoofed XFF; with no trusted proxies we must use
    # the socket peer and ignore the header entirely.
    assert extract_client_ip(
        peer="198.51.100.9",
        xff="1.2.3.4",
        cf_connecting_ip="5.6.7.8",
        trusted_cidrs=[],
    ) == "198.51.100.9"


def test_untrusted_peer_with_configured_proxies_still_ignores_xff():
    # A trust list exists, but THIS peer isn't in it → still peer-only.
    assert extract_client_ip(
        peer="198.51.100.9",
        xff=CLIENT,
        cf_connecting_ip=None,
        trusted_cidrs=[f"{NPM}/32"],
    ) == "198.51.100.9"


def test_one_trusted_hop_extracts_client():
    assert extract_client_ip(
        peer=NPM,
        xff=CLIENT,
        cf_connecting_ip=None,
        trusted_cidrs=[f"{NPM}/32"],
    ) == CLIENT


def test_two_trusted_hops_walk_right_to_left():
    # chain: client -> NPM2 -> NPM (peer). XFF="client, NPM2". Walking
    # right-to-left skips the trusted NPM2 hop and returns the client.
    assert extract_client_ip(
        peer=NPM,
        xff=f"{CLIENT}, {NPM2}",
        cf_connecting_ip=None,
        trusted_cidrs=[f"{NPM}/32", f"{NPM2}/32"],
    ) == CLIENT


def test_cidr_block_matches_trusted_peer():
    assert extract_client_ip(
        peer="173.245.48.10",
        xff=CLIENT,
        cf_connecting_ip=None,
        trusted_cidrs=["173.245.48.0/20"],
    ) == CLIENT


def test_cf_connecting_ip_wins_when_peer_trusted():
    assert extract_client_ip(
        peer=NPM,
        xff=f"{CLIENT}, {NPM}",
        cf_connecting_ip="203.0.113.99",
        trusted_cidrs=[f"{NPM}/32"],
    ) == "203.0.113.99"


def test_cf_connecting_ip_ignored_when_peer_untrusted():
    # CF-Connecting-IP is only meaningful behind a trusted proxy; a direct
    # client sending it must not be believed.
    assert extract_client_ip(
        peer="198.51.100.9",
        xff=None,
        cf_connecting_ip="203.0.113.99",
        trusted_cidrs=[f"{NPM}/32"],
    ) == "198.51.100.9"


def test_all_hops_trusted_falls_back_to_peer():
    assert extract_client_ip(
        peer=NPM,
        xff=f"{NPM2}, {NPM}",
        cf_connecting_ip=None,
        trusted_cidrs=[f"{NPM}/32", f"{NPM2}/32"],
    ) == NPM


def test_invalid_xff_hop_skipped():
    assert extract_client_ip(
        peer=NPM,
        xff=f"garbage, {CLIENT}",
        cf_connecting_ip=None,
        trusted_cidrs=[f"{NPM}/32"],
    ) == CLIENT


def test_malformed_trusted_cidr_is_skipped_not_trusted():
    # A bad allowlist entry must not accidentally trust the peer.
    assert extract_client_ip(
        peer="198.51.100.9",
        xff=CLIENT,
        cf_connecting_ip=None,
        trusted_cidrs=["not-a-cidr"],
    ) == "198.51.100.9"
