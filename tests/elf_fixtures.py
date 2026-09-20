# SPDX-License-Identifier: Apache-2.0

"""Deterministic ELF byte fixtures for hardware-support tests."""

from __future__ import annotations

import struct

_ELF_HEADER_SIZE = 64
_PROGRAM_HEADER_SIZE = 56
_PROGRAM_HEADER_COUNT = 1
_SECTION_HEADER_SIZE = 64
_SECTION_HEADER_COUNT = 3
_PROGRAM_HEADERS_OFFSET = 0x40
_SECTION_HEADERS_OFFSET = 0x200
_ELF_SIZE = _SECTION_HEADERS_OFFSET + _SECTION_HEADER_COUNT * _SECTION_HEADER_SIZE

_PT_LOAD = 1
_PF_R = 4
_ET_EXEC = 2
_EM_X86_64 = 62
_EV_CURRENT = 1
_ELFCLASS64 = 2
_ELFDATA2LSB = 1
_ELF_OSABI_SYSV = 0
_SHT_PROGBITS = 1
_SHT_STRTAB = 3
_SHF_ALLOC = 2

ELF_LOAD_ADDRESS = 0x400000
ELF_SHIFTED_LOAD_VADDR = ELF_LOAD_ADDRESS + 1
_LOAD_FILE_OFFSET = 0
_LOAD_SIZE = 0x180
_LOAD_ALIGNMENT = 1

ELF_WITNESS_OFFSET = 0x120
ELF_WITNESS_ADDRESS = ELF_LOAD_ADDRESS + ELF_WITNESS_OFFSET
ELF_WITNESS_BYTES = bytes(range(0x10, 0x30))
_WITNESS_ALIGNMENT = 16
_SECTION_NAMES_OFFSET = 0x180
_SECTION_NAMES = b"\0.witness\0.shstrtab\0"
_WITNESS_NAME_OFFSET = _SECTION_NAMES.index(b".witness")
_SHSTRTAB_NAME_OFFSET = _SECTION_NAMES.index(b".shstrtab")
_WITNESS_SECTION_INDEX = 1
_SHSTRTAB_SECTION_INDEX = 2

# This byte is inside the first load segment, after its ELF/program headers,
# but outside every allocated section.
ELF_PADDING_OFFSET = 0xC0

# In an ELF64 program header, p_vaddr begins 16 bytes into the record.
ELF_LOAD_VADDR_OFFSET = _PROGRAM_HEADERS_OFFSET + 16


def elf_memory_witness_bytes() -> bytes:
    """Return a small ELF with one loadable section and controlled padding."""
    assert ELF_LOAD_ADDRESS % _LOAD_ALIGNMENT == _LOAD_FILE_OFFSET % _LOAD_ALIGNMENT
    assert ELF_SHIFTED_LOAD_VADDR % _LOAD_ALIGNMENT == _LOAD_FILE_OFFSET % _LOAD_ALIGNMENT
    image = bytearray(_ELF_SIZE)
    identifier = b"\x7fELF" + bytes(
        (_ELFCLASS64, _ELFDATA2LSB, _EV_CURRENT, _ELF_OSABI_SYSV, 0, *([0] * 7))
    )
    # ELF64 header fields follow the order defined by the System V ABI.
    struct.pack_into(
        "<16sHHIQQQIHHHHHH",
        image,
        0,
        identifier,
        _ET_EXEC,
        _EM_X86_64,
        _EV_CURRENT,
        0,
        _PROGRAM_HEADERS_OFFSET,
        _SECTION_HEADERS_OFFSET,
        0,
        _ELF_HEADER_SIZE,
        _PROGRAM_HEADER_SIZE,
        _PROGRAM_HEADER_COUNT,
        _SECTION_HEADER_SIZE,
        _SECTION_HEADER_COUNT,
        _SHSTRTAB_SECTION_INDEX,
    )
    # ELF64 program headers: type, flags, file offset, virtual address,
    # physical address, file size, memory size, and alignment.
    struct.pack_into(
        "<IIQQQQQQ",
        image,
        _PROGRAM_HEADERS_OFFSET,
        _PT_LOAD,
        _PF_R,
        _LOAD_FILE_OFFSET,
        ELF_LOAD_ADDRESS,
        ELF_LOAD_ADDRESS,
        _LOAD_SIZE,
        _LOAD_SIZE,
        _LOAD_ALIGNMENT,
    )

    image[ELF_WITNESS_OFFSET : ELF_WITNESS_OFFSET + len(ELF_WITNESS_BYTES)] = ELF_WITNESS_BYTES
    image[_SECTION_NAMES_OFFSET : _SECTION_NAMES_OFFSET + len(_SECTION_NAMES)] = _SECTION_NAMES
    # ELF64 section headers: name, type, flags, address, file offset, size,
    # link, info, alignment, and entry size. The all-zero header is implicit.
    struct.pack_into(
        "<IIQQQQIIQQ",
        image,
        _SECTION_HEADERS_OFFSET + _WITNESS_SECTION_INDEX * _SECTION_HEADER_SIZE,
        _WITNESS_NAME_OFFSET,
        _SHT_PROGBITS,
        _SHF_ALLOC,
        ELF_WITNESS_ADDRESS,
        ELF_WITNESS_OFFSET,
        len(ELF_WITNESS_BYTES),
        0,
        0,
        _WITNESS_ALIGNMENT,
        0,
    )
    struct.pack_into(
        "<IIQQQQIIQQ",
        image,
        _SECTION_HEADERS_OFFSET + _SHSTRTAB_SECTION_INDEX * _SECTION_HEADER_SIZE,
        _SHSTRTAB_NAME_OFFSET,
        _SHT_STRTAB,
        0,
        0,
        _SECTION_NAMES_OFFSET,
        len(_SECTION_NAMES),
        0,
        0,
        1,
        0,
    )

    assert len(image) == _ELF_SIZE
    assert image[ELF_PADDING_OFFSET] == 0
    assert (
        image[ELF_WITNESS_OFFSET : ELF_WITNESS_OFFSET + len(ELF_WITNESS_BYTES)] == ELF_WITNESS_BYTES
    )
    return bytes(image)
