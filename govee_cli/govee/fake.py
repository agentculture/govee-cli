"""Recording fake transport for tests."""

from __future__ import annotations

from typing import Optional

from govee_cli.govee.types import Command, Device, LampState


class FakeTransport:
    """Records every ``send``; can drop packets or return a scripted state."""

    def __init__(
        self,
        devices: Optional[list[Device]] = None,
        state: Optional[LampState] = None,
        drop: bool = False,
    ) -> None:
        self.devices = list(devices or [])
        self.state = state
        self.drop = drop
        self.sent: list[tuple[Device, Command]] = []

    def discover(self) -> list[Device]:
        return list(self.devices)

    def get_state(self, _device: Device) -> Optional[LampState]:
        return self.state

    def send(self, device: Device, command: Command) -> bool:
        self.sent.append((device, command))
        return not self.drop
