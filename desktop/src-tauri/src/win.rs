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
