# SPDX-License-Identifier: GPL-3.0-or-later
"""Compare an I²C capture from DSView or PXView against a raw 24C08 backup."""

import argparse
import configparser
import hashlib
import heapq
import itertools
import json
import re
import zipfile
from pathlib import Path


EEPROM_SIZE = 1024


def edges(raw, limit):
    previous = raw[0] & 1
    if raw.count(255 if previous else 0) == len(raw):
        return previous, []
    out = []
    for i, byte in enumerate(raw):
        changed = (byte ^ ((byte << 1) | previous)) & 255
        previous = byte >> 7
        while changed:
            bit = changed & -changed
            index = bit.bit_length() - 1
            sample = i * 8 + index
            if sample < limit:
                out.append((sample, (byte >> index) & 1))
            changed ^= bit
    return raw[0] & 1, out

def frames(scl_data, sda_data):
    scl, se = scl_data
    sda, de = sda_data
    joined = heapq.merge(
        ((sample, 0, level) for sample, level in se),
        ((sample, 1, level) for sample, level in de),
    )
    active = False
    words = []
    bits = []
    begin = 0
    for sample, group in itertools.groupby(joined, key=lambda x: x[0]):
        old_scl, old_sda = scl, sda
        for _, signal, level in group:
            if signal == 0:
                scl = level
            else:
                sda = level
        if old_scl == scl == 1 and old_sda != sda:
            if active and words:
                yield begin, sample, words, len(bits), bool(sda)
            active = not sda
            begin = sample
            words = []
            bits = []
        elif active and old_scl == 0 and scl == 1:
            bits.append(sda)
            if len(bits) == 9:
                b = sum(bits[i] << (7 - i) for i in range(8))
                words.append((b, bits[8] == 0))
                bits = []
    if active and words:
        yield begin, None, words, len(bits), False

def decode_eeprom(transactions):
    values = {}
    conflicts = set()
    pointer = None
    valid = []
    skipped_reads = []
    addresses = set()
    for _, _, words, tail, stop in transactions:
        wire, ack = words[0]
        address = wire >> 1
        read = bool(wire & 1)
        if address < 0x50 or address > 0x57 or not ack:
            pointer = None
            continue
        addresses.add(address)
        data = words[1:]
        if not read:
            pointer = (address, data[0][0]) if len(data) == 1 and data[0][1] and tail <= 1 else None
            if pointer is not None:
                valid.append({"address": address, "operation": "offset", "offset": pointer[1], "stop": stop})
            continue
        if len(data) == 0:
            pointer = None
            continue
        # Read control bytes can ignore block bits. The prior address set the pointer.
        if pointer is None or (pointer[0] & 4) != (address & 4):
            skipped_reads.append((address, len(data)))
            pointer = None
            continue
        first = ((pointer[0] & 3) << 8) | pointer[1]
        valid.append({"address": address, "operation": "read", "offset": first, "bytes": len(data), "stop": stop})
        for i, (value, ack) in enumerate(data):
            offset = (first + i) % 1024
            if offset in values and values[offset] != value:
                conflicts.add(offset)
            values[offset] = value
            if not ack:
                break
        pointer = None
    return values, conflicts, valid, skipped_reads, sorted(addresses)


def load_channels(capture):
    with zipfile.ZipFile(capture) as archive:
        groups = {}
        for name in archive.namelist():
            match = re.fullmatch(r"L-(\d+)/(\d+)", name)
            if match:
                channel, block = map(int, match.groups())
                groups.setdefault(channel, {})[block] = name
        if len(groups) < 2:
            raise ValueError("Capture must contain at least two logic channels")

        config = configparser.ConfigParser(interpolation=None, strict=False)
        config.read_string(archive.read("header").decode("utf-8", errors="replace"))
        header = config["header"]
        count = int(header["total samples"])
        channels = {}
        for channel, blocks in sorted(groups.items()):
            if sorted(blocks) != list(range(len(blocks))):
                raise ValueError(f"Missing capture block for channel {channel}")
            raw = b"".join(archive.read(blocks[i]) for i in sorted(blocks))
            if not raw:
                raise ValueError(f"No samples for channel {channel}")
            channels[channel] = edges(raw, min(count, len(raw) * 8))
    return channels, {
        "total_samples": count,
        "sample_rate": header.get("samplerate"),
        "transitions": {channel: len(data[1]) for channel, data in channels.items()},
    }


def compare_capture(capture, reference, scl=None, sda=None):
    if len(reference) != EEPROM_SIZE:
        raise ValueError("The comparison reference must contain exactly 1024 bytes")
    if (scl is None) != (sda is None) or (scl is not None and scl == sda):
        raise ValueError("Specify two distinct channels or omit both")

    channels, report = load_channels(capture)
    if scl is not None:
        if scl not in channels or sda not in channels:
            raise ValueError("Specified channel is absent from the capture")
        pairs = [(scl, sda)]
    else:
        pairs = itertools.permutations(channels, 2)

    candidates = []
    complete = []
    for clock, data in pairs:
        if len(channels[clock][1]) < 18 or len(channels[data][1]) < 2:
            continue
        transactions = list(frames(channels[clock], channels[data]))
        values, conflicts, valid, skipped, addresses = decode_eeprom(transactions)
        mismatches = [
            {"offset": f"0x{i:04X}", "reference": f"0x{reference[i]:02X}", "capture": f"0x{values[i]:02X}"}
            for i in sorted(values) if reference[i] != values[i]
        ]
        candidates.append({
            "scl": clock, "sda": data, "coverage": len(values),
            "matching_bytes": len(values) - len(mismatches), "different_bytes": len(mismatches),
            "first_differences": mismatches[:16], "conflicts": len(conflicts),
            "eeprom_addresses": [f"0x{address:02X}" for address in addresses],
            "addressed_transactions": valid[:25],
            "unanchored_reads": [[f"0x{address:02X}", count] for address, count in skipped[:12]],
            "i2c_transactions": len(transactions),
        })
        if len(values) == EEPROM_SIZE and not conflicts:
            complete.append(bytes(values[i] for i in range(EEPROM_SIZE)))

    candidates.sort(key=lambda result: (-result["coverage"], result["conflicts"]))
    report["candidates"] = candidates[:6]
    report["status"] = "partial"
    if len(complete) != 1:
        if len(complete) > 1:
            report["status"] = "ambiguous"
        return report, None

    reconstructed = complete[0]
    report["status"] = "matched" if reconstructed == reference else "different"
    report["capture_sha256"] = hashlib.sha256(reconstructed).hexdigest()
    report["reference_sha256"] = hashlib.sha256(reference).hexdigest()
    return report, reconstructed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path, help="DSView .dsl or PXView .pxl file")
    parser.add_argument("reference", type=Path, help="1024-byte EEPROM backup")
    parser.add_argument("--scl", type=int, help="SCL channel number")
    parser.add_argument("--sda", type=int, help="SDA channel number")
    parser.add_argument("--output", type=Path, help="Save the reconstructed 1024-byte image")
    args = parser.parse_args()
    report, reconstructed = compare_capture(
        args.capture, args.reference.read_bytes(), args.scl, args.sda,
    )
    if args.output is not None and reconstructed is not None:
        with args.output.open("xb") as file:
            file.write(reconstructed)
        report["output"] = str(args.output)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
