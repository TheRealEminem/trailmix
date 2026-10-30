//! Recording with WASAPI: your microphone (the default capture device, as MicCapture.swift), and the
//! meeting's other side from other apps (as SystemAudioCapture.swift): a per-process loopback on Windows
//! 10 2004 and later (every app's audio but ours, or one app's process tree), else the whole system's
//! output. Each capture runs on its own thread and hands wire-format audio to a sink.

use std::ffi::c_void;
use std::mem::ManuallyDrop;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::Duration;

use windows::core::{implement, w, Interface, IUnknown, Ref, GUID, HRESULT, PCWSTR};
use windows::Win32::Devices::FunctionDiscovery::PKEY_Device_FriendlyName;
use windows::Win32::Foundation::{CloseHandle, HANDLE};
use windows::Win32::Media::Audio::{
    eCapture, eConsole, eRender, ActivateAudioInterfaceAsync, EDataFlow, IActivateAudioInterfaceAsyncOperation,
    IActivateAudioInterfaceCompletionHandler, IActivateAudioInterfaceCompletionHandler_Impl, IAudioCaptureClient,
    IAudioClient, IMMDevice, IMMDeviceEnumerator, MMDeviceEnumerator, AUDCLNT_SHAREMODE_SHARED,
    AUDIOCLIENT_ACTIVATION_PARAMS, AUDIOCLIENT_ACTIVATION_PARAMS_0, AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK,
    AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS, PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE,
    PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE, WAVEFORMATEX,
};
use windows::Win32::System::Com::StructuredStorage::{PROPVARIANT, PROPVARIANT_0, PROPVARIANT_0_0, PROPVARIANT_0_0_0};
use windows::Win32::System::Com::{CoCreateInstance, CoInitializeEx, CoTaskMemFree, BLOB, CLSCTX_ALL, COINIT_MULTITHREADED, STGM_READ};
use windows::Win32::System::Threading::{CreateEventW, WaitForSingleObject};
use windows::Win32::System::Variant::VT_BLOB;

use super::convert::{Converter, Format, Sample};
use crate::wire::{AudioSource, Sink};

// AUDCLNT_STREAMFLAGS_*
const LOOPBACK: u32 = 0x0002_0000;
const EVENTCALLBACK: u32 = 0x0004_0000;
const AUTOCONVERTPCM: u32 = 0x8000_0000;
const SRC_DEFAULT_QUALITY: u32 = 0x0800_0000;
// AUDCLNT_BUFFERFLAGS_SILENT
const SILENT: u32 = 0x2;
const BUFFER_100NS: i64 = 1_000_000; // 100 ms
const KSDATAFORMAT_SUBTYPE_IEEE_FLOAT: GUID = GUID::from_u128(0x00000003_0000_0010_8000_00aa00389b71);

/// Per-process loopback (ActivateAudioInterfaceAsync with VAD\Process_Loopback) arrived in Windows 10 2004.
pub fn process_loopback_supported() -> bool {
    crate::win::os_build() >= 19041
}

/// COM, once per thread that talks to the audio system.
pub fn com_init() {
    unsafe {
        let _ = CoInitializeEx(None, COINIT_MULTITHREADED);
    }
}

fn explain(what: &str, e: windows::core::Error) -> String {
    // E_ACCESSDENIED on the mic: its privacy switch is off (for every app, or for desktop apps).
    if what == "use the microphone" && e.code().0 as u32 == 0x8007_0005 {
        return format!("Couldn't {what}: {}", super::BLOCKED);
    }
    format!("Couldn't {what} ({})", e.message().trim())
}

pub unsafe fn enumerator() -> windows::core::Result<IMMDeviceEnumerator> {
    CoCreateInstance(&MMDeviceEnumerator, None::<&IUnknown>, CLSCTX_ALL)
}

pub unsafe fn device_name(device: &IMMDevice) -> String {
    device
        .OpenPropertyStore(STGM_READ)
        .and_then(|store| store.GetValue(&PKEY_Device_FriendlyName))
        .map(|value| value.to_string())
        .unwrap_or_default()
}

/// The default microphone's name, if there is one.
pub fn default_microphone() -> Option<String> {
    com_init();
    unsafe {
        let device = enumerator().ok()?.GetDefaultAudioEndpoint(eCapture, eConsole).ok()?;
        Some(device_name(&device))
    }
}

/// The default output device's name, if there is one.
pub fn default_output() -> Option<String> {
    com_init();
    unsafe {
        let device = enumerator().ok()?.GetDefaultAudioEndpoint(eRender, eConsole).ok()?;
        Some(device_name(&device))
    }
}

fn pcm(rate: u32, channels: u16) -> WAVEFORMATEX {
    WAVEFORMATEX {
        wFormatTag: 1, // WAVE_FORMAT_PCM
        nChannels: channels,
        nSamplesPerSec: rate,
        nAvgBytesPerSec: rate * channels as u32 * 2,
        nBlockAlign: channels * 2,
        wBitsPerSample: 16,
        cbSize: 0,
    }
}

fn format_of_pcm(wf: &WAVEFORMATEX) -> Format {
    Format { rate: wf.nSamplesPerSec, channels: wf.nChannels, sample: Sample::I16, block_align: wf.nBlockAlign }
}

/// What a device's mix format really is (WAVEFORMATEXTENSIBLE carries the sample type in a GUID).
unsafe fn format_of(wf: *const WAVEFORMATEX) -> Result<Format, String> {
    let tag = (*wf).wFormatTag;
    let bits = (*wf).wBitsPerSample;
    let cb = (*wf).cbSize;
    let float = if tag == 0xFFFE && cb >= 22 {
        // WAVEFORMATEXTENSIBLE: the SubFormat GUID follows the 18-byte header, the valid bits and the channel mask.
        let sub = std::ptr::read_unaligned((wf as *const u8).add(24) as *const GUID);
        sub == KSDATAFORMAT_SUBTYPE_IEEE_FLOAT
    } else {
        tag == 3 // WAVE_FORMAT_IEEE_FLOAT
    };
    let sample = match (float, bits) {
        (true, 32) => Sample::F32,
        (false, 16) => Sample::I16,
        (false, 32) => Sample::I32,
        _ => return Err(format!("the audio format ({bits}-bit, tag {tag}) isn't supported")),
    };
    Ok(Format { rate: (*wf).nSamplesPerSec, channels: (*wf).nChannels, sample, block_align: (*wf).nBlockAlign })
}

/// A capture that's up and running.
struct Running {
    client: IAudioClient,
    capture: IAudioCaptureClient,
    format: Format,
    event: Option<HANDLE>,
}

impl Drop for Running {
    fn drop(&mut self) {
        unsafe {
            let _ = self.client.Stop();
            if let Some(event) = self.event.take() {
                let _ = CloseHandle(event);
            }
        }
    }
}

unsafe fn finish(client: IAudioClient, format: Format, event: Option<HANDLE>) -> Result<Running, String> {
    let capture: IAudioCaptureClient = client.GetService().map_err(|e| explain("read the audio", e))?;
    if let Some(event) = event {
        client.SetEventHandle(event).map_err(|e| explain("wait for audio", e))?;
    }
    client.Start().map_err(|e| explain("start the audio", e))?;
    Ok(Running { client, capture, format, event })
}

/// The default capture device (the mic), or the default output device's loopback (everything playing).
unsafe fn open_endpoint(flow: EDataFlow, loopback: bool) -> Result<(Running, String), String> {
    let what = if loopback { "listen to other apps" } else { "use the microphone" };
    let device = enumerator()
        .and_then(|e| e.GetDefaultAudioEndpoint(flow, eConsole))
        .map_err(|_| if loopback { "There's no audio output device to listen to".to_string() } else { "No microphone is connected".to_string() })?;
    let name = device_name(&device);
    let base = if loopback { LOOPBACK } else { 0 };
    // Ask for the wire format outright: Windows 10 and later resample and mix down in the audio engine.
    let client: IAudioClient = device.Activate(CLSCTX_ALL, None).map_err(|e| explain(what, e))?;
    let wire = pcm(16000, 1);
    if client.Initialize(AUDCLNT_SHAREMODE_SHARED, base | AUTOCONVERTPCM | SRC_DEFAULT_QUALITY, BUFFER_100NS, 0, &wire, None).is_ok() {
        return Ok((finish(client, Format::WIRE, None)?, name));
    }
    // Otherwise take what the device gives and convert here.
    let client: IAudioClient = device.Activate(CLSCTX_ALL, None).map_err(|e| explain(what, e))?;
    let mix = client.GetMixFormat().map_err(|e| explain(what, e))?;
    let format = format_of(mix);
    let initialized = client.Initialize(AUDCLNT_SHAREMODE_SHARED, base, BUFFER_100NS, 0, mix, None);
    CoTaskMemFree(Some(mix as *const c_void));
    initialized.map_err(|e| explain(what, e))?;
    Ok((finish(client, format?, None)?, name))
}

/// Answers the audio system when the per-process loopback interface is ready.
#[implement(IActivateAudioInterfaceCompletionHandler)]
struct Activated(mpsc::Sender<()>);

impl IActivateAudioInterfaceCompletionHandler_Impl for Activated_Impl {
    fn ActivateCompleted(&self, _operation: Ref<'_, IActivateAudioInterfaceAsyncOperation>) -> windows::core::Result<()> {
        let _ = self.0.send(());
        Ok(())
    }
}

/// An audio client on the per-process loopback device: `pid`'s process tree only, or everything but it.
unsafe fn activate_process_loopback(pid: u32, exclude: bool) -> Result<IAudioClient, String> {
    let params = AUDIOCLIENT_ACTIVATION_PARAMS {
        ActivationType: AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK,
        Anonymous: AUDIOCLIENT_ACTIVATION_PARAMS_0 {
            ProcessLoopbackParams: AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS {
                TargetProcessId: pid,
                ProcessLoopbackMode: if exclude {
                    PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE
                } else {
                    PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE
                },
            },
        },
    };
    // A PROPVARIANT pointing at the params. ManuallyDrop: dropping a PROPVARIANT frees what it points at,
    // and this one points at the stack.
    let prop = ManuallyDrop::new(PROPVARIANT {
        Anonymous: PROPVARIANT_0 {
            Anonymous: ManuallyDrop::new(PROPVARIANT_0_0 {
                vt: VT_BLOB,
                wReserved1: 0,
                wReserved2: 0,
                wReserved3: 0,
                Anonymous: PROPVARIANT_0_0_0 {
                    blob: BLOB {
                        cbSize: std::mem::size_of::<AUDIOCLIENT_ACTIVATION_PARAMS>() as u32,
                        pBlobData: &params as *const AUDIOCLIENT_ACTIVATION_PARAMS as *mut u8,
                    },
                },
            }),
        },
    });
    let (tx, rx) = mpsc::channel();
    let handler: IActivateAudioInterfaceCompletionHandler = Activated(tx).into();
    let operation = ActivateAudioInterfaceAsync(
        w!("VAD\\Process_Loopback"),
        &IAudioClient::IID,
        Some(&*prop as *const PROPVARIANT),
        &handler,
    )
    .map_err(|e| explain("listen to other apps", e))?;
    rx.recv_timeout(Duration::from_secs(5)).map_err(|_| "Windows didn't answer the request to listen to other apps".to_string())?;
    let mut result = HRESULT(0);
    let mut interface: Option<IUnknown> = None;
    operation.GetActivateResult(&mut result, &mut interface).map_err(|e| explain("listen to other apps", e))?;
    result.ok().map_err(|e| explain("listen to other apps", e))?;
    interface
        .ok_or_else(|| "Windows gave no audio interface for other apps".to_string())?
        .cast::<IAudioClient>()
        .map_err(|e| explain("listen to other apps", e))
}

unsafe fn open_process_loopback(pid: u32, exclude: bool) -> Result<Running, String> {
    let mut last = String::new();
    // The loopback device takes the format we name; the wire format first, then the usual mix formats.
    for wf in [pcm(16000, 1), pcm(48000, 2), pcm(44100, 2)] {
        let client = activate_process_loopback(pid, exclude)?;
        match client.Initialize(AUDCLNT_SHAREMODE_SHARED, LOOPBACK | EVENTCALLBACK, BUFFER_100NS, 0, &wf, None) {
            Ok(()) => {
                let event = CreateEventW(None, false, false, PCWSTR::null()).map_err(|e| explain("wait for audio", e))?;
                return finish(client, format_of_pcm(&wf), Some(event));
            }
            Err(e) => last = explain("listen to other apps", e),
        }
    }
    Err(last)
}

/// Reads packets as they come and hands them on, until `stop`. Errors out if the device goes away.
fn pump(run: &Running, sink: &Sink, stop: &AtomicBool) -> Result<(), String> {
    let mut converter = (!run.format.is_wire()).then(|| Converter::new(run.format));
    let block = run.format.block_align as usize;
    while !stop.load(Ordering::Relaxed) {
        unsafe {
            match run.event {
                Some(event) => {
                    WaitForSingleObject(event, 100);
                }
                None => thread::sleep(Duration::from_millis(10)),
            }
            loop {
                let packet = run.capture.GetNextPacketSize().map_err(|e| explain("read the audio", e))?;
                if packet == 0 {
                    break;
                }
                let mut data: *mut u8 = std::ptr::null_mut();
                let mut frames = 0u32;
                let mut flags = 0u32;
                run.capture.GetBuffer(&mut data, &mut frames, &mut flags, None, None).map_err(|e| explain("read the audio", e))?;
                let n = frames as usize;
                let silence;
                let bytes: &[u8] = if flags & SILENT != 0 || data.is_null() {
                    silence = vec![0u8; n * block];
                    &silence
                } else {
                    std::slice::from_raw_parts(data, n * block)
                };
                let samples: Vec<i16> = match converter.as_mut() {
                    Some(c) => c.convert(bytes),
                    None => bytes.chunks_exact(2).map(|c| i16::from_le_bytes([c[0], c[1]])).collect(),
                };
                run.capture.ReleaseBuffer(frames).map_err(|e| explain("read the audio", e))?;
                if !samples.is_empty() {
                    sink(&samples);
                }
            }
        }
    }
    Ok(())
}

/// Your side of the conversation, from the default microphone, as wire-format audio. If that mic disappears
/// mid-meeting (a headset unplugged), it carries on with whatever is the default then.
pub struct MicCapture {
    name: Arc<Mutex<String>>,
    stop: Arc<AtomicBool>,
    thread: Option<JoinHandle<()>>,
}

impl MicCapture {
    pub fn new() -> MicCapture {
        MicCapture { name: Arc::new(Mutex::new(String::new())), stop: Arc::new(AtomicBool::new(false)), thread: None }
    }
}

impl AudioSource for MicCapture {
    fn name(&self) -> String {
        self.name.lock().map(|n| n.clone()).unwrap_or_default()
    }

    fn start(&mut self, sink: Sink) -> Result<(), String> {
        let stop = self.stop.clone();
        let name = self.name.clone();
        let (tx, rx) = mpsc::channel::<Result<(), String>>();
        self.thread = Some(thread::spawn(move || {
            com_init();
            let (run, device) = match unsafe { open_endpoint(eCapture, false) } {
                Ok(opened) => opened,
                Err(e) => {
                    let _ = tx.send(Err(e));
                    return;
                }
            };
            *name.lock().unwrap() = device;
            let _ = tx.send(Ok(()));
            let mut run = run;
            while !stop.load(Ordering::Relaxed) {
                if let Err(e) = pump(&run, &sink, &stop) {
                    eprintln!("[audio] microphone stopped: {e}; trying the default microphone again");
                }
                drop(run);
                // The mic went away: use the default one from now on.
                loop {
                    if stop.load(Ordering::Relaxed) {
                        return;
                    }
                    thread::sleep(Duration::from_secs(1));
                    if let Ok((again, device)) = unsafe { open_endpoint(eCapture, false) } {
                        *name.lock().unwrap() = device;
                        run = again;
                        break;
                    }
                }
            }
        }));
        match rx.recv_timeout(Duration::from_secs(15)) {
            Ok(result) => result,
            Err(_) => Err("The microphone didn't start".into()),
        }
    }

    fn stop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub enum Target {
    /// Every app but this one (or, where per-process loopback isn't available, everything playing).
    AllApps,
    /// One app's process tree, with its name.
    Process(u32, String),
}

/// Other apps' audio, as wire-format audio.
pub struct LoopbackCapture {
    target: Target,
    stop: Arc<AtomicBool>,
    thread: Option<JoinHandle<()>>,
}

impl LoopbackCapture {
    pub fn new(target: Target) -> LoopbackCapture {
        LoopbackCapture { target, stop: Arc::new(AtomicBool::new(false)), thread: None }
    }
}

unsafe fn open_loopback(target: &Target) -> Result<Running, String> {
    if process_loopback_supported() {
        let attempt = match target {
            Target::AllApps => open_process_loopback(std::process::id(), true),
            Target::Process(pid, _) => open_process_loopback(*pid, false),
        };
        match (attempt, target) {
            (Ok(run), _) => return Ok(run),
            (Err(e), Target::Process(..)) => return Err(e),
            (Err(e), Target::AllApps) => eprintln!("[audio] per-process loopback failed ({e}); recording the whole output instead"),
        }
    } else if let Target::Process(..) = target {
        return Err("Recording one app's audio needs Windows 10 version 2004 or later".into());
    }
    open_endpoint(eRender, true).map(|(run, _)| run)
}

impl AudioSource for LoopbackCapture {
    fn name(&self) -> String {
        match &self.target {
            Target::AllApps => "All apps".into(),
            Target::Process(_, name) => name.clone(),
        }
    }

    fn start(&mut self, sink: Sink) -> Result<(), String> {
        let stop = self.stop.clone();
        let target = self.target.clone();
        let (tx, rx) = mpsc::channel::<Result<(), String>>();
        self.thread = Some(thread::spawn(move || {
            com_init();
            let run = match unsafe { open_loopback(&target) } {
                Ok(run) => run,
                Err(e) => {
                    let _ = tx.send(Err(e));
                    return;
                }
            };
            let _ = tx.send(Ok(()));
            let mut run = run;
            while !stop.load(Ordering::Relaxed) {
                if let Err(e) = pump(&run, &sink, &stop) {
                    eprintln!("[audio] meeting audio stopped: {e}; starting it again");
                }
                drop(run);
                // The output device changed (headphones plugged in): start again on the new one.
                loop {
                    if stop.load(Ordering::Relaxed) {
                        return;
                    }
                    thread::sleep(Duration::from_secs(1));
                    if let Ok(again) = unsafe { open_loopback(&target) } {
                        run = again;
                        break;
                    }
                }
            }
        }));
        match rx.recv_timeout(Duration::from_secs(15)) {
            Ok(result) => result,
            Err(_) => Err("Listening to other apps didn't start".into()),
        }
    }

    fn stop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}
