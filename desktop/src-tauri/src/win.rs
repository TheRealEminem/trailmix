//! The few plain Win32 calls the shell needs outside of audio, declared here so they don't depend on a
//! particular version of the `windows` crate: which Windows this is, the process list, a job object that
//! takes the server down with the app, and a Ctrl+Break for stopping the server politely.

use std::ffi::c_void;
use std::os::windows::io::AsRawHandle;

#[repr(C)]
struct OsVersionInfoW {
    size: u32,
    major: u32,
    minor: u32,
    build: u32,
    platform: u32,
    csd: [u16; 128],
}

#[repr(C)]
struct ProcessEntry32W {
    size: u32,
    usage: u32,
    pid: u32,
    default_heap: usize,
    module: u32,
    threads: u32,
    parent: u32,
    priority: i32,
    flags: u32,
    exe: [u16; 260],
}

#[repr(C)]
#[derive(Default)]
struct JobBasicLimits {
    per_process_user_time: i64,
    per_job_user_time: i64,
    limit_flags: u32,
    min_working_set: usize,
    max_working_set: usize,
    active_process_limit: u32,
    affinity: usize,
    priority_class: u32,
    scheduling_class: u32,
}

#[repr(C)]
#[derive(Default)]
struct JobExtendedLimits {
    basic: JobBasicLimits,
    io: [u64; 6],
    process_memory_limit: usize,
    job_memory_limit: usize,
    peak_process_memory: usize,
    peak_job_memory: usize,
}

const JOB_OBJECT_EXTENDED_LIMIT_INFORMATION: i32 = 9;
const JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: u32 = 0x2000;
const TH32CS_SNAPPROCESS: u32 = 0x2;
const CTRL_BREAK_EVENT: u32 = 1;

#[link(name = "kernel32")]
extern "system" {
    fn CreateJobObjectW(attributes: *const c_void, name: *const u16) -> *mut c_void;
    fn SetInformationJobObject(job: *mut c_void, class: i32, info: *const c_void, len: u32) -> i32;
    fn AssignProcessToJobObject(job: *mut c_void, process: *mut c_void) -> i32;
    fn CloseHandle(handle: *mut c_void) -> i32;
    fn CreateToolhelp32Snapshot(flags: u32, pid: u32) -> *mut c_void;
    fn Process32FirstW(snapshot: *mut c_void, entry: *mut ProcessEntry32W) -> i32;
    fn Process32NextW(snapshot: *mut c_void, entry: *mut ProcessEntry32W) -> i32;
    fn AttachConsole(pid: u32) -> i32;
    fn FreeConsole() -> i32;
    fn GenerateConsoleCtrlEvent(event: u32, group: u32) -> i32;
    fn SetConsoleCtrlHandler(handler: *const c_void, add: i32) -> i32;
}

#[link(name = "ntdll")]
extern "system" {
    fn RtlGetVersion(info: *mut OsVersionInfoW) -> i32;
}

/// The Windows build number (19041 is Windows 10 2004, the first with per-process audio loopback).
pub fn os_build() -> u32 {
    let mut info = OsVersionInfoW { size: std::mem::size_of::<OsVersionInfoW>() as u32, major: 0, minor: 0, build: 0, platform: 0, csd: [0; 128] };
    unsafe {
        if RtlGetVersion(&mut info) == 0 {
            info.build
        } else {
            0
        }
    }
}

pub fn os_version() -> String {
    let mut info = OsVersionInfoW { size: std::mem::size_of::<OsVersionInfoW>() as u32, major: 0, minor: 0, build: 0, platform: 0, csd: [0; 128] };
    unsafe {
        if RtlGetVersion(&mut info) == 0 {
            format!("{}.{}.{}", info.major, info.minor, info.build)
        } else {
            "unknown".into()
        }
    }
}

#[derive(Clone, Debug)]
pub struct Proc {
    pub pid: u32,
    pub parent: u32,
    /// The executable's file name, lower-case ("zoom.exe").
    pub exe: String,
}

/// Every running process.
pub fn processes() -> Vec<Proc> {
    let mut out = Vec::new();
    unsafe {
        let snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
        if snapshot.is_null() || snapshot as isize == -1 {
            return out;
        }
        let mut entry: ProcessEntry32W = std::mem::zeroed();
        entry.size = std::mem::size_of::<ProcessEntry32W>() as u32;
        if Process32FirstW(snapshot, &mut entry) != 0 {
            loop {
                let len = entry.exe.iter().position(|&c| c == 0).unwrap_or(entry.exe.len());
                let exe = String::from_utf16_lossy(&entry.exe[..len]).to_lowercase();
                out.push(Proc { pid: entry.pid, parent: entry.parent, exe });
                if Process32NextW(snapshot, &mut entry) == 0 {
                    break;
                }
            }
        }
        CloseHandle(snapshot);
    }
    out
}

/// A job object with "kill on close": processes put in it end when this app does, however it ends.
pub struct Job(*mut c_void);

unsafe impl Send for Job {}
unsafe impl Sync for Job {}

impl Job {
    pub fn new() -> Option<Job> {
        unsafe {
            let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
            if job.is_null() {
                return None;
            }
            let mut limits = JobExtendedLimits::default();
            limits.basic.limit_flags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            let ok = SetInformationJobObject(
                job,
                JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                &limits as *const JobExtendedLimits as *const c_void,
                std::mem::size_of::<JobExtendedLimits>() as u32,
            );
            if ok == 0 {
                CloseHandle(job);
                return None;
            }
            Some(Job(job))
        }
    }

    pub fn assign(&self, child: &std::process::Child) -> bool {
        unsafe { AssignProcessToJobObject(self.0, child.as_raw_handle() as *mut c_void) != 0 }
    }
}

impl Drop for Job {
    fn drop(&mut self) {
        unsafe {
            CloseHandle(self.0);
        }
    }
}

/// Sends Ctrl+Break to a process started with CREATE_NEW_PROCESS_GROUP (and its children): Python turns it
/// into SIGBREAK, which uvicorn treats like SIGTERM, so the server stops in an orderly way. False if it
/// couldn't be delivered (then the process is terminated instead).
pub fn ctrl_break(pid: u32) -> bool {
    unsafe {
        FreeConsole();
        if AttachConsole(pid) == 0 {
            return false;
        }
        // Not for us: the event goes to the server's process group, but ignore it here too, to be safe.
        SetConsoleCtrlHandler(std::ptr::null(), 1);
        let sent = GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, pid) != 0;
        FreeConsole();
        sent
    }
}

// MARK: Processes by path, the registry and version resources (the updater and open at login)

const SYNCHRONIZE: u32 = 0x0010_0000;
const PROCESS_TERMINATE: u32 = 0x0001;
const PROCESS_QUERY_LIMITED_INFORMATION: u32 = 0x1000;
const WAIT_OBJECT_0: u32 = 0;
const REG_SZ: u32 = 1;
const RRF_RT_REG_SZ: u32 = 0x0000_0002;
const RRF_RT_REG_BINARY: u32 = 0x0000_0008;

#[link(name = "kernel32")]
extern "system" {
    fn OpenProcess(access: u32, inherit: i32, pid: u32) -> *mut c_void;
    fn WaitForSingleObject(handle: *mut c_void, millis: u32) -> u32;
    fn TerminateProcess(process: *mut c_void, code: u32) -> i32;
    fn QueryFullProcessImageNameW(process: *mut c_void, flags: u32, name: *mut u16, size: *mut u32) -> i32;
}

#[link(name = "advapi32")]
extern "system" {
    fn RegSetKeyValueW(key: *mut c_void, subkey: *const u16, value: *const u16, kind: u32, data: *const c_void, len: u32) -> i32;
    fn RegDeleteKeyValueW(key: *mut c_void, subkey: *const u16, value: *const u16) -> i32;
    fn RegGetValueW(key: *mut c_void, subkey: *const u16, value: *const u16, flags: u32, kind: *mut u32, data: *mut c_void, len: *mut u32) -> i32;
}

#[link(name = "version")]
extern "system" {
    fn GetFileVersionInfoSizeW(path: *const u16, handle: *mut u32) -> u32;
    fn GetFileVersionInfoW(path: *const u16, handle: u32, len: u32, data: *mut c_void) -> i32;
    fn VerQueryValueW(block: *const c_void, sub_block: *const u16, buffer: *mut *mut c_void, len: *mut u32) -> i32;
}

fn wide(text: &str) -> Vec<u16> {
    text.encode_utf16().chain(std::iter::once(0)).collect()
}

fn wide_path(path: &std::path::Path) -> Vec<u16> {
    use std::os::windows::ffi::OsStrExt;
    path.as_os_str().encode_wide().chain(std::iter::once(0)).collect()
}

/// HKEY_CURRENT_USER: ((HKEY)(ULONG_PTR)((LONG)0x80000001)), sign-extended on 64-bit Windows.
fn hkcu() -> *mut c_void {
    0x8000_0001u32 as i32 as isize as *mut c_void
}

/// Waits for a process to end. True once it has (or it was already gone); false if it's still running.
pub fn wait_for_exit(pid: u32, timeout: std::time::Duration) -> bool {
    unsafe {
        let handle = OpenProcess(SYNCHRONIZE, 0, pid);
        if handle.is_null() {
            return true; // gone already (or never there)
        }
        let millis = timeout.as_millis().min(0xFFFF_FFFE) as u32;
        let done = WaitForSingleObject(handle, millis) == WAIT_OBJECT_0;
        CloseHandle(handle);
        done
    }
}

/// The full path of a running process's executable.
pub fn process_path(pid: u32) -> Option<std::path::PathBuf> {
    unsafe {
        let handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
        if handle.is_null() {
            return None;
        }
        let mut buffer = vec![0u16; 1024];
        let mut size = buffer.len() as u32;
        let ok = QueryFullProcessImageNameW(handle, 0, buffer.as_mut_ptr(), &mut size) != 0;
        CloseHandle(handle);
        if !ok {
            return None;
        }
        Some(std::path::PathBuf::from(String::from_utf16_lossy(&buffer[..size as usize])))
    }
}

/// The processes running from inside a folder (the install's own Python, ffmpeg…), other than this one.
pub fn processes_in(folder: &std::path::Path) -> Vec<u32> {
    // Compared in full, long form (a short C:\Users\RUNNER~1\... and its long name are the same folder).
    let normal = |p: &std::path::Path| std::fs::canonicalize(p).unwrap_or_else(|_| p.to_path_buf()).to_string_lossy().to_lowercase();
    let folder = normal(folder).trim_end_matches('\\').to_string() + "\\";
    let me = std::process::id();
    processes()
        .into_iter()
        .filter(|p| p.pid != me && p.pid != 0)
        .filter(|p| process_path(p.pid).map(|path| normal(&path).starts_with(&folder)).unwrap_or(false))
        .map(|p| p.pid)
        .collect()
}

pub fn terminate(pid: u32) -> bool {
    unsafe {
        let handle = OpenProcess(PROCESS_TERMINATE, 0, pid);
        if handle.is_null() {
            return false;
        }
        let ok = TerminateProcess(handle, 1) != 0;
        CloseHandle(handle);
        ok
    }
}

/// A text value under HKEY_CURRENT_USER, if it's there.
pub fn reg_get_string(subkey: &str, value: &str) -> Option<String> {
    let (subkey, value) = (wide(subkey), wide(value));
    unsafe {
        let mut len: u32 = 0;
        let found = RegGetValueW(hkcu(), subkey.as_ptr(), value.as_ptr(), RRF_RT_REG_SZ, std::ptr::null_mut(), std::ptr::null_mut(), &mut len);
        if found != 0 {
            return None;
        }
        let mut buffer = vec![0u16; (len as usize / 2) + 1];
        let mut len = (buffer.len() * 2) as u32;
        let read = RegGetValueW(
            hkcu(),
            subkey.as_ptr(),
            value.as_ptr(),
            RRF_RT_REG_SZ,
            std::ptr::null_mut(),
            buffer.as_mut_ptr() as *mut c_void,
            &mut len,
        );
        if read != 0 {
            return None;
        }
        let end = buffer.iter().position(|&c| c == 0).unwrap_or(buffer.len());
        Some(String::from_utf16_lossy(&buffer[..end]))
    }
}

/// A binary value under HKEY_CURRENT_USER, if it's there.
pub fn reg_get_binary(subkey: &str, value: &str) -> Option<Vec<u8>> {
    let (subkey, value) = (wide(subkey), wide(value));
    unsafe {
        let mut len: u32 = 0;
        let found = RegGetValueW(hkcu(), subkey.as_ptr(), value.as_ptr(), RRF_RT_REG_BINARY, std::ptr::null_mut(), std::ptr::null_mut(), &mut len);
        if found != 0 {
            return None;
        }
        let mut buffer = vec![0u8; len as usize];
        let read = RegGetValueW(
            hkcu(),
            subkey.as_ptr(),
            value.as_ptr(),
            RRF_RT_REG_BINARY,
            std::ptr::null_mut(),
            buffer.as_mut_ptr() as *mut c_void,
            &mut len,
        );
        if read != 0 {
            return None;
        }
        buffer.truncate(len as usize);
        Some(buffer)
    }
}

/// Sets a text value under HKEY_CURRENT_USER (making the key if needed).
pub fn reg_set_string(subkey: &str, value: &str, data: &str) -> bool {
    let (subkey, value, data) = (wide(subkey), wide(value), wide(data));
    unsafe { RegSetKeyValueW(hkcu(), subkey.as_ptr(), value.as_ptr(), REG_SZ, data.as_ptr() as *const c_void, (data.len() * 2) as u32) == 0 }
}

/// Removes a value under HKEY_CURRENT_USER. True if it's gone (or wasn't there).
pub fn reg_delete(subkey: &str, value: &str) -> bool {
    let (subkey, value) = (wide(subkey), wide(value));
    let status = unsafe { RegDeleteKeyValueW(hkcu(), subkey.as_ptr(), value.as_ptr()) };
    status == 0 || status == 2 // ERROR_FILE_NOT_FOUND
}

/// A string from a file's version resource ("ProductName", "ProductVersion"…), in whichever language it has.
pub fn version_string(file: &std::path::Path, name: &str) -> Option<String> {
    let path = wide_path(file);
    unsafe {
        let mut ignored: u32 = 0;
        let size = GetFileVersionInfoSizeW(path.as_ptr(), &mut ignored);
        if size == 0 {
            return None;
        }
        let mut block = vec![0u8; size as usize];
        if GetFileVersionInfoW(path.as_ptr(), 0, size, block.as_mut_ptr() as *mut c_void) == 0 {
            return None;
        }
        let base = block.as_ptr() as *const c_void;
        let mut languages: Vec<String> = Vec::new();
        let mut pointer: *mut c_void = std::ptr::null_mut();
        let mut len: u32 = 0;
        let translation = wide("\\VarFileInfo\\Translation");
        if VerQueryValueW(base, translation.as_ptr(), &mut pointer, &mut len) != 0 && !pointer.is_null() {
            let pairs = std::slice::from_raw_parts(pointer as *const u16, (len / 2) as usize);
            for pair in pairs.chunks(2) {
                if pair.len() == 2 {
                    languages.push(format!("{:04x}{:04x}", pair[0], pair[1]));
                }
            }
        }
        languages.extend(["040904b0".to_string(), "040904e4".to_string(), "04090000".to_string()]);
        for language in languages {
            let query = wide(&format!("\\StringFileInfo\\{language}\\{name}"));
            let mut pointer: *mut c_void = std::ptr::null_mut();
            let mut len: u32 = 0;
            if VerQueryValueW(base, query.as_ptr(), &mut pointer, &mut len) != 0 && !pointer.is_null() && len > 0 {
                let chars = std::slice::from_raw_parts(pointer as *const u16, len as usize);
                let end = chars.iter().position(|&c| c == 0).unwrap_or(chars.len());
                return Some(String::from_utf16_lossy(&chars[..end]).trim().to_string());
            }
        }
        None
    }
}

// MARK: More of the registry (which apps are using the microphone, the light or dark theme)

const KEY_READ: u32 = 0x0002_0019;
const REG_QWORD: u32 = 11;
const RRF_RT_REG_DWORD: u32 = 0x0000_0010;
const RRF_RT_REG_QWORD: u32 = 0x0000_0040;
const ERROR_NO_MORE_ITEMS: i32 = 259;

#[link(name = "advapi32")]
extern "system" {
    fn RegOpenKeyExW(key: *mut c_void, subkey: *const u16, options: u32, access: u32, result: *mut *mut c_void) -> i32;
    fn RegEnumKeyExW(
        key: *mut c_void,
        index: u32,
        name: *mut u16,
        name_len: *mut u32,
        reserved: *mut u32,
        class: *mut u16,
        class_len: *mut u32,
        written: *mut u64,
    ) -> i32;
    fn RegCloseKey(key: *mut c_void) -> i32;
    fn RegDeleteTreeW(key: *mut c_void, subkey: *const u16) -> i32;
}

#[link(name = "kernel32")]
extern "system" {
    fn GetSystemTimeAsFileTime(time: *mut u64);
}

/// The names of the keys under a key in HKEY_CURRENT_USER (none if it isn't there).
pub fn reg_subkeys(subkey: &str) -> Vec<String> {
    let subkey = wide(subkey);
    let mut out = Vec::new();
    unsafe {
        let mut key: *mut c_void = std::ptr::null_mut();
        if RegOpenKeyExW(hkcu(), subkey.as_ptr(), 0, KEY_READ, &mut key) != 0 {
            return out;
        }
        let mut index = 0;
        while index < 10_000 {
            let mut name = [0u16; 512];
            let mut len = name.len() as u32;
            let status = RegEnumKeyExW(
                key,
                index,
                name.as_mut_ptr(),
                &mut len,
                std::ptr::null_mut(),
                std::ptr::null_mut(),
                std::ptr::null_mut(),
                std::ptr::null_mut(),
            );
            if status == ERROR_NO_MORE_ITEMS {
                break;
            }
            if status == 0 {
                out.push(String::from_utf16_lossy(&name[..len as usize]));
            }
            index += 1;
        }
        RegCloseKey(key);
    }
    out
}

fn reg_get_number(subkey: &str, value: &str, flags: u32) -> Option<u64> {
    let (subkey, value) = (wide(subkey), wide(value));
    let mut data: u64 = 0;
    let mut len: u32 = if flags == RRF_RT_REG_QWORD { 8 } else { 4 };
    let status = unsafe {
        RegGetValueW(hkcu(), subkey.as_ptr(), value.as_ptr(), flags, std::ptr::null_mut(), &mut data as *mut u64 as *mut c_void, &mut len)
    };
    (status == 0).then_some(data)
}

/// A 64-bit number under HKEY_CURRENT_USER, if it's there.
pub fn reg_get_qword(subkey: &str, value: &str) -> Option<u64> {
    reg_get_number(subkey, value, RRF_RT_REG_QWORD)
}

/// A 32-bit number under HKEY_CURRENT_USER, if it's there.
pub fn reg_get_dword(subkey: &str, value: &str) -> Option<u32> {
    reg_get_number(subkey, value, RRF_RT_REG_DWORD).map(|n| n as u32)
}

/// Sets a 64-bit number under HKEY_CURRENT_USER (making the key if needed). For the self-test.
pub fn reg_set_qword(subkey: &str, value: &str, data: u64) -> bool {
    let (subkey, value) = (wide(subkey), wide(value));
    unsafe { RegSetKeyValueW(hkcu(), subkey.as_ptr(), value.as_ptr(), REG_QWORD, &data as *const u64 as *const c_void, 8) == 0 }
}

/// Removes a key under HKEY_CURRENT_USER and everything in it. For the self-test.
pub fn reg_delete_tree(subkey: &str) -> bool {
    let subkey = wide(subkey);
    let status = unsafe { RegDeleteTreeW(hkcu(), subkey.as_ptr()) };
    status == 0 || status == 2
}

/// Now, as a FILETIME (100 ns steps since 1601), the way the registry keeps times.
pub fn filetime_now() -> u64 {
    let mut time: u64 = 0;
    unsafe { GetSystemTimeAsFileTime(&mut time) };
    time
}

/// Whether apps are asked to look dark (Settings > Personalization > Colors).
pub fn dark_mode() -> bool {
    reg_get_dword("Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize", "AppsUseLightTheme") == Some(0)
}
