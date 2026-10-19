// engine.swift - the sound engine: one resident process that plays
// the hooks' sounds. Hooks fire ONE OSC datagram at 127.0.0.1:44100 and never
// wait; the engine owns everything after that: the delay, the speech guard,
// the no-replay latch, the stop, the seat-gone check and the sound itself.
//
// THE CORE: UDP + OSC, the state machine, the records, the guards,
// the timers, ping/pong and logging, all behind the `Output` protocol.
// THE AUDIO: `AudioOutput`, an AVAudioEngine with ONE
// AVAudioSourceNode -> the main mixer -> the output. A fixed table of 8 voices;
// the render block never allocates, locks or retains (it reads raw memory and
// `Synchronization.Atomic`s, see "the voice table"). Sounds are decoded on the
// control queue at first use, held as 48 kHz stereo Int16, evicted after 10
// minutes unused. Paths in TCC-protected places are refused untouched. The
// engine follows the default output device and never touches `inputNode` (the
// microphone) or the device's rate or buffer size.
//
// Built with the system toolchain, nothing else:
//   swiftc -O -swift-version 5 -o sound-engine engine.swift
//
// Modes:
//   sound-engine [--port N] [--sessions DIR] [--volume V] [--trace FILE]
//       THE ENGINE: real UDP, real records, the default output device. This
//       is the launchd agent's mode. It opens the device: no test runs it.
//   sound-engine --null-output [--port N] [--sessions DIR] [--null-length S]
//       real UDP and real records, no Core Audio. --port 0 binds an
//       ephemeral port (tests); the `ready` log line names it.
//   sound-engine --script STEPS.json [--sessions DIR] [--null-length S]
//       virtual time, no sockets: the steps drive the state machine
//       deterministically (a loaded host cannot flake it).
//   sound-engine --script STEPS.json --offline OUT.wav [--volume V]
//       the same virtual time, through the REAL audio graph in AVAudioEngine's
//       manual rendering mode: the voices, the mix and the fades are rendered
//       to OUT.wav (48 kHz stereo float32), frame-exact. No device.
// SESSIONTOOLS_SOUND_NO_DEVICE (any value) fences the device off: the device
// mode exits 2. tests/conftest.py sets it for every test.
// Every mode takes --trace FILE (JSONL: one line per datagram, live; one per
// voice start, with its first frame's host time) and --evict-after S (tests).
//
// THE WIRE: OSC 1.0, big-endian, strings NUL-padded:
//   /sound/event ,ssd  EVENT SID T    T = sender's wall clock, Unix seconds
//   /sound/event ,ss   EVENT SID      arrival time stands in for T
//   /sound/ping  ,d T  (or no args)   -> /sound/pong ,ds T STATUS-JSON
// Anything else is dropped and counted (`malformed`). A known address with an
// unknown EVENT is ignored and counted (`unknown`). An `arm` whose T is more
// than 2 s old is dropped (`stale`); a stop is never dropped.
//
// THE BUSY SOUND'S EVENTS: arm (PreToolUse), end (PostToolBatch), stop (Stop),
// clear (UserPromptSubmit, SessionEnd). Per sid:
//   idle --arm--> counting --deadline, mouth silent--> playing
//   playing --played out | cut by the mouth--> latched   (no replay)
//   any --end|stop|clear--> idle (a playing voice fades out)
//   counting|playing|latched --the record's pid dies--> forgotten
// Repeated arms are no-ops in every phase but idle.
//
// THE RECORD: <sessions>/<sid>/sounds/busy/engine.<claude pid>, written by
// python at the session's first arm: {"v":1,"file":ABS,"delay":S,"pid":N,
// "mouth":DIR}. When a sid has more than one (python unlinks the others, so
// this is the rare case), the engine takes a record whose pid is ALIVE, the
// newest mtime first, then the lowest path; if none is alive the seat has
// gone and nothing plays. A record whose name's pid differs from its "pid"
// is refused.
//
// THE SPEECH GUARD (as busy.wait): the countdown restarts
// at the full delay while the mouth speaks; nothing starts under the mouth; a
// playing sound is cut when the mouth starts; ANY session's speech counts.
// Speaking = <mouth>/playing.json exists AND <mouth>/player.pid names a live
// process (missing/empty/not a pid: silent; unreadable: speaking).
//
// It fails toward silence everywhere: an unreadable record, a missing or
// undecodable file, a path in a TCC-protected place, an unknown event, a
// malformed datagram, a full voice table - nothing plays. `status` counts the
// refusals and names the last.
//
// TCC: every path the engine would
// touch - the sound file, the mouth's playing.json and player.pid, the
// sessions root, each seat's record dir and a record that is a link - is
// classified from its string first, and a refused one is logged and NEVER
// stat'ed or opened, so no privacy prompt reaches the screen. The places:
// Desktop, Documents, Downloads, iCloud Drive, ~/Library/CloudStorage,
// ~/Library/Containers, ~/Library/Group Containers, ~/Library/Mail,
// ~/Library/Messages, /Volumes/*, /net, /Network, and any mount the kernel's
// table calls non-local or removable. A refused mouth latches its seat at arm
// (silent); a mouth that turns protected later counts as speaking (silent),
// and is counted once while it stays refused. A refused sessions root reads
// no record at all; a refused record dir is never listed. `--classify PATH` prints the verdict
// and every path the walk touched; `--mounts FILE` adds test mounts.
// Each is then READ at the path the check resolved, never by its name again:
// a link moved between the check and the read would lead
// a second lookup somewhere unchecked. The last component is read without
// following a link, and with no link ANYWHERE on the way (O_NOFOLLOW_ANY,
// AT_SYMLINK_NOFOLLOW_ANY: the checked path has none), so a link put there
// since, on the file or on a directory above it, is refused, not followed.
// The sound file is opened once and decoded from that descriptor
// (/dev/fd/N), never by name. `relink` in --script moves a link
// at exactly that instant, for the tests.
//
// THE DEVICE: the engine holds the default output open and follows it. On
// AVAudioEngineConfigurationChange, or when Core Audio's default output device
// changes, it rebuilds the engine and restarts, with backoff; if it cannot
// restart within 10 s it exits 3 and launchd starts a fresh one. The voices
// live outside the engine, so a playing sound carries on across a rebuild.
//
// LOGGING: one JSON line per state change on stderr (launchd sends it to
// ~/Library/Logs/sessiontools/sound-engine.log), never one per datagram.

import AVFoundation
import CoreAudio
import Foundation
import Synchronization

// MARK: - constants

let defaultPort = 44100
let staleAfter = 2.0          // an arm older than this is dropped
let tickEvery = 0.1           // the guard's poll while something counts or plays
var evictAfter = 600.0        // a loaded file unused this long is dropped (--evict-after: tests)
let sweepEvery = 60.0         // latched seats are checked for a dead pid this often
let voiceCount = 8            // the fixed voice table
let fadeMs = 20               // a stopped voice fades over this
let virtualRate = 48_000.0    // the null output counts frames at this rate
let soundRate = 48_000.0      // decoded sounds are held at this rate; the mixer resamples
let maxSeconds = 120.0        // a longer file is refused (23 MB held at 48 kHz stereo Int16)
let restartWithin = 10.0      // the device cannot restart within this: exit 3, launchd respawns
let maxText = 256             // an event name or sid longer than this is malformed
let epsilon = 1e-9
let busyEvents: Set<String> = ["arm", "end", "stop", "clear"]

// MARK: - the log

final class Journal {
    /// Set in --script mode: lines carry the virtual time `t` instead of `ts`.
    var virtualNow: (() -> Double)?
    private let iso: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()

    func emit(_ ev: String, _ fields: [String: Any] = [:]) {
        var f = fields
        f["ev"] = ev
        if let now = virtualNow {
            f["t"] = (now() * 1e6).rounded() / 1e6
        } else {
            f["ts"] = iso.string(from: Date())
        }
        guard JSONSerialization.isValidJSONObject(f),
              var line = try? JSONSerialization.data(withJSONObject: f, options: [.sortedKeys, .withoutEscapingSlashes])
        else { return }
        line.append(0x0A)
        line.withUnsafeBytes { _ = write(2, $0.baseAddress, $0.count) }
    }
}

let journal = Journal()

func json(_ obj: [String: Any]) -> String {
    guard JSONSerialization.isValidJSONObject(obj),
          let d = try? JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys, .withoutEscapingSlashes])
    else { return "{}" }
    return String(decoding: d, as: UTF8.self)
}

// MARK: - the trace (--trace FILE: the measurement)

/// JSONL, written on the control queue only, flushed per line. Host times are
/// CLOCK_UPTIME_RAW nanoseconds, the clock python's
/// `time.clock_gettime_ns(time.CLOCK_UPTIME_RAW)` reads (mach time).
final class Trace {
    private let fp: UnsafeMutablePointer<FILE>

    init?(_ path: String) {
        guard let f = fopen(path, "a") else { return nil }
        fp = f
    }

    func line(_ ev: String, _ fields: [String: Any]) {
        var f = fields
        f["ev"] = ev
        fputs(json(f) + "\n", fp)
        fflush(fp)
    }
}

var trace: Trace?

func uptimeNs() -> UInt64 { clock_gettime_nsec_np(CLOCK_UPTIME_RAW) }

let timebase: mach_timebase_info_data_t = {
    var tb = mach_timebase_info_data_t()
    mach_timebase_info(&tb)
    return tb
}()

/// Mach host ticks (an AudioTimeStamp's mHostTime) to CLOCK_UPTIME_RAW ns.
func hostNs(_ ticks: UInt64) -> UInt64 {
    ticks / UInt64(timebase.denom) * UInt64(timebase.numer)
        + ticks % UInt64(timebase.denom) * UInt64(timebase.numer) / UInt64(timebase.denom)
}

// MARK: - TCC: the privacy-protected places are never touched
//
// A launchd engine that stat()s or opens a file in a protected place puts a
// privacy prompt on the operator's screen. So every path the engine would
// touch (the sound file, the mouth's playing.json and player.pid, the
// sessions root, each seat's record dir) is classified FIRST, from its string, a component at a time,
// and a refused path is skipped with a log line and never touched. The classes:
//
//   folder   ~/Desktop, ~/Documents, ~/Downloads
//   icloud   ~/Library/Mobile Documents (iCloud Drive)
//   cloud    ~/Library/CloudStorage (the File Provider clouds: Dropbox,
//            Google Drive, OneDrive ...)
//   appdata  ~/Library/Containers and ~/Library/Group Containers: other apps'
//            data, behind macOS 14's "App Data" prompt. The engine is not
//            sandboxed and has no container of its own, so all of it is
//            another app's
//   private  ~/Library/Mail and ~/Library/Messages: the stores behind Full
//            Disk Access
//   volume   /Volumes/*: where macOS mounts every volume but the boot one,
//            removable or not. Which is which cannot be told without asking
//            the volume, so all of them are refused.
//   network  /net and /Network (the automounter), and every mount the kernel's
//            table says is not local (smbfs, nfs, afpfs, webdav, autofs maps)
//   removable  a mount the kernel's table flags MNT_REMOVABLE, wherever it is
//
// The log's `reason` stays "tcc" for every class; `class` says which.
// The home folders are matched under $HOME, the account's home, their real
// paths, and the Data volume's firmlink; everything lower-cased, because the
// default APFS volume ignores case.

let dataFirmlink = "/System/Volumes/Data"

let tccDirs: [(dir: String, kind: String)] = {
    var homes = Set<String>()
    if let h = ProcessInfo.processInfo.environment["HOME"], h.hasPrefix("/") { homes.insert(h) }
    if let pw = getpwuid(getuid()), let d = pw.pointee.pw_dir { homes.insert(String(cString: d)) }
    for h in homes {                       // the home itself is never protected: realpath is safe
        if let r = realpath(h, nil) { homes.insert(String(cString: r)); free(r) }
    }
    var dirs: [(String, String)] = []
    for h in homes {
        let home = h.count > 1 && h.hasSuffix("/") ? String(h.dropLast()) : h
        for base in [home, dataFirmlink + home] {
            for (d, kind) in [("Desktop", "folder"), ("Documents", "folder"), ("Downloads", "folder"),
                              ("Library/Mobile Documents", "icloud"), ("Library/CloudStorage", "cloud"),
                              ("Library/Containers", "appdata"), ("Library/Group Containers", "appdata"),
                              ("Library/Mail", "private"), ("Library/Messages", "private")] {
                dirs.append(((base + "/" + d).lowercased(), kind))
            }
        }
    }
    return dirs
}()

/// Refused below (never the dir itself, which holds nothing of anyone's).
let volumeRoots: [(dir: String, kind: String)] = ["/Volumes", "/net", "/Network"].flatMap { d in
    [d, dataFirmlink + d].map { ($0.lowercased(), d == "/Volumes" ? "volume" : "network") }
}

/// Mount points to treat as network or removable on top of the kernel's
/// table (--mounts FILE: tests, which cannot mount a share).
var extraMounts: [(dir: String, kind: String)] = []

/// The mounts whose files can prompt: not local, or removable. MNT_NOWAIT
/// reads the kernel's table as it stands; no filesystem is asked anything.
func riskyMounts() -> [(dir: String, kind: String)] {
    var out = extraMounts
    let n = getfsstat(nil, 0, MNT_NOWAIT)
    guard n > 0 else { return out }
    let cap = Int(n) + 8                         // room for a mount that lands meanwhile
    let buf = UnsafeMutablePointer<statfs>.allocate(capacity: cap)
    defer { buf.deallocate() }
    let got = getfsstat(buf, Int32(cap * MemoryLayout<statfs>.stride), MNT_NOWAIT)
    for i in 0..<max(0, min(Int(got), cap)) {
        var fs = buf[i]
        let kind: String
        if fs.f_flags & UInt32(MNT_LOCAL) == 0 {
            kind = "network"
        } else if fs.f_flags & UInt32(MNT_REMOVABLE) != 0 {
            kind = "removable"
        } else {
            continue
        }
        let on = withUnsafeBytes(of: &fs.f_mntonname) { raw in
            String(decoding: raw.prefix { $0 != 0 }, as: UTF8.self)
        }
        if on.isEmpty || on == "/" { continue }  // the root is never refused wholesale
        out.append((on.lowercased(), kind))
    }
    return out
}

/// The class PATH falls in, from its string alone, or nil.
func refusedClass(_ path: String, mounts: [(dir: String, kind: String)]) -> String? {
    let p = path.lowercased()
    for (d, kind) in tccDirs where p == d || p.hasPrefix(d + "/") { return kind }
    for (d, kind) in volumeRoots where p.hasPrefix(d + "/") { return kind }
    for (d, kind) in mounts where p == d || p.hasPrefix(d + "/") { return kind }
    return nil
}

/// The class of protected place PATH, or where its symlinks lead, is in; nil
/// when the engine may touch it. It walks the path a component at a time and
/// classifies each prefix BEFORE it lstat()s it, so it never touches anything
/// inside a protected place (a stat there can be what puts the prompt up).
/// A relative path is taken from the working directory. TOUCHED, if given,
/// hears every path it lstat()s (--classify: the tests' proof).
func refusal(_ path: String, touched: ((String) -> Void)? = nil) -> String? {
    resolve(path, touched: touched).kind
}

/// refusal(), and the path the walk checked, with every link it met
/// resolved. Read THAT path, never PATH again: a link that moves after the
/// check would lead a second lookup of PATH somewhere unchecked, into a
/// protected place (a live test's FIFO hang, 1 run in 12).
func resolve(_ path: String, touched: ((String) -> Void)? = nil) -> (kind: String?, path: String) {
    var full = path
    if !full.hasPrefix("/") {
        var buf = [CChar](repeating: 0, count: Int(PATH_MAX) + 1)
        guard getcwd(&buf, buf.count) != nil else { return ("cwd", path) }
        full = String(cString: buf) + "/" + path
    }
    let mounts = riskyMounts()
    var pending = Array(full.split(separator: "/", omittingEmptySubsequences: true).reversed())
    var done: [Substring] = []
    var hops = 0
    while let c = pending.popLast() {
        if c == "." { continue }
        if c == ".." { _ = done.popLast(); continue }
        let candidate = "/" + (done + [c]).joined(separator: "/")
        if let kind = refusedClass(candidate, mounts: mounts) { return (kind, candidate) }
        touched?(candidate)
        var st = stat()
        guard lstatChecked(candidate, &st) == 0, (st.st_mode & S_IFMT) == S_IFLNK else {
            done.append(c)                      // a dir, a file, or missing: the load says
            continue
        }
        hops += 1
        if hops > 32 { return (nil, full) }     // a loop: opening it fails anyway (ELOOP)
        var buf = [CChar](repeating: 0, count: Int(PATH_MAX) + 1)
        let n = readlink(candidate, &buf, Int(PATH_MAX))
        if n <= 0 { done.append(c); continue }
        let target = String(decoding: buf[0..<n].map { UInt8(bitPattern: $0) }, as: UTF8.self)
        if target.hasPrefix("/") { done = [] }
        pending += target.split(separator: "/", omittingEmptySubsequences: true).reversed()
    }
    return (nil, "/" + done.joined(separator: "/"))
}

/// lstat() for a path the walk resolved: AT_SYMLINK_NOFOLLOW_ANY gives the
/// last component's own status, link or not, and refuses (ELOOP) a link met
/// anywhere BEFORE it. Every path read after its check has no link in it, so
/// a directory on the way that was swapped for a link since the check is
/// never crossed into somewhere unchecked.
func lstatChecked(_ path: String, _ st: inout stat) -> Int32 {
    fstatat(AT_FDCWD, path, &st, AT_SYMLINK_NOFOLLOW_ANY)
}

/// The flags of every open of a checked path: no link ANYWHERE in it, the
/// last component included (O_NOFOLLOW_ANY: ELOOP; it replaces O_NOFOLLOW,
/// and the kernel refuses the two together, EINVAL), and no wait on a FIFO
/// (O_NONBLOCK). The path was resolved with no link in it, so a link there
/// now was put there since the check, and is refused.
let checkedOpen: Int32 = O_RDONLY | O_NOFOLLOW_ANY | O_NONBLOCK

/// PATH, a path the walk resolved, opened for reading with checkedOpen: the
/// descriptor and its fstat(), or nil unless it is a regular file this
/// process can read. Everything after reads the descriptor, never PATH, so
/// the read is of the inode this open reached, whatever PATH names by then.
func openChecked(_ path: String) -> (fd: Int32, st: stat)? {
    let fd = open(path, checkedOpen)
    guard fd >= 0 else { return nil }
    var st = stat()
    guard fstat(fd, &st) == 0, (st.st_mode & S_IFMT) == S_IFREG else {
        close(fd)
        return nil
    }
    return (fd, st)
}

/// The names in DIR, a directory the walk resolved, listed through a
/// descriptor opened with no link anywhere in the path: a directory
/// on the way swapped for a link since the check is never listed. nil when
/// it cannot be opened as a directory.
func listChecked(_ dir: String) -> [String]? {
    let fd = open(dir, O_RDONLY | O_DIRECTORY | O_NOFOLLOW_ANY)
    guard fd >= 0 else { return nil }
    guard let d = fdopendir(fd) else {
        close(fd)
        return nil
    }
    defer { closedir(d) }                       // closes fd too
    var names: [String] = []
    while let e = readdir(d) {
        let name = withUnsafePointer(to: e.pointee.d_name) {
            $0.withMemoryRebound(to: CChar.self, capacity: Int(MAXPATHLEN)) { String(cString: $0) }
        }
        if name != "." && name != ".." { names.append(name) }
    }
    return names
}

/// The mouth's two files (busy.mouth_speaking reads them every tick): the
/// first one refused (named under MOUTH, with its class), or the checked
/// paths of both. The dir is resolved ONCE, so a mouth link that moves
/// mid-check cannot split the two files between two places.
func mouthCheck(_ mouth: String) -> (refused: (path: String, kind: String)?, files: [String]) {
    let (dirKind, dir) = resolve(mouth)
    if let k = dirKind { return ((mouth + "/playing.json", k), []) }
    var files: [String] = []
    for name in ["playing.json", "player.pid"] {
        let (kind, path) = resolve(dir + "/" + name)
        if let k = kind { return ((mouth + "/" + name, k), []) }
        files.append(path)
    }
    return (nil, files)
}

func mouthRefusal(_ mouth: String) -> (path: String, kind: String)? { mouthCheck(mouth).refused }

// MARK: - processes and the mouth

func pidAlive(_ pid: Int32) -> Bool {
    if pid <= 1 { return false }
    if kill(pid, 0) == 0 { return true }
    return errno == EPERM
}

/// PATH names a live process (busy._pid_live). Missing: false (the
/// killed-player gap). Empty or not a pid: false. Unreadable: true, which
/// fails toward silence; a link (it was checked as not one) is unreadable.
/// It is opened with checkedOpen: no link anywhere on the path, and
/// O_NONBLOCK, so a FIFO named player.pid cannot block the tick that every
/// seat shares. Not a regular file (a FIFO, a directory): false, never a
/// pid file, so it does not hold the seat.
func pidFileLive(_ path: String) -> Bool {
    let fd = open(path, checkedOpen)
    if fd < 0 { return errno != ENOENT }
    defer { close(fd) }
    var st = stat()
    guard fstat(fd, &st) == 0 else { return true }
    if (st.st_mode & S_IFMT) != S_IFREG { return false }
    var buf = [UInt8](repeating: 0, count: 64)
    let n = read(fd, &buf, buf.count)
    if n < 0 { return true }
    let raw = buf[0..<n]
    if raw.contains(where: { $0 >= 0x80 }) { return true }   // not ascii: python's UnicodeError
    let s = String(decoding: raw, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
    guard !s.isEmpty, s.utf8.allSatisfy({ $0 >= 0x30 && $0 <= 0x39 }), let pid = Int32(s) else {
        return false
    }
    return pidAlive(pid)
}

/// What a seat's mouth says at a tick.
enum Mouth: Equatable {
    case silent
    case speaking
    /// In a protected place now (it came to be refused after its seat armed:
    /// a mount, a moved symlink). Its files are never touched, and it holds
    /// the seat like speech, which fails toward silence. The machine logs and
    /// counts it.
    case refused(path: String, kind: String)
}

/// The mouth in dir MOUTH (busy.mouth_speaking): speaking when playing.json
/// exists and player.pid names a live process. No dir: silent.
/// It reads the paths the classification resolved, never the mouth's links
/// again, and neither read follows a link made since, on the file or on a
/// directory above it (AT_SYMLINK_NOFOLLOW_ANY, O_NOFOLLOW_ANY).
func mouthState(_ mouth: String?) -> Mouth {
    guard let m = mouth, !m.isEmpty else { return .silent }
    let (refused, real) = mouthCheck(m)
    if let (path, kind) = refused { return .refused(path: path, kind: kind) }
    var st = stat()
    if lstatChecked(real[0], &st) != 0 { return .silent }
    return pidFileLive(real[1]) ? .speaking : .silent
}

// MARK: - the world: clocks and probes, real or virtual

protocol World: AnyObject {
    /// Monotonic seconds: every deadline and timer.
    func now() -> Double
    /// Wall-clock Unix seconds: only for an arm's staleness.
    func wall() -> Double
    func alive(_ pid: Int32) -> Bool
    func mouthNow(_ mouth: String?) -> Mouth
    /// An instant between PATH's TCC check and its read. AT names which:
    /// "check", just after the check (the sound file in play, a record in
    /// find); "open", just before the sound file's open; "decode", after that
    /// open and before the decode reads it. The real world does
    /// nothing here; --script's `relink` step moves a link at exactly that
    /// moment, so a test can prove the read takes the checked path and never
    /// the name again, nor a directory swapped on the way.
    func gap(_ path: String, at: String)
}

final class RealWorld: World {
    func now() -> Double { Double(DispatchTime.now().uptimeNanoseconds) / 1e9 }
    func wall() -> Double { Date().timeIntervalSince1970 }
    func alive(_ pid: Int32) -> Bool { pidAlive(pid) }
    func mouthNow(_ mouth: String?) -> Mouth { mouthState(mouth) }
    func gap(_ path: String, at: String) {}
}

/// --script: time is a number the runner moves. The mouth and pids are the
/// real probes unless a step overrides them (`mouth`, `kill`, `revive`).
/// A `relink` step arms a race: at the next AT gap (default "check") of PATH,
/// or of a path under it, PATH becomes a link to TO.
final class ScriptWorld: World {
    var t = 0.0
    var mouth: Bool?
    var dead: Set<Int32> = []
    var relinks: [String: (to: String, at: String)] = [:]
    func now() -> Double { t }
    func wall() -> Double { t }
    func alive(_ pid: Int32) -> Bool { dead.contains(pid) ? false : pidAlive(pid) }
    func mouthNow(_ m: String?) -> Mouth {
        if let speaking = mouth { return speaking ? .speaking : .silent }
        return mouthState(m)
    }
    /// Replace the armed path (PATH, or a directory PATH is under) with a link
    /// to its target, the way a racing writer would: a fresh link renamed over
    /// it. A directory is first moved aside to `<dir>.was` (rename cannot put
    /// a link over a directory). Once per step.
    func gap(_ path: String, at: String) {
        guard let key = relinks.first(where: { k, v in
            v.at == at && (path == k || path.hasPrefix(k + "/"))
        })?.key, let (to, _) = relinks.removeValue(forKey: key) else { return }
        var st = stat()
        if lstat(key, &st) == 0, (st.st_mode & S_IFMT) == S_IFDIR, rename(key, key + ".was") != 0 {
            journal.emit("error", ["what": "relink", "path": key, "errno": Int(errno)])
            return
        }
        let tmp = key + ".relink"
        unlink(tmp)
        guard symlink(to, tmp) == 0, rename(tmp, key) == 0 else {
            journal.emit("error", ["what": "relink", "path": key, "errno": Int(errno)])
            return
        }
        journal.emit("relinked", ["path": key, "to": to, "at": at])
    }
}

// MARK: - records

struct Record {
    let path: String
    let file: String
    let delay: Double
    let pid: Int32
    let mouth: String?
    let mtime: Double
}

final class Records {
    let root: String
    /// The root's class when it is in a protected place: then no record is
    /// ever read (every arm is "sessions-refused"), and main logs it once.
    let rootRefused: String?
    private var cache: [String: Record] = [:]   // by path; valid while the mtime matches
    /// sid -> the class its record dir was refused with.
    /// Only refusals are cached: an allowed dir is classified at every find,
    /// because a cached "allowed" could go stale (a dir that turns into a link
    /// later) and list a protected place, the one thing this must never do.
    private var dirsRefused: [String: String] = [:]
    /// The last record path refused by find, and its class: the machine's log line.
    private(set) var lastRefusal: (path: String, kind: String)?

    init(root: String) {
        self.root = root
        rootRefused = refusal(root)
    }

    func dir(_ sid: String) -> String { root + "/" + sid + "/sounds/busy" }

    /// SID's record dir's class when it, or a link on the way to it (the
    /// session dir, sounds, busy), is in a protected place; nil when it may
    /// be listed. The root is classified once, at start; this is the rest.
    /// The path is the one the walk checked: list that, never dir(sid) again.
    func dirRefusal(_ sid: String) -> (kind: String?, path: String) {
        if let kind = dirsRefused[sid] { return (kind, dir(sid)) }
        let (kind, real) = resolve(dir(sid))
        if let k = kind {
            if dirsRefused.count > 1024 { dirsRefused.removeAll() }
            dirsRefused[sid] = k
        }
        return (kind, real)
    }

    /// The record that arms SID, or nil and why: "no-record" (none valid),
    /// "seat-gone" (valid ones, but no pid alive), "sessions-refused" (the
    /// root is protected) or "records-refused" (SID's record dir, or every
    /// record in it, leads into a protected place: `lastRefusal` says where).
    func find(_ sid: String, _ world: World) -> (Record?, String) {
        if rootRefused != nil { return (nil, "sessions-refused") }
        let d = dir(sid)
        let (dirKind, real) = dirRefusal(sid)
        if let kind = dirKind {                            // never listed
            lastRefusal = (d, kind)
            return (nil, "records-refused")
        }
        guard let names = listChecked(real) else {
            return (nil, "no-record")
        }
        var valid: [Record] = []
        var refusedRecord: (path: String, kind: String)?
        for name in names.sorted() {
            guard name.hasPrefix("engine.") else { continue }
            let suffix = name.dropFirst("engine.".count)
            guard !suffix.isEmpty, suffix.utf8.allSatisfy({ $0 >= 0x30 && $0 <= 0x39 }),
                  let namePid = Int32(suffix) else { continue }
            let path = d + "/" + name
            // The dir is allowed, so only a record that is itself a link can
            // lead elsewhere: lstat() reads the link, never its target.
            var file = real + "/" + name
            var st = stat()
            if lstatChecked(file, &st) == 0, (st.st_mode & S_IFMT) == S_IFLNK {
                let (kind, target) = resolve(file)
                if let k = kind {
                    refusedRecord = (path, k)
                    continue
                }
                file = target
            }
            world.gap(file, at: "check")
            if let r = load(file, as: path), r.pid == namePid { valid.append(r) }
        }
        if cache.count > 1024 { cache.removeAll() }
        if valid.isEmpty, let r = refusedRecord {
            lastRefusal = r
            return (nil, "records-refused")
        }
        if valid.isEmpty { return (nil, "no-record") }
        let live = valid.filter { world.alive($0.pid) }
        guard !live.isEmpty else { return (nil, "seat-gone") }
        let best = live.sorted { a, b in
            a.mtime != b.mtime ? a.mtime > b.mtime : a.path < b.path
        }
        return (best[0], "ok")
    }

    /// The record read from FILE (a checked path), known by its name PATH.
    /// It is opened with checkedOpen (O_NOFOLLOW_ANY), so a link put on it,
    /// or on a directory above it, since the check is never followed (ELOOP:
    /// no record); O_NONBLOCK, so a FIFO cannot hang
    /// the open; and fstat() and read() then use that descriptor alone.
    private func load(_ file: String, as path: String) -> Record? {
        let fd = open(file, checkedOpen)
        guard fd >= 0 else { return nil }
        defer { close(fd) }
        var st = stat()
        guard fstat(fd, &st) == 0, (st.st_mode & S_IFMT) == S_IFREG, st.st_size <= 65_536 else {
            return nil
        }
        let mtime = Double(st.st_mtimespec.tv_sec) + Double(st.st_mtimespec.tv_nsec) / 1e9
        if let c = cache[path], c.mtime == mtime { return c }
        guard let data = readAll(fd, max: 65_536),
              let obj = try? JSONSerialization.jsonObject(with: data),
              let d = obj as? [String: Any],
              let v = number(d["v"]), v == 1,
              let file = d["file"] as? String, file.hasPrefix("/"),
              let delay = number(d["delay"]), delay.isFinite, delay >= 0,
              let pid = number(d["pid"]), pid == pid.rounded(), pid > 1, pid <= Double(Int32.max)
        else { return nil }
        var mouth: String?
        if let m = d["mouth"] {
            guard let s = m as? String else { return nil }
            mouth = s
        }
        let r = Record(path: path, file: file, delay: delay, pid: Int32(pid), mouth: mouth, mtime: mtime)
        cache[path] = r
        return r
    }

    /// Up to MAX bytes from FD, or nil when the read fails or there are more.
    private func readAll(_ fd: Int32, max: Int) -> Data? {
        let cap = max + 1
        var buf = [UInt8](repeating: 0, count: cap)
        var n = 0
        while n < cap {
            let got = buf.withUnsafeMutableBytes { read(fd, $0.baseAddress! + n, cap - n) }
            if got < 0 {
                if errno == EINTR { continue }
                return nil
            }
            if got == 0 { break }
            n += got
        }
        return n > max ? nil : Data(buf[0..<n])
    }

    /// A JSON number, never a JSON boolean (NSNumber carries both).
    private func number(_ x: Any?) -> Double? {
        guard let n = x as? NSNumber, CFGetTypeID(n) != CFBooleanGetTypeID() else { return nil }
        return n.doubleValue
    }
}

// MARK: - the output

protocol Output: AnyObject {
    var name: String { get }
    /// World.gap, for the moments inside the output's own read of a sound
    /// ("open", "decode"); --script wires it, live it does nothing.
    var gap: (String, String) -> Void { get set }
    /// Make FILE (the sound's name, its key everywhere) ready to play,
    /// decoded from PATH, the path its TCC check resolved: never FILE again,
    /// whose links may have moved since. nil, or why it is refused.
    func load(_ file: String, from path: String) -> String?
    func evict(_ file: String)
    /// Start FILE on a free voice for seat TAG: its index, or nil when the
    /// table is full.
    func start(_ file: String, tag: String, at now: Double) -> Int?
    /// Fade voice V out and free it; what to log about it.
    func stop(_ v: Int, at now: Double) -> [String: Any]
    /// Voice V played out (and is freed): what to log, else nil.
    func finished(_ v: Int, at now: Double) -> [String: Any]?
    /// When voice V will play out, when the output knows (the null output does).
    func ends(_ v: Int) -> Double?
    func playing(_ file: String) -> Bool
    var inUse: Int { get }
    func status() -> [String: Any]
    /// Called at the top of every advance(): housekeeping off the render
    /// thread (the trace's voice lines, releasing what the render let go).
    func service(at now: Double)
}

/// No Core Audio at all: a voice "plays" for LENGTH seconds of virtual frames.
final class NullOutput: Output {
    struct Voice { let file: String; let start: Double }
    let name = "null"
    var gap: (String, String) -> Void = { _, _ in }
    let length: Double
    private var voices: [Voice?] = Array(repeating: nil, count: voiceCount)
    private var loaded: [String: String] = [:]     // name -> the checked path it was loaded from

    init(length: Double) { self.length = length }

    func load(_ file: String, from path: String) -> String? {
        if loaded[file] == path { return nil }
        gap(path, "open")
        guard let (fd, _) = openChecked(path) else { return "unreadable" }   // as decode() opens it
        close(fd)
        loaded[file] = path
        return nil
    }

    func evict(_ file: String) { loaded[file] = nil }

    func start(_ file: String, tag: String, at now: Double) -> Int? {
        guard let i = voices.firstIndex(where: { $0 == nil }) else { return nil }
        voices[i] = Voice(file: file, start: now)
        return i
    }

    private func frames(_ v: Voice, _ now: Double) -> Int {
        Int(((min(now, v.start + length) - v.start) * virtualRate).rounded())
    }

    func stop(_ v: Int, at now: Double) -> [String: Any] {
        guard let voice = voices[v] else { return [:] }
        voices[v] = nil
        return ["voice": v, "frames": frames(voice, now), "fade_ms": fadeMs]
    }

    func finished(_ v: Int, at now: Double) -> [String: Any]? {
        guard let voice = voices[v], now + epsilon >= voice.start + length else { return nil }
        voices[v] = nil
        return ["voice": v, "frames": frames(voice, now)]
    }

    func ends(_ v: Int) -> Double? { voices[v].map { $0.start + length } }

    func playing(_ file: String) -> Bool { voices.contains { $0?.file == file } }

    var inUse: Int { voices.filter { $0 != nil }.count }

    func status() -> [String: Any] { ["output": name, "null_length": length] }

    func service(at now: Double) {}
}

// MARK: - sounds: decoded at first use, on the control queue

/// A decoded sound: 48 kHz stereo Int16, interleaved, in raw memory the
/// render thread reads without retaining anything. It is freed when its last
/// holder lets go: the cache, and every voice slot the render may still read.
final class Sound {
    /// The name it is known by (the record's `file`: the cache key, the log).
    let name: String
    /// Where it was decoded from: the path the TCC check resolved.
    let path: String
    let data: UnsafeMutablePointer<Int16>
    let frames: Int
    let mtime: Double
    let size: Int

    init(name: String, path: String, data: UnsafeMutablePointer<Int16>, frames: Int, mtime: Double,
         size: Int) {
        self.name = name
        self.path = path
        self.data = data
        self.frames = frames
        self.mtime = mtime
        self.size = size
    }

    deinit { data.deallocate() }
}

func mtimeOf(_ st: stat) -> Double {
    Double(st.st_mtimespec.tv_sec) + Double(st.st_mtimespec.tv_nsec) / 1e9
}

/// Decode PATH, known as NAME: the sound, or nil and why ("unreadable",
/// "undecodable", "too-long"). PATH is the path the TCC check resolved, with
/// no link in it. It is opened ONCE, by openChecked(): a link put anywhere in
/// it since the check (the file, or a directory on the way) is refused as
/// unreadable, never followed. The decode then reads that descriptor through
/// /dev/fd/N, which dup()s it (fdesc), so it reads the inode the open
/// reached and never looks PATH up again. GAP is
/// World.gap: --script's `relink` lands before the open, or after it.
func decode(_ path: String, as name: String,
            gap: (String, String) -> Void = { _, _ in }) -> (Sound?, String) {
    gap(path, "open")
    guard let (fd, st) = openChecked(path) else { return (nil, "unreadable") }
    defer { close(fd) }
    gap(path, "decode")
    guard let file = try? AVAudioFile(forReading: URL(fileURLWithPath: "/dev/fd/\(fd)")) else {
        return (nil, "undecodable")
    }
    let inFmt = file.processingFormat                      // float32, deinterleaved
    let length = file.length
    guard length > 0, inFmt.sampleRate > 0, inFmt.channelCount > 0 else { return (nil, "undecodable") }
    guard Double(length) / inFmt.sampleRate <= maxSeconds else { return (nil, "too-long") }
    guard let raw = AVAudioPCMBuffer(pcmFormat: inFmt, frameCapacity: AVAudioFrameCount(length)),
          (try? file.read(into: raw)) != nil, raw.frameLength > 0
    else { return (nil, "undecodable") }
    var pcm = raw
    if inFmt.sampleRate != soundRate {
        let cap = AVAudioFrameCount((Double(raw.frameLength) * soundRate / inFmt.sampleRate).rounded(.up)) + 4096
        guard let outFmt = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: soundRate,
                                         channels: inFmt.channelCount, interleaved: false),
              let conv = AVAudioConverter(from: inFmt, to: outFmt),
              let out = AVAudioPCMBuffer(pcmFormat: outFmt, frameCapacity: cap)
        else { return (nil, "undecodable") }
        var fed = false
        var err: NSError?
        let status = conv.convert(to: out, error: &err) { _, outStatus in
            if fed {
                outStatus.pointee = .endOfStream
                return nil
            }
            fed = true
            outStatus.pointee = .haveData
            return raw
        }
        guard status != .error, err == nil, out.frameLength > 0 else { return (nil, "undecodable") }
        pcm = out
    }
    guard let ch = pcm.floatChannelData else { return (nil, "undecodable") }
    let frames = Int(pcm.frameLength)
    let left = ch[0]
    let right = pcm.format.channelCount > 1 ? ch[1] : ch[0]   // mono: both sides; more: the first two
    func int16(_ x: Float) -> Int16 {
        if x.isNaN { return 0 }
        return Int16(max(-32768, min(32767, (x * 32768).rounded())))   // an Int16 file round-trips exactly
    }
    let data = UnsafeMutablePointer<Int16>.allocate(capacity: frames * 2)
    for i in 0..<frames {
        data[2 * i] = int16(left[i])
        data[2 * i + 1] = int16(right[i])
    }
    return (Sound(name: name, path: path, data: data, frames: frames, mtime: mtimeOf(st),
                  size: Int(st.st_size)), "ok")
}

// MARK: - the voice table: raw memory shared with the render thread

/// What a voice plays. Control writes it only while the slot is free (state 0
/// or 4), then publishes it with a releasing store of 1.
struct VoiceParams {
    var data: UnsafePointer<Int16>?
    var frames: Int
}

/// The render thread's own per-voice state; nothing else reads or writes it.
struct RenderVoice {
    var pos: Int
    var fadeLeft: Int        // -1: not fading
}

/// Per voice, `state` is the handshake:
///   0 free      control owns the params; the slot is reusable
///   1 start     control wrote the params, then stored 1; the render picks it up
///   2 playing   the render owns the voice
///   3 stopping  control asked (CAS 2->3); the render fades it over fadeMs
///   4 done      the render let go: played out, faded, or stopped before it began
/// Control moves 0|4 -> 1 (start), 1 -> 4 (CAS: stopped before the render saw
/// it) and 2 -> 3 (CAS). The render moves 1 -> 2 (CAS) and 2|3 -> 4. Control
/// reuses a slot only at 0 or 4, so the render never reads params mid-write.
/// The table is allocated once and lives as long as the process.
struct VoiceTable {
    let n: Int
    let state: UnsafeMutablePointer<Atomic<Int>>
    let played: UnsafeMutablePointer<Atomic<Int>>          // frames played, published by the render
    let startSample: UnsafeMutablePointer<Atomic<Int>>     // render sample time of the first frame; -1 before
    let startHost: UnsafeMutablePointer<Atomic<UInt64>>    // its host time, mach ticks; 0 when not valid
    let params: UnsafeMutablePointer<VoiceParams>
    let voices: UnsafeMutablePointer<RenderVoice>
    let fadeFrames: Int
    let gain: Float

    init(n: Int, fadeFrames: Int, gain: Float) {
        self.n = n
        self.fadeFrames = max(1, fadeFrames)
        self.gain = gain
        state = .allocate(capacity: n)
        played = .allocate(capacity: n)
        startSample = .allocate(capacity: n)
        startHost = .allocate(capacity: n)
        params = .allocate(capacity: n)
        voices = .allocate(capacity: n)
        for v in 0..<n {
            (state + v).initialize(to: Atomic(0))
            (played + v).initialize(to: Atomic(0))
            (startSample + v).initialize(to: Atomic(-1))
            (startHost + v).initialize(to: Atomic(0))
            (params + v).initialize(to: VoiceParams(data: nil, frames: 0))
            (voices + v).initialize(to: RenderVoice(pos: 0, fadeLeft: -1))
        }
    }

    // RT-BEGIN: the render block's body. Real-time rules: no allocation, no
    // lock, no retain or release, no syscall, no log line. It reads raw memory
    // and atomics, and writes the output buffers. tests/test_sound_engine.py
    // scans this region for anything that would break those rules.
    func render(_ count: Int, _ ts: UnsafePointer<AudioTimeStamp>,
                _ abl: UnsafeMutablePointer<AudioBufferList>) {
        let bufs = UnsafeMutableAudioBufferListPointer(abl)
        if bufs.count == 0 { return }
        for b in 0..<bufs.count {
            if let d = bufs[b].mData { memset(d, 0, Int(bufs[b].mDataByteSize)) }
        }
        guard let l = bufs[0].mData else { return }
        let left = l.assumingMemoryBound(to: Float.self)
        var right = left
        if bufs.count > 1, let r = bufs[1].mData { right = r.assumingMemoryBound(to: Float.self) }
        let frames = min(count, Int(bufs[0].mDataByteSize) / MemoryLayout<Float>.size)
        let stamp = ts.pointee
        let sample = stamp.mFlags.contains(.sampleTimeValid) ? Int(stamp.mSampleTime) : 0
        let host = stamp.mFlags.contains(.hostTimeValid) ? stamp.mHostTime : 0
        let scale = gain / 32768
        for v in 0..<n {
            var s = state[v].load(ordering: .acquiring)
            if s == 1 {
                let (won, now) = state[v].compareExchange(expected: 1, desired: 2,
                                                          ordering: .acquiringAndReleasing)
                if won {
                    voices[v] = RenderVoice(pos: 0, fadeLeft: -1)
                    startHost[v].store(host, ordering: .relaxed)
                    startSample[v].store(sample, ordering: .releasing)
                    s = 2
                } else {
                    s = now
                }
            }
            guard s == 2 || s == 3, let data = params[v].data else { continue }
            let total = params[v].frames
            var r = voices[v]
            if s == 3 && r.fadeLeft < 0 { r.fadeLeft = fadeFrames }
            var i = 0
            while i < frames && r.pos < total && r.fadeLeft != 0 {
                var g = scale
                if r.fadeLeft > 0 {
                    g *= Float(r.fadeLeft) / Float(fadeFrames)
                    r.fadeLeft -= 1
                }
                left[i] += Float(data[2 * r.pos]) * g
                right[i] += Float(data[2 * r.pos + 1]) * g
                r.pos += 1
                i += 1
            }
            voices[v] = r
            played[v].store(r.pos, ordering: .relaxed)
            if r.pos >= total || r.fadeLeft == 0 { state[v].store(4, ordering: .releasing) }
        }
    }
    // RT-END
}

// MARK: - the audio output: AVAudioEngine, live or offline

/// The default output device's name, from Core Audio ("?" if it will not say).
func defaultOutputName() -> String {
    var id = AudioObjectID(0)
    var size = UInt32(MemoryLayout<AudioObjectID>.size)
    var addr = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDefaultOutputDevice,
                                          mScope: kAudioObjectPropertyScopeGlobal,
                                          mElement: kAudioObjectPropertyElementMain)
    guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil,
                                     &size, &id) == noErr else { return "?" }
    var name: Unmanaged<CFString>?
    size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    addr.mSelector = kAudioObjectPropertyName
    let ok = withUnsafeMutablePointer(to: &name) {
        AudioObjectGetPropertyData(id, &addr, 0, nil, &size, $0) == noErr
    }
    guard ok, let n = name else { return "?" }
    return n.takeRetainedValue() as String
}

func connectSource(_ e: AVAudioEngine, _ node: AVAudioNode, _ fmt: AVAudioFormat) throws {
    if #available(macOS 27, *) {
        try e.connectNode(node, to: e.mainMixerNode, format: fmt)
    } else {
        e.connect(node, to: e.mainMixerNode, format: fmt)
    }
}

struct EngineError: Error, CustomStringConvertible {
    let description: String
}

/// ONE AVAudioSourceNode (the shape a spike measured at ~11 ms) -> the main
/// mixer -> the output. Live, the output is the default device; offline, it is
/// AVAudioEngine's manual rendering mode, written to a file. Everything here
/// but the render block runs on the control queue (live) or the script's
/// thread (offline).
final class AudioOutput: Output {
    enum Mode { case live, offline }
    var gap: (String, String) -> Void = { _, _ in }

    let mode: Mode
    let name: String
    private let format = AVAudioFormat(standardFormatWithSampleRate: soundRate, channels: 2)!
    private let table: VoiceTable
    private let queue: DispatchQueue
    private let clock: () -> Double
    private var owned = [Bool](repeating: false, count: voiceCount)     // Machine holds the voice
    private var holds = [Sound?](repeating: nil, count: voiceCount)     // what the slot's params point at
    private var tags = [String](repeating: "", count: voiceCount)
    private var starts = [Double](repeating: 0, count: voiceCount)
    private var traced = [Bool](repeating: true, count: voiceCount)
    private var sounds: [String: Sound] = [:]
    private var engine: AVAudioEngine?
    private var observer: NSObjectProtocol?
    private var generation = 0
    private var restarts = 0
    private var device = ""
    private var rate = 0.0
    private var latency = 0.0
    private var outFile: AVAudioFile?
    private var renderBuf: AVAudioPCMBuffer?
    private var rendered = 0                                            // offline: frames written

    private var renderBlock: AVAudioSourceNodeRenderBlock {
        let t = table                                                   // a struct of pointers: no ARC
        return { _, ts, count, abl in
            t.render(Int(count), ts, abl)
            return noErr
        }
    }

    private init(mode: Mode, volume: Float, queue: DispatchQueue, clock: @escaping () -> Double) {
        self.mode = mode
        name = mode == .live ? "avaudioengine" : "offline"
        table = VoiceTable(n: voiceCount, fadeFrames: Int(soundRate) * fadeMs / 1000, gain: volume)
        self.queue = queue
        self.clock = clock
    }

    /// Live: the default output device. Nothing opens until begin().
    convenience init(live volume: Float, queue: DispatchQueue, clock: @escaping () -> Double) {
        self.init(mode: .live, volume: volume, queue: queue, clock: clock)
    }

    /// Offline: manual rendering into PATH, 48 kHz stereo float32. Manual mode
    /// is enabled before any node is touched, so no device is ever involved.
    convenience init(offline path: String, volume: Float, clock: @escaping () -> Double) throws {
        self.init(mode: .offline, volume: volume, queue: DispatchQueue(label: "sound-engine.offline"),
                  clock: clock)
        let e = AVAudioEngine()
        try e.enableManualRenderingMode(.offline, format: format, maximumFrameCount: 4096)
        let node = AVAudioSourceNode(format: format, renderBlock: renderBlock)
        e.attach(node)
        try connectSource(e, node, format)
        try e.start()
        let settings: [String: Any] = [
            AVFormatIDKey: kAudioFormatLinearPCM, AVSampleRateKey: soundRate, AVNumberOfChannelsKey: 2,
            AVLinearPCMBitDepthKey: 32, AVLinearPCMIsFloatKey: true, AVLinearPCMIsNonInterleaved: false,
        ]
        outFile = try AVAudioFile(forWriting: URL(fileURLWithPath: path), settings: settings,
                                  commonFormat: .pcmFormatFloat32, interleaved: false)
        guard let buf = AVAudioPCMBuffer(pcmFormat: e.manualRenderingFormat, frameCapacity: 4096) else {
            throw EngineError(description: "no render buffer")
        }
        renderBuf = buf
        engine = e
        device = "offline"
        rate = soundRate
    }

    // MARK: offline

    /// Render up to virtual time T, frame-exact (frame round(T * 48 kHz)), so a
    /// voice started at T begins at exactly that frame.
    func render(to t: Double) throws {
        guard let e = engine, let f = outFile, let buf = renderBuf else { return }
        let target = Int((t * soundRate).rounded())
        while rendered < target {
            let n = AVAudioFrameCount(min(4096, target - rendered))
            let status = try e.renderOffline(n, to: buf)
            guard status == .success, buf.frameLength > 0 else {
                throw EngineError(description: "renderOffline status \(status.rawValue)")
            }
            try f.write(from: buf)
            rendered += Int(buf.frameLength)
        }
    }

    func close() {
        if #available(macOS 15, *) { outFile?.close() }
        outFile = nil
        engine?.stop()
        engine = nil
    }

    // MARK: live: the device, followed

    /// Start the engine on the default output, and follow that device.
    func begin() {
        var addr = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDefaultOutputDevice,
                                              mScope: kAudioObjectPropertyScopeGlobal,
                                              mElement: kAudioObjectPropertyElementMain)
        let st = AudioObjectAddPropertyListenerBlock(AudioObjectID(kAudioObjectSystemObject), &addr, queue) {
            [weak self] _, _ in self?.restart("default-device")
        }
        if st != noErr { journal.emit("error", ["what": "device-listener", "status": Int(st)]) }
        queue.async { self.restart("start") }
    }

    /// On the control queue: tear the engine down and build a fresh one on
    /// the current default output, retrying with backoff; exit 3 after 10 s.
    private func restart(_ reason: String) {
        generation += 1
        teardown()
        attempt(generation, reason, 0, since: clock())
    }

    private func teardown() {
        if let o = observer { NotificationCenter.default.removeObserver(o) }
        observer = nil
        engine?.stop()
        engine = nil
    }

    private func attempt(_ gen: Int, _ reason: String, _ n: Int, since: Double) {
        guard gen == generation else { return }                       // a newer restart owns it
        do {
            try build()
            if reason != "start" { restarts += 1 }
            journal.emit("device", ["reason": reason, "name": device, "rate": rate,
                                    "latency": latency, "attempts": n + 1])
        } catch {
            teardown()
            if clock() - since >= restartWithin {
                journal.emit("error", ["what": "device", "reason": reason, "error": "\(error)",
                                       "attempts": n + 1])
                exit(3)                                               // launchd starts a fresh one
            }
            let wait = min(0.1 * pow(2.0, Double(n)), 2.0)
            queue.asyncAfter(deadline: .now() + wait) { self.attempt(gen, reason, n + 1, since: since) }
        }
    }

    private func build() throws {
        let e = AVAudioEngine()
        let engineRef = e
        let node = AVAudioSourceNode(format: format, renderBlock: renderBlock)
        e.attach(node)
        try connectSource(e, node, format)
        e.prepare()
        try e.start()
        engine = e
        observer = NotificationCenter.default.addObserver(
            forName: .AVAudioEngineConfigurationChange, object: e, queue: nil
        ) { [weak self, weak engineRef] _ in
            guard let self else { return }
            self.queue.async {
                // Only a change that stopped the engine (the documented case) or
                // moved the output format is acted on: a notification raised by
                // our own start never loops restarts.
                guard let e = self.engine, let built = engineRef, e === built else { return }
                let f = e.outputNode.outputFormat(forBus: 0)
                if e.isRunning && f.sampleRate == self.rate { return }
                self.restart("configuration")
            }
        }
        rate = e.outputNode.outputFormat(forBus: 0).sampleRate
        latency = e.outputNode.presentationLatency
        device = defaultOutputName()
    }

    // MARK: Output

    func load(_ file: String, from path: String) -> String? {
        if let s = sounds[file] {
            var st = stat()
            if s.path == path, lstatChecked(path, &st) == 0, (st.st_mode & S_IFMT) == S_IFREG,
               mtimeOf(st) == s.mtime, Int(st.st_size) == s.size { return nil }
            sounds[file] = nil                     // changed on disk, or the name leads elsewhere now
        }
        let (snd, why) = decode(path, as: file, gap: gap)
        guard let s = snd else { return why }
        sounds[file] = s
        return nil
    }

    /// Drop the decode. A slot the render may still read keeps it alive.
    func evict(_ file: String) { sounds[file] = nil }

    func start(_ file: String, tag: String, at now: Double) -> Int? {
        guard let snd = sounds[file] else { return nil }
        for v in 0..<table.n where !owned[v] {
            let s = table.state[v].load(ordering: .acquiring)
            guard s == 0 || s == 4 else { continue }                  // the render still fades it
            table.params[v] = VoiceParams(data: UnsafePointer(snd.data), frames: snd.frames)
            table.played[v].store(0, ordering: .relaxed)
            table.startSample[v].store(-1, ordering: .relaxed)
            table.startHost[v].store(0, ordering: .relaxed)
            holds[v] = snd
            tags[v] = tag
            starts[v] = now
            owned[v] = true
            traced[v] = false
            table.state[v].store(1, ordering: .releasing)
            return v
        }
        return nil
    }

    func stop(_ v: Int, at now: Double) -> [String: Any] {
        guard owned[v] else { return [:] }
        owned[v] = false
        while true {
            let s = table.state[v].load(ordering: .acquiring)
            if s == 1 {
                if table.state[v].compareExchange(expected: 1, desired: 4, ordering: .acquiringAndReleasing).exchanged { break }
            } else if s == 2 {
                if table.state[v].compareExchange(expected: 2, desired: 3, ordering: .acquiringAndReleasing).exchanged { break }
            } else {
                break                                                 // 4: it played out already
            }
        }
        return ["voice": v, "frames": table.played[v].load(ordering: .relaxed), "fade_ms": fadeMs]
    }

    func finished(_ v: Int, at now: Double) -> [String: Any]? {
        guard owned[v], table.state[v].load(ordering: .acquiring) == 4 else { return nil }
        owned[v] = false
        return ["voice": v, "frames": table.played[v].load(ordering: .relaxed)]
    }

    /// When voice V should play out: frame-exact offline; live, an estimate
    /// (the tick polls after it). nil once it has passed, so it never spins.
    func ends(_ v: Int) -> Double? {
        guard owned[v], let s = holds[v] else { return nil }
        let e = mode == .offline
            ? ((starts[v] * soundRate).rounded() + Double(s.frames)) / soundRate
            : starts[v] + Double(s.frames) / soundRate + latency
        return e > clock() + epsilon ? e : nil
    }

    private func busy(_ v: Int) -> Bool {
        let s = table.state[v].load(ordering: .acquiring)
        return s >= 1 && s <= 3
    }

    func playing(_ file: String) -> Bool {
        (0..<table.n).contains { holds[$0]?.name == file && busy($0) }
    }

    var inUse: Int { owned.filter { $0 }.count }

    func service(at now: Double) {
        for v in 0..<table.n where !traced[v] {
            let sample = table.startSample[v].load(ordering: .acquiring)
            if sample < 0 {
                if !owned[v] && !busy(v) { traced[v] = true }         // stopped before its first frame
                continue
            }
            traced[v] = true
            let host = table.startHost[v].load(ordering: .relaxed)
            trace?.line("voice", [
                "voice": v, "sid": tags[v], "file": holds[v]?.name ?? "", "sample": sample,
                "host_ns": host == 0 ? NSNull() : Int(hostNs(host)) as Any,
                "latency": latency, "rate": rate, "output": name, "uptime_ns": Int(uptimeNs()),
            ])
        }
        for v in 0..<table.n where !owned[v] && !busy(v) {
            holds[v] = nil                                            // the render let go
        }
    }

    func status() -> [String: Any] {
        var s: [String: Any] = [
            "output": name, "device": device, "rate": rate, "latency": latency,
            "volume": Double(table.gain), "decoded": sounds.count,
            "decoded_bytes": sounds.values.reduce(0) { $0 + $1.frames * 4 },
            "fading": (0..<table.n).filter { !owned[$0] && busy($0) }.count,
            "restarts": restarts, "running": engine?.isRunning ?? false,
        ]
        if mode == .offline { s["rendered_frames"] = rendered }
        return s
    }
}

// MARK: - OSC

enum Message {
    case event(String, String, Double?)
    case ping(Double?)
    case malformed
}

/// A valid sid can name a session dir (busy.is_sid).
func isSid(_ s: String) -> Bool { !s.isEmpty && s != "." && s != ".." && !s.contains("/") }

func parse(_ b: [UInt8]) -> Message {
    var off = 0
    func string() -> String? {
        guard off < b.count, let nul = b[off...].firstIndex(of: 0) else { return nil }
        let s = String(bytes: b[off..<nul], encoding: .utf8)
        let next = (nul + 4) & ~3
        guard next <= b.count, b[nul..<next].allSatisfy({ $0 == 0 }) else { return nil }
        off = next
        return s
    }
    func double() -> Double? {
        guard off + 8 <= b.count else { return nil }
        var u: UInt64 = 0
        for x in b[off..<off + 8] { u = u << 8 | UInt64(x) }
        off += 8
        let d = Double(bitPattern: u)
        return d.isFinite ? d : nil
    }
    guard b.count % 4 == 0, let address = string() else { return .malformed }
    let types = off < b.count ? string() : ","
    guard let tags = types else { return .malformed }
    switch address {
    case "/sound/event":
        guard tags == ",ssd" || tags == ",ss",
              let ev = string(), let sid = string(),
              !ev.isEmpty, ev.utf8.count <= maxText, sid.utf8.count <= maxText, isSid(sid)
        else { return .malformed }
        var t: Double?
        if tags == ",ssd" {
            guard let d = double() else { return .malformed }
            t = d
        }
        return off == b.count ? .event(ev, sid, t) : .malformed
    case "/sound/ping":
        var t: Double?
        if tags == ",d" {
            guard let d = double() else { return .malformed }
            t = d
        } else if tags != "," {
            return .malformed
        }
        return off == b.count ? .ping(t) : .malformed
    default:
        return .malformed
    }
}

func oscString(_ s: String) -> [UInt8] {
    var b = Array(s.utf8)
    b.append(0)
    while b.count % 4 != 0 { b.append(0) }
    return b
}

func pong(_ t: Double, _ status: String) -> [UInt8] {
    var b = oscString("/sound/pong") + oscString(",ds")
    withUnsafeBytes(of: t.bitPattern.bigEndian) { b += $0 }
    return b + oscString(status)
}

// MARK: - the state machine

enum Phase {
    case counting(Double)   // the deadline
    case playing(Int)       // the voice
    case latched

    var label: String {
        switch self {
        case .counting: return "counting"
        case .playing: return "playing"
        case .latched: return "latched"
        }
    }
}

struct Seat {
    var phase: Phase
    let rec: Record
    /// The mouth path refused at a tick, logged and counted once while it
    /// stays refused; a mouth that comes back clears it.
    var mouthRefused: String? = nil
}

final class Machine {
    let world: World
    let records: Records
    let out: Output
    var port = 0
    private(set) var seats: [String: Seat] = [:]
    private var used: [String: Double] = [:]        // a loaded file -> its last use
    private var refused: [String: (why: String, at: Double)] = [:]   // sid -> the last refusal logged
    private var lastRefused: [String: Any]?          // the last sound refused, for status
    private var lastAdvance = 0.0
    private var nextSweep: Double?
    private(set) var counts = ["malformed": 0, "stale": 0, "unknown": 0, "refused": 0]

    init(world: World, records: Records, out: Output) {
        self.world = world
        self.records = records
        self.out = out
    }

    /// One datagram's message. Returns the pong to send back, if any.
    func handle(_ m: Message) -> [UInt8]? {
        switch m {
        case .malformed:
            counts["malformed", default: 0] += 1
        case let .event(ev, sid, t):
            event(ev, sid, sent: t)
        case let .ping(t):
            return pong(t ?? 0, json(status()))
        }
        return nil
    }

    func event(_ ev: String, _ sid: String, sent: Double?) {
        let now = world.now()
        switch ev {
        case "arm":
            if let s = sent, world.wall() - s > staleAfter {
                counts["stale", default: 0] += 1
                return
            }
            if seats[sid] != nil { return }                     // repeats are no-ops
            let (rec, why) = records.find(sid, world)
            guard let r = rec else {
                if refused[sid]?.why != why {                   // once, not per tool
                    // Pruned as it grows: a sid refused more
                    // than ten minutes ago is forgotten, and may log once again.
                    refused = refused.filter { now - $0.value.at < evictAfter }
                    refused[sid] = (why, now)
                    if why == "records-refused", let (path, kind) = records.lastRefusal {
                        note(["sid": sid, "what": "records", "path": path, "reason": "tcc",
                              "class": kind, "at": "arm"])
                    } else {
                        journal.emit(why, ["sid": sid, "at": "arm"])
                    }
                }
                return
            }
            refused[sid] = nil
            // The mouth is read every tick: refused HERE, before the first
            // tick touches it. The seat latches (silent until end, stop or
            // clear), and every other seat goes on as before.
            if let m = r.mouth, !m.isEmpty, let (path, kind) = mouthRefusal(m) {
                seats[sid] = Seat(phase: refuse(sid, path, "tcc", kind: kind, what: "mouth"), rec: r)
                return
            }
            seats[sid] = Seat(phase: .counting(now + r.delay), rec: r)
            journal.emit("armed", ["sid": sid, "delay": r.delay, "file": r.file,
                                   "pid": Int(r.pid), "record": r.path])
            advance()
        case "end", "stop", "clear":
            guard let s = seats.removeValue(forKey: sid) else { return }
            var f: [String: Any] = ["sid": sid, "by": ev, "from": s.phase.label]
            if case let .playing(v) = s.phase {
                f.merge(out.stop(v, at: now)) { a, _ in a }
                used[s.rec.file] = now
            }
            journal.emit("stopped", f)
        default:
            counts["unknown", default: 0] += 1                  // a later sender's name
        }
    }

    /// Everything due at the world's now: deadlines, the guard, voices that
    /// played out, evictions, the latched sweep.
    func advance() {
        let now = world.now()
        lastAdvance = now
        out.service(at: now)
        for sid in seats.keys.sorted() {
            guard var s = seats[sid] else { continue }
            switch s.phase {
            case let .counting(deadline):
                if !world.alive(s.rec.pid) {
                    seats[sid] = nil
                    journal.emit("seat-gone", ["sid": sid, "from": "counting", "pid": Int(s.rec.pid)])
                    continue
                }
                if holds(sid, &s) {
                    s.phase = .counting(now + s.rec.delay)       // restarts at the full delay
                } else if now + epsilon >= deadline {
                    s.phase = play(sid, s.rec, now)
                }
                seats[sid] = s
            case let .playing(v):
                if !world.alive(s.rec.pid) {
                    var f: [String: Any] = ["sid": sid, "from": "playing", "pid": Int(s.rec.pid)]
                    f.merge(out.stop(v, at: now)) { a, _ in a }
                    used[s.rec.file] = now
                    seats[sid] = nil
                    journal.emit("seat-gone", f)
                } else if holds(sid, &s) {
                    var f: [String: Any] = ["sid": sid]
                    f.merge(out.stop(v, at: now)) { a, _ in a }
                    used[s.rec.file] = now
                    s.phase = .latched
                    seats[sid] = s
                    journal.emit("cut", f)
                } else if let done = out.finished(v, at: now) {
                    var f: [String: Any] = ["sid": sid]
                    f.merge(done) { a, _ in a }
                    used[s.rec.file] = now
                    s.phase = .latched
                    seats[sid] = s
                    journal.emit("finished", f)
                } else {
                    seats[sid] = s                              // its mouthRefused, if it changed
                }
            case .latched:
                break
            }
        }
        for (file, last) in used where now + epsilon >= last + evictAfter {
            if out.playing(file) {
                used[file] = now                                // in use: not unused
            } else {
                out.evict(file)
                used[file] = nil
                journal.emit("evicted", ["file": file])
            }
        }
        if let due = nextSweep, now + epsilon >= due {
            nextSweep = nil
            for sid in seats.keys.sorted() {
                if let s = seats[sid], case .latched = s.phase, !world.alive(s.rec.pid) {
                    seats[sid] = nil
                    journal.emit("seat-gone", ["sid": sid, "from": "latched", "pid": Int(s.rec.pid)])
                }
            }
        }
        if nextSweep == nil, seats.values.contains(where: { if case .latched = $0.phase { true } else { false } }) {
            nextSweep = now + sweepEvery
        }
    }

    /// The seat's mouth holds it at this tick: speaking, or refused. A mouth
    /// that turned refused after the arm is logged and counted like one
    /// refused at arm, once while it stays refused.
    private func holds(_ sid: String, _ s: inout Seat) -> Bool {
        switch world.mouthNow(s.rec.mouth) {
        case .silent:
            s.mouthRefused = nil
            return false
        case .speaking:
            s.mouthRefused = nil
            return true
        case let .refused(path, kind):
            if s.mouthRefused != path {
                s.mouthRefused = path
                note(["sid": sid, "what": "mouth", "path": path, "reason": "tcc", "class": kind,
                      "at": "tick"])
            }
            return true
        }
    }

    /// WHAT is the sound file (logged as `file`, as before) or the mouth
    /// (`path`); KIND is a TCC refusal's class.
    private func refuse(_ sid: String, _ file: String, _ why: String, kind: String? = nil,
                        what: String = "sound") -> Phase {
        var f: [String: Any] = ["sid": sid, what == "sound" ? "file" : "path": file, "reason": why]
        if what != "sound" { f["what"] = what }
        if let k = kind { f["class"] = k }
        note(f)
        return .latched                                         // no retry inside the batch
    }

    /// One refusal: counted, the last one kept for status, and logged.
    private func note(_ f: [String: Any]) {
        counts["refused", default: 0] += 1
        lastRefused = f
        journal.emit("refused", f)
    }

    private func play(_ sid: String, _ r: Record, _ now: Double) -> Phase {
        // Refused BEFORE any output opens it: no privacy prompt on the screen.
        // The output decodes the path the check resolved, never the name
        // again: a link moved between the two would lead it somewhere
        // unchecked. The name stays the key and the log's `file`.
        let (kind, checked) = resolve(r.file)
        if let k = kind { return refuse(sid, r.file, "tcc", kind: k) }
        world.gap(r.file, at: "check")
        if let why = out.load(r.file, from: checked) { return refuse(sid, r.file, why) }
        used[r.file] = now
        guard let v = out.start(r.file, tag: sid, at: now) else {
            return refuse(sid, r.file, "voices")
        }
        journal.emit("played", ["sid": sid, "file": r.file, "voice": v])
        return .playing(v)
    }

    /// When advance() next has work: a deadline, a voice's end, the tick
    /// (only while something counts or plays), an eviction, the sweep.
    /// SWEEP false leaves the latched sweep out: a script with no `until`
    /// ends when nothing else is pending, not one minute at a time forever.
    func nextWake(sweep: Bool = true) -> Double? {
        var wakes: [Double] = []
        var active = false
        for s in seats.values {
            switch s.phase {
            case let .counting(d):
                active = true
                // A deadline already past means the mouth holds it: the tick
                // polls that. Waking at a past deadline would spin.
                if d > lastAdvance + epsilon { wakes.append(d) }
            case let .playing(v):
                active = true
                if let e = out.ends(v) { wakes.append(e) }
            case .latched:
                break
            }
        }
        if active { wakes.append(lastAdvance + tickEvery) }
        if let oldest = used.values.min() { wakes.append(oldest + evictAfter) }
        if sweep, let s = nextSweep { wakes.append(s) }
        return wakes.min()
    }

    func status() -> [String: Any] {
        var s: [String: Any] = [
            "pid": Int(getpid()), "port": port, "sids": seats.count, "voices": out.inUse,
            "voices_max": voiceCount, "files": used.count,
        ]
        for (k, v) in counts { s[k] = v }
        if let last = lastRefused { s["last_refused"] = last }
        s.merge(out.status()) { a, _ in a }
        return s
    }
}

// MARK: - live: UDP on 127.0.0.1

/// The control queue: the state machine, the timers, the output's control side.
let control = DispatchQueue(label: "sound-engine.control")

/// One trace line per datagram (--trace): when it arrived, and what it said.
func traceDatagram(_ m: Message, at: UInt64, wall: Double) {
    guard let tr = trace else { return }
    var f: [String: Any] = ["host_ns": Int(at), "wall": wall]
    switch m {
    case let .event(ev, sid, t):
        f["kind"] = "event"
        f["event"] = ev
        f["sid"] = sid
        f["t"] = t.map { $0 as Any } ?? NSNull()
    case let .ping(t):
        f["kind"] = "ping"
        f["t"] = t.map { $0 as Any } ?? NSNull()
    case .malformed:
        f["kind"] = "malformed"
    }
    tr.line("datagram", f)
}

func runLive(port: Int, machine: Machine, world: World) -> Never {
    let fd = socket(AF_INET, SOCK_DGRAM, 0)
    guard fd >= 0 else {
        journal.emit("error", ["what": "socket", "errno": Int(errno)])
        exit(1)
    }
    var sa = sockaddr_in()
    sa.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
    sa.sin_family = sa_family_t(AF_INET)
    sa.sin_port = UInt16(port).bigEndian
    sa.sin_addr.s_addr = inet_addr("127.0.0.1")              // loopback only, never 0.0.0.0
    let bound = withUnsafePointer(to: &sa) {
        $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
            bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
        }
    }
    guard bound == 0 else {
        journal.emit("error", ["what": "bind", "port": port, "errno": Int(errno),
                               "reason": String(cString: strerror(errno))])
        exit(1)
    }
    var got = sockaddr_in()
    var len = socklen_t(MemoryLayout<sockaddr_in>.size)
    _ = withUnsafeMutablePointer(to: &got) {
        $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { getsockname(fd, $0, &len) }
    }
    let actual = Int(UInt16(bigEndian: got.sin_port))
    machine.port = actual

    let q = control
    let timer = DispatchSource.makeTimerSource(queue: q)
    func reschedule() {
        if let w = machine.nextWake() {
            let ns = max(0, (w - world.now()) * 1e9)
            timer.schedule(deadline: .now() + .nanoseconds(Int(ns)), leeway: .milliseconds(1))
        } else {
            timer.schedule(deadline: .distantFuture)
        }
    }
    timer.setEventHandler {
        machine.advance()
        reschedule()
    }
    reschedule()
    timer.resume()

    var signals: [DispatchSourceSignal] = []
    for (sig, name) in [(SIGTERM, "TERM"), (SIGINT, "INT")] {
        signal(sig, SIG_IGN)
        let src = DispatchSource.makeSignalSource(signal: sig, queue: q)
        src.setEventHandler {
            journal.emit("exit", ["signal": name])
            exit(0)
        }
        src.resume()
        signals.append(src)
    }

    journal.emit("ready", ["host": "127.0.0.1", "port": actual, "output": machine.out.name,
                           "pid": Int(getpid()), "sessions": machine.records.root])

    let reader = Thread {
        var buf = [UInt8](repeating: 0, count: 4096)
        while true {
            var from = sockaddr_in()
            var flen = socklen_t(MemoryLayout<sockaddr_in>.size)
            let n = buf.withUnsafeMutableBytes { p in
                withUnsafeMutablePointer(to: &from) {
                    $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                        recvfrom(fd, p.baseAddress, p.count, 0, $0, &flen)
                    }
                }
            }
            if n < 0 {
                if errno == EINTR { continue }
                journal.emit("error", ["what": "recvfrom", "errno": Int(errno)])
                exit(1)
            }
            // A datagram that fills the buffer may have been truncated: malformed.
            let at = uptimeNs()
            let wall = Date().timeIntervalSince1970
            let msg: Message = n >= buf.count ? .malformed : parse(Array(buf[0..<n]))
            let peer = from
            q.async {
                traceDatagram(msg, at: at, wall: wall)
                if let reply = machine.handle(msg) {
                    var to = peer
                    _ = reply.withUnsafeBytes { p in
                        withUnsafePointer(to: &to) {
                            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                                sendto(fd, p.baseAddress, p.count, 0, $0,
                                       socklen_t(MemoryLayout<sockaddr_in>.size))
                            }
                        }
                    }
                }
                reschedule()
            }
        }
    }
    reader.start()
    withExtendedLifetime(signals) { dispatchMain() }
}

// MARK: - script: virtual time

func runScript(_ path: String, machine: Machine, world: ScriptWorld, offline: AudioOutput? = nil) -> Never {
    func fail(_ why: String) -> Never {
        journal.emit("error", ["what": "script", "reason": why])
        exit(2)
    }
    /// --offline: the audio graph renders every frame up to T before the
    /// clock moves there, so what happens at T starts at T's frame.
    func renderTo(_ t: Double) {
        do { try offline?.render(to: t) } catch { fail("offline render: \(error)") }
    }
    guard let data = FileManager.default.contents(atPath: path),
          let obj = try? JSONSerialization.jsonObject(with: data),
          let top = obj as? [String: Any],
          let raw = top["steps"] as? [[String: Any]]
    else { fail("unreadable, or no \"steps\" list") }
    let until = (top["until"] as? NSNumber)?.doubleValue
    var steps: [(Double, Int, [String: Any])] = []
    for (i, s) in raw.enumerated() {
        guard let t = (s["t"] as? NSNumber)?.doubleValue, t.isFinite, t >= 0 else {
            fail("step \(i) has no t >= 0")
        }
        steps.append((t, i, s))
    }
    steps.sort { $0.0 != $1.0 ? $0.0 < $1.0 : $0.1 < $1.1 }   // stable: by t, then as written

    func apply(_ s: [String: Any]) {
        if let ev = s["event"] as? String {
            guard let sid = s["sid"] as? String else { fail("an event step needs a sid") }
            machine.event(ev, sid, sent: (s["sent"] as? NSNumber)?.doubleValue)
        } else if let hex = s["datagram"] as? String {
            var bytes: [UInt8] = []
            var it = hex.makeIterator()
            while let a = it.next() {
                guard let b = it.next(), let v = UInt8(String([a, b]), radix: 16) else {
                    fail("datagram is not hex")
                }
                bytes.append(v)
            }
            if let reply = machine.handle(parse(bytes)) {
                journal.emit("pong", ["bytes": reply.count])
            }
        } else if s["ping"] != nil {
            journal.emit("status", machine.status())
        } else if s.keys.contains("mouth") {
            world.mouth = s["mouth"] as? Bool                  // null: back to the real probe
        } else if let pid = (s["kill"] as? NSNumber)?.int32Value {
            world.dead.insert(pid)
        } else if let pid = (s["revive"] as? NSNumber)?.int32Value {
            world.dead.remove(pid)
        } else if let path = s["relink"] as? String {
            guard let to = s["to"] as? String else { fail("a relink step needs a to") }
            let at = s["at"] as? String ?? "check"
            guard ["check", "open", "decode"].contains(at) else {
                fail("a relink step's at is check, open or decode")
            }
            world.relinks[path] = (to, at)
        } else {
            fail("unknown step \(json(s))")
        }
    }

    var i = 0
    var spins = 0
    while true {
        spins += 1
        if spins > 2_000_000 { fail("runaway: the clock stopped moving") }
        let stepT: Double? = i < steps.count ? steps[i].0 : nil
        let wake = machine.nextWake(sweep: stepT != nil || until != nil)
        guard let next = [stepT, wake].compactMap({ $0 }).min() else { break }
        if let u = until, next > u + epsilon {
            renderTo(u)
            world.t = max(world.t, u)
            break
        }
        renderTo(next)
        world.t = max(world.t, next)
        machine.advance()                                   // what was due, in the old world
        while i < steps.count, steps[i].0 <= world.t + epsilon {
            apply(steps[i].2)
            machine.advance()
            i += 1
        }
    }
    if let u = until { renderTo(u) }                   // the file runs to `until`, even when idle
    journal.emit("done", machine.status())
    offline?.close()
    exit(0)
}

// MARK: - main

func usage(_ why: String) -> Never {
    let text = """
    sound-engine: \(why)
    usage: sound-engine [--port N] [--sessions DIR] [--volume V] [--trace FILE]
           sound-engine --null-output [--port N] [--sessions DIR] [--null-length S] [--trace FILE]
           sound-engine --script STEPS.json [--sessions DIR] [--null-length S]
           sound-engine --script STEPS.json --offline OUT.wav [--sessions DIR] [--volume V] [--trace FILE]
           sound-engine --classify PATH [--classify PATH ...] [--mounts FILE]
    (every mode also takes --evict-after S and --mounts FILE, for tests)

    """
    FileHandle.standardError.write(Data(text.utf8))
    exit(2)
}

func expand(_ p: String) -> String {
    if p == "~" || p.hasPrefix("~/") {
        let home = ProcessInfo.processInfo.environment["HOME"] ?? NSHomeDirectory()
        return home + p.dropFirst(1)
    }
    return p
}

var port = defaultPort
var sessions = "~/.session/sessions"
var nullOutput = false
var nullLength = 1.0
var script: String?
var offlinePath: String?
var tracePath: String?
var volume: Float = 1
var classify: [String] = []
var mountsPath: String?
var args = CommandLine.arguments.dropFirst().makeIterator()
while let a = args.next() {
    func value() -> String {
        guard let v = args.next() else { usage("\(a) needs a value") }
        return v
    }
    switch a {
    case "--port":
        guard let p = Int(value()), (0...65535).contains(p) else { usage("--port takes 0..65535") }
        port = p
    case "--sessions": sessions = value()
    case "--null-output": nullOutput = true
    case "--null-length":
        guard let s = Double(value()), s.isFinite, s > 0 else { usage("--null-length takes seconds > 0") }
        nullLength = s
    case "--script": script = value()
    case "--offline": offlinePath = value()
    case "--trace": tracePath = value()
    case "--volume":
        guard let v = Float(value()), v.isFinite, v >= 0, v <= 1 else { usage("--volume takes 0..1") }
        volume = v
    case "--classify": classify.append(value())
    case "--mounts": mountsPath = value()
    case "--evict-after":
        guard let s = Double(value()), s.isFinite, s > 0 else { usage("--evict-after takes seconds > 0") }
        evictAfter = s
    case "-h", "--help": usage("the busy sound's engine")
    default: usage("unknown argument \(a)")
    }
}
if let m = mountsPath {
    // [{"on": DIR, "kind": "network"|"removable"}]: mounts a test cannot make.
    guard let data = FileManager.default.contents(atPath: expand(m)),
          let list = (try? JSONSerialization.jsonObject(with: data)) as? [[String: Any]] else {
        usage("--mounts: cannot read a JSON list from \(m)")
    }
    for x in list {
        guard let on = x["on"] as? String, on.hasPrefix("/"),
              let kind = x["kind"] as? String, ["network", "removable"].contains(kind) else {
            usage("--mounts: each entry is {\"on\": ABS, \"kind\": \"network\"|\"removable\"}")
        }
        extraMounts.append((on.count > 1 && on.hasSuffix("/") ? String(on.dropLast()).lowercased()
                                                               : on.lowercased(), kind))
    }
}
if !classify.isEmpty {
    // The classifier alone, from strings: one JSON line per PATH on stdout, with
    // every path it lstat()ed on the way (never one inside a refused place).
    for path in classify {
        var touched: [String] = []
        let kind = refusal(path) { touched.append($0) }
        let line: [String: Any] = ["path": path, "refused": kind ?? NSNull(), "touched": touched]
        let data = (try? JSONSerialization.data(withJSONObject: line, options: [.sortedKeys])) ?? Data()
        FileHandle.standardOutput.write(data + Data("\n".utf8))
    }
    exit(0)
}
if offlinePath != nil && script == nil { usage("--offline renders a --script; give one") }
if offlinePath != nil && nullOutput { usage("--offline and --null-output are two different outputs") }
if let p = tracePath {
    guard let t = Trace(expand(p)) else { usage("--trace: cannot open \(p)") }
    trace = t
}

let records = Records(root: expand(sessions))
/// A sessions root in a protected place is never read: said once, at start.
func sayRootRefused() {
    if let kind = records.rootRefused {
        journal.emit("refused", ["what": "sessions", "path": records.root, "reason": "tcc", "class": kind])
    }
}
if let path = script {
    let world = ScriptWorld()
    journal.virtualNow = { world.t }
    sayRootRefused()
    var offline: AudioOutput?
    if let o = offlinePath {
        do {
            offline = try AudioOutput(offline: expand(o), volume: volume, clock: { world.t })
        } catch {
            journal.emit("error", ["what": "offline", "reason": "\(error)"])
            exit(2)
        }
    }
    let out: Output = offline ?? NullOutput(length: nullLength)
    out.gap = { world.gap($0, at: $1) }
    runScript(path, machine: Machine(world: world, records: records, out: out), world: world,
              offline: offline)
}
let world = RealWorld()
sayRootRefused()
if !nullOutput, let fence = ProcessInfo.processInfo.environment["SESSIONTOOLS_SOUND_NO_DEVICE"], !fence.isEmpty {
    usage("the audio device is fenced off (SESSIONTOOLS_SOUND_NO_DEVICE is set: tests); "
          + "use --null-output, --script or --script --offline")
}
if nullOutput {
    runLive(port: port, machine: Machine(world: world, records: records, out: NullOutput(length: nullLength)),
            world: world)
}
let audio = AudioOutput(live: volume, queue: control, clock: { world.now() })
audio.begin()                                           // the device: the launchd agent's mode
runLive(port: port, machine: Machine(world: world, records: records, out: audio), world: world)
