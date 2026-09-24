"""Small synthetic flat CFB/FIB files; no real corpus content."""

import struct

FREE = 0xFFFFFFFF
END = 0xFFFFFFFE


def binary_doc(main="第一条 合成正文。\r", header="", *, compressed=False):
    combined = main + header + ("\r" if header else "")
    word = bytearray(8192)
    struct.pack_into("<HH", word, 0, 0xA5EC, 0xC1)
    struct.pack_into("<H", word, 10, 0x1000)
    struct.pack_into("<H", word, 12, 0xBF)
    struct.pack_into("<H", word, 32, 14)
    struct.pack_into("<H", word, 62, 22)
    struct.pack_into("<I", word, 64, len(word))
    struct.pack_into("<I", word, 76, len(main.encode("utf-16-le")) // 2)
    struct.pack_into("<I", word, 84, len(header.encode("utf-16-le")) // 2)
    struct.pack_into("<H", word, 152, 164)
    struct.pack_into("<HH", word, 154 + 164 * 8, 2, 0x10C)
    table = bytearray(4096)
    cp_total = len(combined.encode("utf-16-le")) // 2
    clx = b"\x02" + struct.pack("<I", 16) + struct.pack("<II", 0, cp_total)
    clx += struct.pack("<HIH", 0, 0x40001000 if compressed else 2048, 0)
    table[256 : 256 + len(clx)] = clx
    struct.pack_into("<II", word, 154 + 33 * 8, 256, len(clx))
    word[2048 : 2048 + len(combined.encode("utf-16-le"))] = combined.encode("utf-16-le")
    next_table = 512
    for index, story in [(16, main), (17, header)]:
        markers = [(i, ord(char)) for i, char in enumerate(story) if char in "\x13\x14\x15"]
        if markers:
            plc = b"".join(struct.pack("<I", cp) for cp, _ in markers)
            plc += struct.pack("<I", cp_total)
            plc += b"".join(bytes((char, 0)) for _, char in markers)
            table[next_table : next_table + len(plc)] = plc
            struct.pack_into("<II", word, 154 + index * 8, next_table, len(plc))
            next_table += len(plc)
    if header:
        struct.pack_into("<II", word, 154 + 11 * 8, 768, 8)
        struct.pack_into("<II", table, 768, 0, len(header) + 2)
    # Sectors: directory=0, WordDocument=1..16, 0Table=17..24, FAT=25.
    header_bytes = bytearray(512)
    header_bytes[:8] = bytes.fromhex("d0cf11e0a1b11ae1")
    struct.pack_into("<HHHH", header_bytes, 24, 0x3E, 3, 0xFFFE, 9)
    struct.pack_into("<H", header_bytes, 32, 6)
    struct.pack_into("<IIIIIIIII", header_bytes, 40, 0, 1, 0, 0, 4096, END, 0, END, 0)
    for index in range(109):
        struct.pack_into("<I", header_bytes, 76 + 4 * index, 25 if index == 0 else FREE)
    directory = bytearray(512)
    for index, (name, kind, start, size, right, child) in enumerate(
        [
            ("Root Entry", 5, END, 0, FREE, 1),
            ("WordDocument", 2, 1, len(word), 2, FREE),
            ("0Table", 2, 17, len(table), FREE, FREE),
        ]
    ):
        pos = index * 128
        encoded = (name + "\0").encode("utf-16-le")
        directory[pos : pos + len(encoded)] = encoded
        struct.pack_into("<HBBIII", directory, pos + 64, len(encoded), kind, 1, FREE, right, child)
        struct.pack_into("<IQ", directory, pos + 116, start, size)
    fat = [FREE] * 128
    fat[0] = END
    for start, finish in [(1, 16), (17, 24)]:
        for sid in range(start, finish):
            fat[sid] = sid + 1
        fat[finish] = END
    fat[25] = 0xFFFFFFFD
    return bytes(header_bytes + directory + word + table + struct.pack("<128I", *fat))


def patch_word(payload, offset, fmt, *values):
    result = bytearray(payload)
    struct.pack_into(fmt, result, 1024 + offset, *values)
    return bytes(result)


def with_mini_stream(payload, *, crosslink=False):
    result = bytearray(payload)
    # Append mini-stream and MiniFAT sectors after the existing FAT sector.
    root_size = 128 if crosslink else 64
    struct.pack_into("<IQ", result, 512 + 116, 26, root_size)
    struct.pack_into("<II", result, 60, 27, 1)
    struct.pack_into("<II", result, 26 * 512 + 26 * 4, END, END)
    result += b"synthetic metadata".ljust(512, b"\0")
    result += struct.pack("<128I", END, *([FREE] * 127))
    entries = [(3, "WpsCustomData", 4 if crosslink else FREE)]
    struct.pack_into("<I", result, 512 + 2 * 128 + 72, 3)
    if crosslink:
        # A second directory sector lets two streams intentionally claim mini 0.
        struct.pack_into("<I", result, 26 * 512, 28)
        struct.pack_into("<I", result, 26 * 512 + 28 * 4, END)
        result += bytes(512)
        entries.append((4, "\x05SummaryInformation", FREE))
    for index, name, right in entries:
        pos = 512 + index * 128 if index < 4 else 29 * 512
        encoded = (name + "\0").encode("utf-16-le")
        result[pos : pos + len(encoded)] = encoded
        struct.pack_into("<HBBIII", result, pos + 64, len(encoded), 2, 1, FREE, right, FREE)
        struct.pack_into("<IQ", result, pos + 116, 0, 18)
    return bytes(result)
