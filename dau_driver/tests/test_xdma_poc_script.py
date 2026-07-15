from __future__ import annotations

import importlib.util
import struct
from pathlib import Path

from dau_core.registers import DAU_MAGIC_WORD, RegisterOffset, StreamProtocolVersion

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "dau-xdma-poc.py"
REGISTER_WINDOW_OFFSET = 0x1000
REGISTER_WINDOW_BYTES = 0x1000


def load_poc_module():
    spec = importlib.util.spec_from_file_location("dau_xdma_poc", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


poc = load_poc_module()


def write_composition_register_image(user_path: Path, *, operator_bitmap: int = 0x1E, register_window_offset: int = REGISTER_WINDOW_OFFSET) -> None:
    """BAR image of a composition top: the identity fallthrough reads all-ones
    everywhere except the mapped window-relative contract registers."""
    image = bytearray(b"\xff" * (register_window_offset + REGISTER_WINDOW_BYTES))
    for offset, value in (
        (RegisterOffset.MAGIC, DAU_MAGIC_WORD),
        (RegisterOffset.STREAM_PROTOCOL_VERSION, StreamProtocolVersion(0, 1).to_u32()),
        (RegisterOffset.OPERATOR_BITMAP, operator_bitmap),
    ):
        struct.pack_into("<I", image, register_window_offset + int(offset), value)
    user_path.write_bytes(image)


def test_health_offsets_are_mapped_contract_registers_inside_the_window() -> None:
    offsets = [REGISTER_WINDOW_OFFSET + int(offset) for offset in poc.HEALTH_REGISTER_OFFSETS]
    assert offsets == [0x1000, 0x1008, 0x1028]
    for offset in offsets:
        assert REGISTER_WINDOW_OFFSET <= offset < REGISTER_WINDOW_OFFSET + REGISTER_WINDOW_BYTES


def test_bar_health_passes_on_composition_top_register_image(tmp_path: Path) -> None:
    user_path = tmp_path / "xdma0_user"
    write_composition_register_image(user_path)

    health = poc.bar_health(str(user_path), REGISTER_WINDOW_OFFSET)

    assert health[REGISTER_WINDOW_OFFSET + int(RegisterOffset.MAGIC)] == DAU_MAGIC_WORD
    assert health[REGISTER_WINDOW_OFFSET + int(RegisterOffset.STREAM_PROTOCOL_VERSION)] == StreamProtocolVersion(0, 1).to_u32()
    assert health[REGISTER_WINDOW_OFFSET + int(RegisterOffset.OPERATOR_BITMAP)] == 0x1E
    assert all(value != 0xFFFFFFFF for value in health.values())


def test_bar_health_offsets_follow_a_non_default_register_window(tmp_path: Path) -> None:
    window = 0x2000
    user_path = tmp_path / "xdma0_user"
    write_composition_register_image(user_path, register_window_offset=window)

    health = poc.bar_health(str(user_path), window)

    assert set(health) == {window + int(offset) for offset in poc.HEALTH_REGISTER_OFFSETS}
    assert health[window + int(RegisterOffset.MAGIC)] == DAU_MAGIC_WORD
    assert all(value != 0xFFFFFFFF for value in health.values())


def test_bar_health_still_surfaces_all_ones_on_a_mapped_register(tmp_path: Path) -> None:
    user_path = tmp_path / "xdma0_user"
    write_composition_register_image(user_path, operator_bitmap=0xFFFF_FFFF)

    health = poc.bar_health(str(user_path), REGISTER_WINDOW_OFFSET)

    assert any(value == 0xFFFFFFFF for value in health.values())
