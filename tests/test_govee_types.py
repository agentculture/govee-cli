"""Tests for govee_cli.govee core types and the recording fake transport."""

import dataclasses

import pytest

from govee_cli.govee.fake import FakeTransport
from govee_cli.govee.types import Command, Device, LampState, Transport, kelvin_to_rgb


def _device() -> Device:
    return Device(id="AA:BB", name="Lamp", model="H6008", ip="192.168.1.5", transports=("lan",))


def test_dataclasses_are_frozen():
    for obj in (
        _device(),
        LampState(on=True, brightness=50),
        Command("on", "lan", {"cmd": "turn"}),
    ):
        name = dataclasses.fields(obj)[0].name
        with pytest.raises(dataclasses.FrozenInstanceError):
            obj.__setattr__(name, "x")


def test_lampstate_defaults():
    s = LampState(on=False, brightness=1)
    assert s.color is None
    assert s.kelvin is None
    assert s.scene is None


def test_command_fields():
    c = Command(verb="on", transport="cloud", payload={"a": 1})
    assert (c.verb, c.transport, c.payload) == ("on", "cloud", {"a": 1})


@pytest.mark.parametrize("k", [2000, 6500, 9000])
def test_kelvin_to_rgb_range(k):
    rgb = kelvin_to_rgb(k)
    assert len(rgb) == 3
    assert all(0 <= c <= 255 and isinstance(c, int) for c in rgb)


def test_kelvin_to_rgb_endpoints():
    warm, mid, cool = kelvin_to_rgb(2000), kelvin_to_rgb(6500), kelvin_to_rgb(9000)
    assert warm[0] == 255  # warm is red-heavy
    assert warm[2] < warm[0]
    assert mid[0] >= 250  # ~white
    assert mid[1] >= 250
    assert mid[2] >= 250
    assert cool[2] == 255  # cool is blue-heavy
    assert cool[0] < cool[2]


def test_kelvin_clamps():
    assert kelvin_to_rgb(100) == kelvin_to_rgb(2000)
    assert kelvin_to_rgb(20000) == kelvin_to_rgb(9000)


def test_fake_is_transport():
    assert isinstance(FakeTransport(), Transport)


def test_fake_records_sends():
    f = FakeTransport()
    d = _device()
    c1, c2 = Command("on", "lan", {"x": 1}), Command("off", "lan", {"x": 2})
    assert f.send(d, c1) is True
    f.send(d, c2)
    assert f.sent == [(d, c1), (d, c2)]


def test_fake_drop_packets_records_but_reports_failure():
    f = FakeTransport(drop=True)
    d = _device()
    c = Command("on", "lan", {})
    assert f.send(d, c) is False
    assert f.sent == [(d, c)]


def test_fake_scripted_state_and_discover():
    st = LampState(on=True, brightness=80, kelvin=4000)
    d = _device()
    f = FakeTransport(devices=[d], state=st)
    assert f.discover() == [d]
    assert f.get_state(d) == st


def test_fake_state_none_by_default():
    assert FakeTransport().get_state(_device()) is None
