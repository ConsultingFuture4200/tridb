"""Baseline compose must bind published ports to loopback by default (advisor plan 083).

The baseline stack ships development credentials, so unqualified Compose port
mappings ("7474:7474" binds 0.0.0.0) would expose Neo4j, MinIO, Milvus, and
Postgres to the surrounding network on any box that runs the documented
`make baseline-up`. Every published mapping must therefore be prefixed with
`${BASELINE_BIND:-127.0.0.1}:` — loopback unless deliberately overridden.

These tests are static (YAML parse) plus an optional `docker compose config`
render check; they never bring the stack up.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

COMPOSE_PATH = Path(__file__).resolve().parents[1] / "baseline" / "docker-compose.yml"

BIND_PREFIX = "${BASELINE_BIND:-127.0.0.1}:"

# Every service expected to publish host ports, with the host-port token of each
# mapping (after the bind prefix). Named explicitly so a new/renamed published
# port fails the test instead of passing vacuously.
EXPECTED_PUBLISHED = {
    "neo4j": ["7474", "7687"],
    "minio": ["9000", "9001"],
    "milvus": ["19530", "9091"],
    "postgres": ["${BASELINE_PG_PORT:-5432}"],
}

# Services that must NOT publish any host ports (internal-only deps).
EXPECTED_UNPUBLISHED = {"etcd"}


def _load_services():
    with COMPOSE_PATH.open() as f:
        doc = yaml.safe_load(f)
    return doc["services"]


def test_compose_service_set_is_covered():
    """Every service in the file is accounted for — no unaudited port surface."""
    services = _load_services()
    assert set(services) == set(EXPECTED_PUBLISHED) | EXPECTED_UNPUBLISHED


def test_unpublished_services_expose_no_host_ports():
    services = _load_services()
    for name in EXPECTED_UNPUBLISHED:
        assert "ports" not in services[name], f"{name} must not publish host ports"


@pytest.mark.parametrize("service", sorted(EXPECTED_PUBLISHED))
def test_published_ports_bind_loopback_by_default(service):
    services = _load_services()
    ports = services[service].get("ports", [])
    assert ports, f"{service} should publish ports {EXPECTED_PUBLISHED[service]}"

    seen_host_ports = []
    for mapping in ports:
        assert isinstance(mapping, str), (
            f"{service}: use string 'bind:host:container' mappings, got {mapping!r}"
        )
        assert mapping.startswith(BIND_PREFIX), (
            f"{service}: {mapping!r} must start with {BIND_PREFIX!r} "
            "(bare host:container binds all interfaces)"
        )
        rest = mapping[len(BIND_PREFIX) :]
        host_port, sep, container_port = rest.rpartition(":")
        assert sep and host_port and container_port, (
            f"{service}: {mapping!r} is not bind:host:container"
        )
        seen_host_ports.append(host_port)

    assert seen_host_ports == EXPECTED_PUBLISHED[service]


docker_available = shutil.which("docker") is not None


@pytest.mark.skipif(not docker_available, reason="docker CLI not available")
@pytest.mark.parametrize(
    ("bind_env", "expected_ip"),
    [(None, "127.0.0.1"), ("0.0.0.0", "0.0.0.0")],
    ids=["default-loopback", "explicit-override"],
)
def test_compose_config_renders_bind(bind_env, expected_ip):
    """`docker compose config` (read-only render) resolves the bind variable."""
    cmd = ["env", "-u", "BASELINE_BIND"]
    if bind_env is not None:
        cmd += [f"BASELINE_BIND={bind_env}"]
    cmd += [
        "docker",
        "compose",
        "-f",
        str(COMPOSE_PATH),
        "config",
        "--format",
        "json",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        pytest.skip(f"docker compose config unavailable: {proc.stderr.strip()[:200]}")
    rendered = json.loads(proc.stdout)

    published = 0
    for name, svc in rendered["services"].items():
        for port in svc.get("ports", []):
            published += 1
            assert port.get("host_ip") == expected_ip, (
                f"{name}: published port {port} not bound to {expected_ip}"
            )
    expected_count = sum(len(v) for v in EXPECTED_PUBLISHED.values())
    assert published == expected_count
