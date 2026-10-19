// event.swift - the hook's sender: `sound-event [--hook] [--port N]
// EVENT SID` sends ONE OSC datagram to the sound engine and exits 0. It never
// waits, never reads a reply, and prints nothing, whatever happens: a hook
// must never be the reason a turn breaks.
//
// Built with the system toolchain, Darwin only (no Foundation: +1.1 ms a tool):
//   swiftc -O -swift-version 5 -o sound-event event.swift
//
// THE WIRE, the same bytes as sessiontools.sound.encode:
//   /sound/event ,ssd EVENT SID T     T = the wall clock, Unix seconds (float64)
// from an UNCONNECTED UDP socket to 127.0.0.1:PORT. PORT is --port N, else
// $SESSIONTOOLS_SOUND_PORT, else 44100 (sessiontools.sound.PORT; a test pins
// that the three constants agree).
//
// --hook: the hook payload is on stdin. It is read to EOF, and NOTHING is sent
// when the payload's TOP LEVEL has a truthy `agent_id` (a subagent's event), as
// python's busy.arm/end skip `payload.get("agent_id")`. The check is a
// depth-tracking scan, not a parser: it skips strings (honouring escapes),
// looks only at depth-1 keys, and takes the last `agent_id` if there are two.
// Truthy is python's: not null, false, "", 0, [] or {}. An empty payload is {}
// (python's main does the same) and sends. A payload that is not one balanced
// JSON object sends nothing: python's json.loads would refuse it too.

import Darwin

let defaultPort: UInt16 = 44100
let maxText = 256

// MARK: - the payload scan

/// Whether payload P (stdin's bytes) lets the event through: no truthy
/// top-level `agent_id`, and the shape of one JSON object.
func letsThrough(_ p: [UInt8]) -> Bool {
    let n = p.count
    func isSpace(_ c: UInt8) -> Bool { c == 0x20 || c == 0x09 || c == 0x0A || c == 0x0D }
    var i = 0
    while i < n, isSpace(p[i]) { i += 1 }
    if i == n { return true }                         // empty: python reads {}
    guard p[i] == UInt8(ascii: "{") else { return false }

    let key = Array("agent_id".utf8)
    var depth = 0
    var expectKey = false                             // at depth 1: the next string is a key
    var lastKeyIsAgent = false
    var agentTruthy = false

    /// Skip the string whose opening quote is at i. Returns the index past
    /// its closing quote, and its decoded bytes when DECODE (keys only; the
    /// decoding stops being exact past 16 bytes, which no match needs).
    func string(_ start: Int, decode: Bool) -> (Int, [UInt8])? {
        var j = start + 1
        var out: [UInt8] = []
        while j < n {
            let c = p[j]
            if c == UInt8(ascii: "\"") { return (j + 1, out) }
            if c == UInt8(ascii: "\\") {
                guard j + 1 < n else { return nil }
                let e = p[j + 1]
                if e == UInt8(ascii: "u") {
                    guard j + 5 < n else { return nil }
                    var v = 0
                    for h in p[(j + 2)...(j + 5)] {
                        switch h {
                        case 0x30...0x39: v = v * 16 + Int(h - 0x30)
                        case 0x41...0x46: v = v * 16 + Int(h - 0x41 + 10)
                        case 0x61...0x66: v = v * 16 + Int(h - 0x61 + 10)
                        default: return nil
                        }
                    }
                    if decode, out.count <= 16 { out.append(v < 0x80 ? UInt8(v) : 0xFF) }
                    j += 6
                    continue
                }
                if decode, out.count <= 16 {
                    switch e {
                    case UInt8(ascii: "n"): out.append(0x0A)
                    case UInt8(ascii: "t"): out.append(0x09)
                    case UInt8(ascii: "r"): out.append(0x0D)
                    case UInt8(ascii: "b"): out.append(0x08)
                    case UInt8(ascii: "f"): out.append(0x0C)
                    default: out.append(e)            // \" \\ \/
                    }
                }
                j += 2
                continue
            }
            if decode, out.count <= 16 { out.append(c) }
            j += 1
        }
        return nil                                    // unterminated
    }

    /// Python's truthiness of the depth-1 value that starts at i.
    func truthy(_ start: Int) -> Bool {
        var j = start
        let c = p[j]
        switch c {
        case UInt8(ascii: "\""):
            return j + 1 < n && p[j + 1] != UInt8(ascii: "\"")
        case UInt8(ascii: "["), UInt8(ascii: "{"):
            j += 1
            while j < n, isSpace(p[j]) { j += 1 }
            let close = c == UInt8(ascii: "[") ? UInt8(ascii: "]") : UInt8(ascii: "}")
            return !(j < n && p[j] == close)
        case UInt8(ascii: "t"): return true
        case UInt8(ascii: "f"), UInt8(ascii: "n"): return false
        case UInt8(ascii: "N"), UInt8(ascii: "I"): return true      // NaN, Infinity
        case UInt8(ascii: "-"), 0x30...0x39:
            // A number is falsy only when its mantissa's digits are all 0.
            if c == UInt8(ascii: "-") { j += 1 }
            if j < n, p[j] == UInt8(ascii: "I") { return true }      // -Infinity
            while j < n {
                let d = p[j]
                if d == UInt8(ascii: "e") || d == UInt8(ascii: "E") { break }
                if d >= 0x31 && d <= 0x39 { return true }
                if d != 0x30 && d != UInt8(ascii: ".") { break }
                j += 1
            }
            return false
        default:
            return true                               // not JSON: fail toward no send
        }
    }

    while i < n {
        let c = p[i]
        switch c {
        case UInt8(ascii: "\""):
            let atKey = depth == 1 && expectKey
            guard let scanned = string(i, decode: atKey) else { return false }
            let (next, text) = scanned
            if atKey {
                lastKeyIsAgent = text == key
                expectKey = false
            }
            i = next
            continue
        case UInt8(ascii: "{"), UInt8(ascii: "["):
            depth += 1
            if depth == 1 { expectKey = true }
        case UInt8(ascii: "}"), UInt8(ascii: "]"):
            depth -= 1
            if depth < 0 { return false }
            if depth == 0 {
                // The one object closed: only whitespace may follow.
                var j = i + 1
                while j < n, isSpace(p[j]) { j += 1 }
                return j == n && !agentTruthy
            }
        case UInt8(ascii: ","):
            if depth == 1 { expectKey = true }
        case UInt8(ascii: ":"):
            if depth == 1, lastKeyIsAgent {
                var j = i + 1
                while j < n, isSpace(p[j]) { j += 1 }
                guard j < n else { return false }
                agentTruthy = truthy(j)
                lastKeyIsAgent = false
            }
        default:
            break
        }
        i += 1
    }
    return false                                      // unbalanced: not one object
}

// MARK: - OSC

func oscString(_ s: String) -> [UInt8] {
    var b = Array(s.utf8)
    b.append(0)
    while b.count % 4 != 0 { b.append(0) }
    return b
}

func wallClock() -> Double {
    var ts = timespec()
    clock_gettime(CLOCK_REALTIME, &ts)
    return Double(ts.tv_sec) + Double(ts.tv_nsec) / 1e9
}

// MARK: - main: every path ends in exit(0), silent

func readAll(_ fd: Int32) -> [UInt8] {
    var all: [UInt8] = []
    var buf = [UInt8](repeating: 0, count: 65_536)
    while true {
        let got = buf.withUnsafeMutableBytes { read(fd, $0.baseAddress, $0.count) }
        if got > 0 {
            all.append(contentsOf: buf[0..<got])
        } else if got < 0 && errno == EINTR {
            continue
        } else {
            return all
        }
    }
}

func parsePort(_ s: String) -> UInt16? {
    guard !s.isEmpty, s.utf8.allSatisfy({ $0 >= 0x30 && $0 <= 0x39 }), let v = Int(s),
          v > 0, v <= 65535 else { return nil }
    return UInt16(v)
}

var hook = false
var port: UInt16?
var words: [String] = []
var args = CommandLine.arguments.dropFirst().makeIterator()
while let a = args.next() {
    switch a {
    case "--hook": hook = true
    case "--port":
        guard let v = args.next(), let p = parsePort(v) else { exit(0) }
        port = p
    default: words.append(a)
    }
}
guard words.count == 2 else { exit(0) }
let event = words[0], sid = words[1]
guard !event.isEmpty, event.utf8.count <= maxText, !sid.isEmpty, sid.utf8.count <= maxText,
      sid != ".", sid != "..", !sid.utf8.contains(UInt8(ascii: "/"))
else { exit(0) }
if port == nil, let raw = getenv("SESSIONTOOLS_SOUND_PORT") {
    port = parsePort(String(cString: raw))
}
if hook, !letsThrough(readAll(0)) { exit(0) }

var msg = oscString("/sound/event") + oscString(",ssd") + oscString(event) + oscString(sid)
withUnsafeBytes(of: wallClock().bitPattern.bigEndian) { msg += $0 }

let fd = socket(AF_INET, SOCK_DGRAM, 0)
if fd >= 0 {
    var sa = sockaddr_in()
    sa.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
    sa.sin_family = sa_family_t(AF_INET)
    sa.sin_port = (port ?? defaultPort).bigEndian
    sa.sin_addr.s_addr = inet_addr("127.0.0.1")
    _ = msg.withUnsafeBytes { p in
        withUnsafePointer(to: &sa) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                sendto(fd, p.baseAddress, p.count, 0, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
    }
    close(fd)
}
exit(0)
