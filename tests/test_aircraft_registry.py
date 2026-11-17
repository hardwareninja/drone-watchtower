"""A serial is resolved once against the local aircraft list."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "webapp" / "backend"))

import aircraft_registry


def test_the_same_serial_is_resolved_once():
    registry = aircraft_registry.LocalRegistry()
    table = {"SN100": "Mini 3"}
    assert registry.resolve("SN100", table) == "Mini 3"
    assert registry.resolve("SN100", table) == "Mini 3"
    assert registry.lookups == 1


def test_an_unknown_serial_is_cached_too():
    registry = aircraft_registry.LocalRegistry()
    table = {"SN100": "Mini 3"}
    assert registry.resolve("OTHER", table) is None
    assert registry.resolve("OTHER", table) is None
    assert registry.lookups == 1


def test_a_changed_list_is_resolved_again():
    registry = aircraft_registry.LocalRegistry()
    assert registry.resolve("SN100", {"SN100": "Mini 3"}) == "Mini 3"
    assert registry.resolve("SN100", {"SN100": "Avata"}) == "Avata"
    assert registry.lookups == 2


def test_a_hostile_display_name_cannot_carry_markup():
    registry = aircraft_registry.LocalRegistry()
    name = registry.resolve("SN100", {"SN100": "<script>alert(1)</script>Mini"})
    assert name is not None
    assert "<" not in name
    assert "script" in name
