#!/usr/bin/env python3
"""
obis_probe.py - findet heraus, was ein Stromzähler an der optischen Schnittstelle sendet.

Lesekopf (TTL) an einen USB-Seriell-Adapter (FTDI) hängen:
    Lesekopf TX -> FTDI RX,  Lesekopf GND -> FTDI GND,  Lesekopf VCC -> FTDI VCC (Spannung des Kopfs beachten)

Aufruf (Python 3.8+, einmalig: pip install pyserial):
    python obis_probe.py --list                       # verfügbare COM-Ports anzeigen
    python obis_probe.py COM5                          # alle Baudraten/Datenformate durchprobieren
    python obis_probe.py COM5 --baud 9600 --format 7E1 --seconds 20 --raw dump.bin
                                                       # eine Einstellung länger mitschneiden und auswerten
    python obis_probe.py --selftest                    # Auswertung ohne Hardware prüfen

Der Scan liest jede Kombination einige Sekunden lang und bewertet die Daten:
SML (Startsequenz 1B1B1B1B 01010101) oder OBIS-ASCII (Zeilen wie 1-0:1.8.0*255(...)).
Am Ende steht, welche ETS-Einstellung zum Zähler passt.
"""

import argparse
import re
import sys
import time

BAUDRATES = [300, 1200, 2400, 4800, 9600, 19200]
FORMATS = ["8N1", "7E1"]

SML_START = b"\x1b\x1b\x1b\x1b\x01\x01\x01\x01"
# Kennungszeile nach IEC 62056-21: '/', 3 Buchstaben Hersteller, 1 Zeichen Baudrate, druckbarer Rest
IDENT_LINE = re.compile(r"^/[A-Za-z]{3}[0-9A-Z][\x21-\x7e]{1,32}$")
OBIS_LINE = re.compile(
    r"^(?:(\d{1,3})-(\d{1,3}):)?([0-9]{1,3}|[A-Za-z])\.(\d{1,3})\.(\d{1,3})(?:\*(\d{1,3}))?\(([^)]*)\)"
)


# ---------------------------------------------------------------- Auswertung (ohne Hardware testbar)

def mask7(data: bytes) -> bytes:
    """Bit 7 ausblenden: 7E1-Daten, die als 8N1 gelesen wurden, werden so wieder zu ASCII."""
    return bytes(b & 0x7F for b in data)


def printable_ratio(data: bytes) -> float:
    if not data:
        return 0.0
    ok = sum(1 for b in data if 0x20 <= b <= 0x7E or b in (0x0D, 0x0A, 0x02, 0x03))
    return ok / len(data)


def even_parity_ratio(data: bytes) -> float:
    """Anteil der Bytes, deren Bit 7 genau das gerade Paritätsbit der unteren 7 Bit ist (Hinweis auf 7E1)."""
    if not data:
        return 0.0
    ok = 0
    for b in data:
        parity = bin(b & 0x7F).count("1") & 1
        if ((b >> 7) & 1) == parity:
            ok += 1
    return ok / len(data)


def ascii_lines(data: bytes):
    text = mask7(data).decode("ascii", errors="replace")
    return [line.strip("\r\x02\x03") for line in text.split("\n")]


def analyze(data: bytes) -> dict:
    """Bewertet einen Mitschnitt. Je höher score, desto wahrscheinlicher passt die Einstellung."""
    lines = ascii_lines(data)
    obis = [l for l in lines if OBIS_LINE.match(l)]
    idents = [l for l in lines if IDENT_LINE.match(l)]
    ends = [l for l in lines if l == "!"]
    sml = data.count(SML_START)
    pr_raw = printable_ratio(data)
    pr_masked = printable_ratio(mask7(data))
    res = {
        "bytes": len(data),
        "sml_frames": sml,
        "obis_lines": len(obis),
        "idents": idents[:3],
        "telegram_ends": len(ends),
        "printable_raw": pr_raw,
        "printable_masked": pr_masked,
        "even_parity": even_parity_ratio(data),
    }
    if sml:
        res["kind"] = "SML"
    elif obis or idents:
        res["kind"] = "OBIS-ASCII"
    elif not data:
        res["kind"] = "keine Daten"
    else:
        res["kind"] = "unbekannt"
    res["score"] = sml * 50 + len(obis) * 10 + len(idents) * 20 + len(ends) * 5 + int(pr_masked * 10)
    return res


def extract_telegram(data: bytes):
    """Erstes vollständiges ASCII-Telegramm von '/' bis '!' als Zeilenliste, sonst None."""
    lines = ascii_lines(data)
    start = None
    for i, line in enumerate(lines):
        if IDENT_LINE.match(line):
            start = i
        elif line == "!" and start is not None:
            return lines[start:i + 1]
    return None


def parse_obis(line: str):
    m = OBIS_LINE.match(line)
    if not m:
        return None
    a, b, c, d, e, f, inner = m.groups()
    value, unit = (inner.rsplit("*", 1) + [""])[:2] if "*" in inner else (inner, "")
    code = "%s-%s:%s.%s.%s" % (a or "1", b or "0", c, d, e)
    return code, value, unit


def trim_meter_id(value: str) -> str:
    while len(value) > 14 and value.startswith("0"):
        value = value[1:]
    return value


def describe_telegram(lines) -> str:
    known = {
        "96.1.0": "Zähler-ID", "1.8.0": "Bezug gesamt", "1.8.1": "Bezug Tarif 1", "1.8.2": "Bezug Tarif 2",
        "2.8.0": "Einspeisung gesamt", "16.7.0": "Wirkleistung gesamt", "36.7.0": "Leistung L1",
        "56.7.0": "Leistung L2", "76.7.0": "Leistung L3", "32.7.0": "Spannung L1", "52.7.0": "Spannung L2",
        "72.7.0": "Spannung L3", "31.7.0": "Strom L1", "51.7.0": "Strom L2", "71.7.0": "Strom L3",
        "14.7.0": "Frequenz", "0.2.0": "Firmware", "96.50.1": "Hersteller",
    }
    out = []
    for line in lines:
        if line.startswith("/"):
            out.append("  Kennung      %s" % line[1:])
            continue
        p = parse_obis(line)
        if not p:
            if line != "!":
                out.append("  (unbekannt)  %s" % line)
            continue
        code, value, unit = p
        short = code.split(":", 1)[1]
        name = known.get(short, "")
        if short == "96.1.0":
            value = "%s  -> im KO: %s" % (value, trim_meter_id(value))
        out.append("  %-12s %-22s %s %s" % (short, name, value, unit))
    return "\n".join(out)


# ---------------------------------------------------------------- Hardware

def open_port(port: str, baud: int, fmt: str):
    import serial
    bytesize = serial.SEVENBITS if fmt == "7E1" else serial.EIGHTBITS
    parity = serial.PARITY_EVEN if fmt == "7E1" else serial.PARITY_NONE
    return serial.serial_for_url(port, baudrate=baud, bytesize=bytesize, parity=parity,
                                 stopbits=serial.STOPBITS_ONE, timeout=0.2)


def capture(port: str, baud: int, fmt: str, seconds: float) -> bytes:
    buf = bytearray()
    with open_port(port, baud, fmt) as s:
        s.reset_input_buffer()
        end = time.time() + seconds
        while time.time() < end:
            buf += s.read(4096)
    return bytes(buf)


def hexdump(data: bytes, n: int = 64) -> str:
    chunk = data[:n]
    hx = " ".join("%02X" % b for b in chunk)
    tx = "".join(chr(b & 0x7F) if 0x20 <= (b & 0x7F) <= 0x7E else "." for b in chunk)
    return "  hex: %s\n  txt: %s" % (hx, tx)


def report_single(data: bytes, baud: int, fmt: str):
    r = analyze(data)
    print("\n%d Baud %s: %d Bytes, erkannt: %s" % (baud, fmt, r["bytes"], r["kind"]))
    if not data:
        print("  Keine Daten. Lesekopf richtig herum und mittig auf der Diode? TX/RX vertauscht? Spannung?")
        return
    print(hexdump(data))
    print("  SML-Startsequenzen: %d, OBIS-Zeilen: %d, Telegrammenden '!': %d"
          % (r["sml_frames"], r["obis_lines"], r["telegram_ends"]))
    if fmt == "8N1" and r["printable_masked"] > 0.9 and r["printable_raw"] < 0.9 and r["even_parity"] > 0.9:
        print("  Bit 7 ist das gerade Paritätsbit: der Zähler sendet 7E1.")
    tel = extract_telegram(data)
    if tel:
        print("\n  Vollständiges Telegramm:")
        print(describe_telegram(tel))
    elif r["kind"] == "OBIS-ASCII":
        print("  Noch kein vollständiges Telegramm (/ ... !). Länger mitschneiden: --seconds 30")


def recommend(best):
    baud, fmt, r = best
    print("\n=== Ergebnis ===")
    if r["kind"] == "SML":
        print("Der Zähler sendet SML mit %d Baud." % baud)
        print("ETS Kanal: Protokoll SML, Baudrate %d, Datenformat Automatisch (8N1)." % baud)
    elif r["kind"] == "OBIS-ASCII":
        fmt_hint = "7E1" if (fmt == "7E1" or r["even_parity"] > 0.9) else "8N1"
        print("Der Zähler sendet OBIS-ASCII mit %d Baud (%s)." % (baud, fmt_hint))
        print("ETS Kanal: Protokoll OBIS-ASCII (IEC 62056-21), Baudrate %d, Datenformat Automatisch" % baud
              + (" (7E1)." if fmt_hint == "7E1" else " oder fest 8N1."))
        print("Danach in der Gerätekonsole 'sml info01' prüfen: muss mit 'ASCII %d %s' beginnen." % (baud, fmt_hint))
    else:
        print("Keine Einstellung hat verwertbare Daten geliefert.")
        print("Lesekopf-Position, Verkabelung (Kopf-TX an FTDI-RX, GND) und Versorgung prüfen.")


def scan(port: str, seconds: float):
    results = []
    print("Scanne %s, je %.0f s pro Einstellung ..." % (port, seconds))
    for baud in BAUDRATES:
        for fmt in FORMATS:
            data = capture(port, baud, fmt, seconds)
            r = analyze(data)
            results.append((baud, fmt, r, data))
            print("  %6d %s  %6d Bytes  %-11s Score %4d  OBIS-Zeilen %3d  SML %2d  druckbar %3.0f%%"
                  % (baud, fmt, r["bytes"], r["kind"], r["score"], r["obis_lines"], r["sml_frames"],
                     r["printable_masked"] * 100))
    best = max(results, key=lambda x: x[2]["score"])
    report_single(best[3], best[0], best[1])
    recommend(best[:3])


# ---------------------------------------------------------------- Selbsttest

SAMPLE = (b"/LOG5LK13BE803039\r\n"
          b"1-0:96.1.0*255(001LOG0067059477)\r\n"
          b"1-0:1.8.0*255(021915.9796*kWh)\r\n"
          b"1-0:16.7.0*255(000696*W)\r\n"
          b"1-0:0.2.0*255(ver.03,432F,20170504)\r\n"
          b"!\r\n")


def to_7e1_as_8n1(data: bytes) -> bytes:
    return bytes(b | 0x80 if bin(b).count("1") & 1 else b for b in data)


def selftest() -> int:
    fails = 0

    def check(cond, msg):
        nonlocal fails
        print(("  ok    " if cond else "  FEHLER ") + msg)
        if not cond:
            fails += 1

    r = analyze(SAMPLE * 2)
    check(r["kind"] == "OBIS-ASCII" and r["obis_lines"] == 8, "ASCII-Beispiel erkannt")
    r7 = analyze(to_7e1_as_8n1(SAMPLE * 2))
    check(r7["kind"] == "OBIS-ASCII" and r7["even_parity"] == 1.0 and r7["printable_raw"] < 0.9,
          "7E1 als 8N1 gelesen erkannt, Parität gerade")
    sml = SML_START + bytes(range(40)) + b"\x1b\x1b\x1b\x1b\x1a\x00\x12\x34"
    check(analyze(sml * 3)["kind"] == "SML", "SML-Startsequenz erkannt")
    check(analyze(b"")["kind"] == "keine Daten", "leerer Mitschnitt")
    noise = bytes((i * 37 + 11) & 0xFF for i in range(500))
    check(analyze(noise)["score"] < analyze(SAMPLE)["score"], "Rauschen bewertet schlechter als ASCII")
    tel = extract_telegram(b"xx\r\n" + SAMPLE[:40] + b"\r\n" + SAMPLE)
    check(tel is not None and tel[0] == "/LOG5LK13BE803039" and tel[-1] == "!", "Telegramm extrahiert")
    check(parse_obis("1-0:1.8.0*255(021915.9796*kWh)") == ("1-0:1.8.0", "021915.9796", "kWh"), "Zeile zerlegt")
    check(parse_obis("C.90.2(12345678)") == ("1-0:C.90.2", "12345678", ""), "Buchstabencode zerlegt")
    check(trim_meter_id("001LOG0067059477") == "1LOG0067059477", "Zähler-ID gekürzt")

    # Durchgang über einen virtuellen Port (pyserial loop://), falls pyserial installiert ist
    try:
        import serial
        with serial.serial_for_url("loop://", baudrate=9600, timeout=0.2) as s:
            s.write(SAMPLE)
            got = bytearray()
            end = time.time() + 1
            while time.time() < end and len(got) < len(SAMPLE):
                got += s.read(4096)
        check(bytes(got) == SAMPLE, "Mitschnitt über virtuellen Port")
    except ImportError:
        print("  (pyserial nicht installiert, virtueller Port übersprungen)")

    print("\n%s" % ("Selbsttest bestanden" if fails == 0 else "%d Fehler" % fails))
    print(describe_telegram(extract_telegram(SAMPLE)))
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser(description="Findet Protokoll, Baudrate und Datenformat eines Stromzählers.")
    ap.add_argument("port", nargs="?", help="z. B. COM5 oder /dev/ttyUSB0")
    ap.add_argument("--baud", type=int, choices=BAUDRATES, help="nur diese Baudrate mitschneiden")
    ap.add_argument("--format", choices=FORMATS, default="8N1", help="Datenformat für --baud (Standard 8N1)")
    ap.add_argument("--seconds", type=float, default=None, help="Mitschnittdauer (Scan: je 4 s, sonst 15 s)")
    ap.add_argument("--raw", help="Rohdaten zusätzlich in diese Datei schreiben")
    ap.add_argument("--list", action="store_true", help="serielle Ports auflisten")
    ap.add_argument("--selftest", action="store_true", help="Auswertung ohne Hardware testen")
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    if args.list:
        from serial.tools import list_ports
        for p in list_ports.comports():
            print("%-12s %s" % (p.device, p.description))
        return 0
    if not args.port:
        ap.error("Port fehlt (z. B. COM5). Ports anzeigen: --list")

    try:
        import serial  # noqa: F401
    except ImportError:
        print("pyserial fehlt: pip install pyserial")
        return 2

    if args.baud:
        data = capture(args.port, args.baud, args.format, args.seconds or 15)
        if args.raw:
            with open(args.raw, "wb") as f:
                f.write(data)
            print("Rohdaten: %s (%d Bytes)" % (args.raw, len(data)))
        report_single(data, args.baud, args.format)
        r = analyze(data)
        if r["kind"] in ("SML", "OBIS-ASCII"):
            recommend((args.baud, args.format, r))
    else:
        scan(args.port, args.seconds or 4)
    return 0


if __name__ == "__main__":
    sys.exit(main())
