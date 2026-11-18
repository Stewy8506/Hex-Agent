#![cfg(windows)]

use crate::event::AppEvent;
use crossterm::event::{
    KeyCode, KeyEvent, KeyEventKind, KeyEventState, KeyModifiers, MediaKeyCode, ModifierKeyCode,
    MouseButton, MouseEvent, MouseEventKind,
};
use std::collections::HashSet;
use std::io;
use std::os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle};
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::ptr::{null, null_mut};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};
use termwiz::input::{InputEvent, InputParser, KeyCode as WizKeyCode, Modifiers, MouseButtons};
use tokio::sync::mpsc::{error::TrySendError, Sender};
use windows_sys::Win32::Foundation::{
    GENERIC_READ, GENERIC_WRITE, HANDLE, INVALID_HANDLE_VALUE, WAIT_FAILED, WAIT_OBJECT_0,
    WAIT_TIMEOUT,
};
use windows_sys::Win32::Storage::FileSystem::{
    CreateFileW, FILE_SHARE_READ, FILE_SHARE_WRITE, OPEN_EXISTING,
};
use windows_sys::Win32::System::Console::*;
use windows_sys::Win32::System::Threading::{
    CreateEventW, SetEvent, WaitForMultipleObjects, WaitForSingleObject, INFINITE,
};
use windows_sys::Win32::UI::Input::KeyboardAndMouse::*;

const PASTE_START: &[u8] = b"\x1b[200~";
const PASTE_END: &[u8] = b"\x1b[201~";
const MAX_PASTE_BYTES: usize = 8 * 1024 * 1024;
const MAX_VT_SEQUENCE_BYTES: usize = 4096;
const ESCAPE_IDLE: Duration = Duration::from_millis(50);
const SEND_RETRY_MS: u32 = 10;
static INPUT_ACTIVE: AtomicBool = AtomicBool::new(false);

pub struct WindowsInput {
    stop: Arc<OwnedHandle>,
    stopping: Arc<AtomicBool>,
    worker: Option<JoinHandle<()>>,
}

impl WindowsInput {
    // VT framing requires sole ownership of CONIN$; do not run EventStream alongside this reader.
    pub fn spawn(tx: Sender<AppEvent>) -> io::Result<Self> {
        let lease = InputLease::acquire()?;
        let input = open_console("CONIN$", GENERIC_READ | GENERIC_WRITE)?;
        let output = open_console("CONOUT$", GENERIC_READ | GENERIC_WRITE)?;
        let stop = Arc::new(create_stop_event()?);
        let stopping = Arc::new(AtomicBool::new(false));
        let mut original = 0;
        if unsafe { GetConsoleMode(input.as_raw_handle(), &mut original) } == 0 {
            return Err(console_error("GetConsoleMode"));
        }
        if unsafe {
            SetConsoleMode(
                input.as_raw_handle(),
                original | ENABLE_VIRTUAL_TERMINAL_INPUT,
            )
        } == 0
        {
            return Err(console_error(
                "SetConsoleMode(ENABLE_VIRTUAL_TERMINAL_INPUT)",
            ));
        }
        let mut console = Console {
            input,
            output,
            introduced_vt: original & ENABLE_VIRTUAL_TERMINAL_INPUT == 0,
            introduced_output_vt: false,
            win32_input: false,
            _lease: lease,
        };
        let mut output_mode = 0;
        if unsafe { GetConsoleMode(console.output.as_raw_handle(), &mut output_mode) } == 0 {
            return Err(console_error("GetConsoleMode(output)"));
        }
        if output_mode & ENABLE_VIRTUAL_TERMINAL_PROCESSING == 0 {
            if unsafe {
                SetConsoleMode(
                    console.output.as_raw_handle(),
                    output_mode | ENABLE_VIRTUAL_TERMINAL_PROCESSING,
                )
            } == 0
            {
                return Err(console_error("SetConsoleMode(output VT)"));
            }
            console.introduced_output_vt = true;
        }
        set_win32_input(console.output.as_raw_handle(), true)?;
        console.win32_input = true;
        let thread_stop = Arc::clone(&stop);
        let thread_stopping = Arc::clone(&stopping);
        let worker = thread::Builder::new()
            .name("hex-windows-input".into())
            .spawn(move || {
                let result = catch_unwind(AssertUnwindSafe(|| {
                    read_input(&console, &thread_stop, &thread_stopping, &tx)
                }));
                let error = match result {
                    Ok(Ok(())) => None,
                    Ok(Err(error)) => Some(error.to_string()),
                    Err(_) => Some("Windows input thread panicked".into()),
                };
                drop(console);
                if let Some(error) = error {
                    let _ = send_event(
                        &tx,
                        AppEvent::TerminalError(error),
                        &thread_stop,
                        &thread_stopping,
                    );
                }
            })?;
        Ok(Self {
            stop,
            stopping,
            worker: Some(worker),
        })
    }
}

impl Drop for WindowsInput {
    fn drop(&mut self) {
        self.stopping.store(true, Ordering::Release);
        unsafe { SetEvent(self.stop.as_raw_handle()) };
        if let Some(worker) = self.worker.take() {
            let _ = worker.join();
        }
    }
}

struct InputLease;

impl InputLease {
    fn acquire() -> io::Result<Self> {
        INPUT_ACTIVE
            .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
            .map_err(|_| {
                io::Error::new(
                    io::ErrorKind::AlreadyExists,
                    "Windows input is already running",
                )
            })?;
        Ok(Self)
    }
}

impl Drop for InputLease {
    fn drop(&mut self) {
        INPUT_ACTIVE.store(false, Ordering::Release);
    }
}

struct Console {
    input: OwnedHandle,
    output: OwnedHandle,
    introduced_vt: bool,
    introduced_output_vt: bool,
    win32_input: bool,
    _lease: InputLease,
}

impl Console {
    fn viewport(&self) -> io::Result<Viewport> {
        let mut info = CONSOLE_SCREEN_BUFFER_INFO::default();
        if unsafe { GetConsoleScreenBufferInfo(self.output.as_raw_handle(), &mut info) } == 0 {
            return Err(console_error("GetConsoleScreenBufferInfo"));
        }
        Viewport::from_rect(info.srWindow)
    }
}

fn cleanup_mode(current: u32, introduced_vt: bool) -> u32 {
    if introduced_vt {
        current & !ENABLE_VIRTUAL_TERMINAL_INPUT
    } else {
        current
    }
}

fn set_win32_input(output: HANDLE, enabled: bool) -> io::Result<()> {
    let sequence: Vec<u16> = if enabled {
        "\x1b[?9001h"
    } else {
        "\x1b[?9001l"
    }
    .encode_utf16()
    .collect();
    let mut written = 0;
    if unsafe {
        WriteConsoleW(
            output,
            sequence.as_ptr(),
            sequence.len() as u32,
            &mut written,
            null(),
        )
    } == 0
    {
        return Err(console_error("WriteConsoleW(win32 input mode)"));
    }
    if written != sequence.len() as u32 {
        return Err(io::Error::new(
            io::ErrorKind::WriteZero,
            "Incomplete win32 input mode request",
        ));
    }
    Ok(())
}

impl Drop for Console {
    fn drop(&mut self) {
        if self.win32_input {
            let _ = set_win32_input(self.output.as_raw_handle(), false);
        }
        if self.introduced_output_vt {
            let mut current = 0;
            if unsafe { GetConsoleMode(self.output.as_raw_handle(), &mut current) } != 0 {
                unsafe {
                    SetConsoleMode(
                        self.output.as_raw_handle(),
                        current & !ENABLE_VIRTUAL_TERMINAL_PROCESSING,
                    )
                };
            }
        }
        // A panic hook may already have restored raw mode, so only remove our own VT bit.
        if self.introduced_vt {
            let mut current = 0;
            if unsafe { GetConsoleMode(self.input.as_raw_handle(), &mut current) } != 0 {
                let restored = cleanup_mode(current, self.introduced_vt);
                if restored != current {
                    unsafe { SetConsoleMode(self.input.as_raw_handle(), restored) };
                }
            }
        }
    }
}

fn open_console(name: &str, access: u32) -> io::Result<OwnedHandle> {
    let name: Vec<u16> = name.encode_utf16().chain(Some(0)).collect();
    let handle = unsafe {
        CreateFileW(
            name.as_ptr(),
            access,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            null(),
            OPEN_EXISTING,
            0,
            null_mut(),
        )
    };
    if handle.is_null() || handle == INVALID_HANDLE_VALUE {
        return Err(console_error("CreateFileW(console)"));
    }
    Ok(unsafe { OwnedHandle::from_raw_handle(handle) })
}

fn create_stop_event() -> io::Result<OwnedHandle> {
    let handle = unsafe { CreateEventW(null(), 1, 0, null()) };
    if handle.is_null() {
        return Err(console_error("CreateEventW"));
    }
    Ok(unsafe { OwnedHandle::from_raw_handle(handle) })
}

fn console_error(operation: &str) -> io::Error {
    let error = io::Error::last_os_error();
    io::Error::new(error.kind(), format!("{operation}: {error}"))
}

fn send_event(
    tx: &Sender<AppEvent>,
    mut event: AppEvent,
    stop: &OwnedHandle,
    stopping: &AtomicBool,
) -> bool {
    // Blocking on a full Tokio channel would prevent Drop from joining the reader.
    loop {
        if stopping.load(Ordering::Acquire) {
            return false;
        }
        match tx.try_send(event) {
            Ok(()) => return true,
            Err(TrySendError::Closed(_)) => return false,
            Err(TrySendError::Full(pending)) => event = pending,
        }
        if unsafe { WaitForSingleObject(stop.as_raw_handle(), SEND_RETRY_MS) } != WAIT_TIMEOUT {
            return false;
        }
    }
}

fn read_input(
    console: &Console,
    stop: &OwnedHandle,
    stopping: &AtomicBool,
    tx: &Sender<AppEvent>,
) -> io::Result<()> {
    let handles: [HANDLE; 2] = [stop.as_raw_handle(), console.input.as_raw_handle()];
    let mut records = [INPUT_RECORD::default(); 128];
    let mut decoder = Decoder::default();
    let mut events = Vec::new();
    loop {
        if stopping.load(Ordering::Acquire) || tx.is_closed() {
            return Ok(());
        }
        let wait = unsafe {
            WaitForMultipleObjects(
                handles.len() as u32,
                handles.as_ptr(),
                0,
                decoder.wait_timeout(Instant::now()),
            )
        };
        if wait == WAIT_OBJECT_0 {
            return Ok(());
        }
        if wait == WAIT_TIMEOUT {
            decoder.idle(Instant::now(), &mut events);
        } else if wait == WAIT_OBJECT_0 + 1 {
            if stopping.load(Ordering::Acquire) {
                return Ok(());
            }
            let mut count = 0;
            if unsafe {
                ReadConsoleInputW(
                    console.input.as_raw_handle(),
                    records.as_mut_ptr(),
                    records.len() as u32,
                    &mut count,
                )
            } == 0
            {
                return Err(console_error("ReadConsoleInputW"));
            }
            if count == 0 {
                return Err(io::Error::new(
                    io::ErrorKind::UnexpectedEof,
                    "CONIN$ returned no records",
                ));
            }
            for record in &records[..count as usize] {
                if stopping.load(Ordering::Acquire) {
                    return Ok(());
                }
                match u32::from(record.EventType) {
                    KEY_EVENT => decoder.key(
                        unsafe { record.Event.KeyEvent },
                        Instant::now(),
                        &mut events,
                    ),
                    MOUSE_EVENT => decoder.native_mouse(
                        unsafe { record.Event.MouseEvent },
                        console.viewport()?,
                        &mut events,
                    ),
                    WINDOW_BUFFER_SIZE_EVENT => {
                        let viewport = console.viewport()?;
                        events.push(AppEvent::Resize(viewport.cols, viewport.rows));
                    }
                    _ => {}
                }
                for event in events.drain(..) {
                    if !send_event(tx, event, stop, stopping) {
                        return Ok(());
                    }
                }
            }
        } else if wait == WAIT_FAILED {
            return Err(console_error("WaitForMultipleObjects"));
        } else {
            return Err(io::Error::other(format!(
                "Unexpected console wait result: {wait}"
            )));
        }
        for event in events.drain(..) {
            if !send_event(tx, event, stop, stopping) {
                return Ok(());
            }
        }
    }
}

#[derive(Clone, Copy)]
struct Viewport {
    left: i16,
    top: i16,
    cols: u16,
    rows: u16,
}

impl Viewport {
    fn from_rect(rect: SMALL_RECT) -> io::Result<Self> {
        let cols = i32::from(rect.Right) - i32::from(rect.Left) + 1;
        let rows = i32::from(rect.Bottom) - i32::from(rect.Top) + 1;
        if cols <= 0 || rows <= 0 || cols > u16::MAX as i32 || rows > u16::MAX as i32 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "Invalid console viewport",
            ));
        }
        Ok(Self {
            left: rect.Left,
            top: rect.Top,
            cols: cols as u16,
            rows: rows as u16,
        })
    }

    fn relative(&self, position: COORD) -> Option<(u16, u16)> {
        let column = u16::try_from(i32::from(position.X) - i32::from(self.left)).ok()?;
        let row = u16::try_from(i32::from(position.Y) - i32::from(self.top)).ok()?;
        (column < self.cols && row < self.rows).then_some((column, row))
    }
}

#[derive(Default)]
struct Utf16<T: Copy> {
    high: Option<(u16, T)>,
}

impl<T: Copy> Utf16<T> {
    fn push(&mut self, unit: u16, metadata: T) -> [Option<(char, T)>; 2] {
        let mut replacement = None;
        if let Some((high, saved)) = self.high.take() {
            if (0xdc00..=0xdfff).contains(&unit) {
                let scalar =
                    0x10000 + ((u32::from(high) - 0xd800) << 10) + u32::from(unit) - 0xdc00;
                return [
                    Some((char::from_u32(scalar).unwrap_or('\u{fffd}'), saved)),
                    None,
                ];
            }
            replacement = Some(('\u{fffd}', saved));
        }
        let current = match unit {
            0xd800..=0xdbff => {
                self.high = Some((unit, metadata));
                None
            }
            0xdc00..=0xdfff => Some(('\u{fffd}', metadata)),
            _ => Some((
                char::from_u32(u32::from(unit)).unwrap_or('\u{fffd}'),
                metadata,
            )),
        };
        [replacement, current]
    }
}

struct PasteFrame {
    payload: Vec<u8>,
    tail: Vec<u8>,
    discarded: bool,
    limit: usize,
}

impl PasteFrame {
    fn new(limit: usize) -> Self {
        Self {
            payload: Vec::new(),
            tail: Vec::with_capacity(PASTE_END.len()),
            discarded: false,
            limit,
        }
    }

    fn push(&mut self, byte: u8, events: &mut Vec<AppEvent>) -> bool {
        self.tail.push(byte);
        while !PASTE_END.starts_with(&self.tail) {
            let byte = self.tail.remove(0);
            if !self.discarded {
                if self.payload.len() == self.limit {
                    self.discarded = true;
                    self.payload = Vec::new();
                    events.push(AppEvent::Notice(format!(
                        "Bracketed paste exceeds {} bytes; discarding it through its closing delimiter",
                        self.limit,
                    )));
                } else {
                    self.payload.push(byte);
                }
            }
        }
        self.tail == PASTE_END
    }
}

#[derive(Default)]
struct Decoder {
    parser: InputParser,
    wire_utf16: Utf16<u16>,
    wire_sequence: Vec<u8>,
    wire_deadline: Option<Instant>,
    wire_discard: bool,
    vt_utf16: Utf16<u16>,
    native_utf16: [Utf16<KEY_EVENT_RECORD>; 2],
    sequence: Vec<u8>,
    paste: Option<PasteFrame>,
    escape_deadline: Option<Instant>,
    discard_sequence: bool,
    held: HashSet<(u16, u16)>,
    suppressed: HashSet<(u16, u16)>,
    native_buttons: u32,
    vt_buttons: u32,
}

impl Decoder {
    fn wait_timeout(&self, now: Instant) -> u32 {
        self.escape_deadline
            .into_iter()
            .chain(self.wire_deadline)
            .min()
            .map_or(INFINITE, |deadline| {
                let remaining = deadline.saturating_duration_since(now);
                remaining
                    .as_millis()
                    .saturating_add(u128::from(remaining.subsec_nanos() % 1_000_000 != 0))
                    .min(u128::from(INFINITE - 1)) as u32
            })
    }

    fn idle(&mut self, now: Instant, events: &mut Vec<AppEvent>) {
        if self.wire_sequence == b"\x1b"
            && self.wire_deadline.is_some_and(|deadline| now >= deadline)
            && self.wire_utf16.high.is_none()
        {
            self.flush_wire(now, events);
            if self.sequence == b"\x1b" {
                self.escape_deadline = Some(now);
            }
        }
        // Only a lone ESC is ambiguous on idle; an explicit paste prefix must stay quarantined.
        if self.sequence == b"\x1b"
            && self.escape_deadline.is_some_and(|deadline| now >= deadline)
            && self.vt_utf16.high.is_none()
        {
            self.flush_sequence(events);
        }
    }

    fn key(&mut self, record: KEY_EVENT_RECORD, now: Instant, events: &mut Vec<AppEvent>) {
        let unit = unsafe { record.uChar.UnicodeChar };
        if record.wVirtualKeyCode == 0 {
            if record.bKeyDown != 0 {
                for (ch, count) in self
                    .wire_utf16
                    .push(unit, record.wRepeatCount.max(1))
                    .into_iter()
                    .flatten()
                {
                    for _ in 0..count {
                        self.wire_char(ch, now, events);
                    }
                }
                if self.wire_utf16.high.is_some() {
                    self.wire_deadline = None;
                }
            }
        } else {
            if self.wire_sequence.len() >= 2 || self.wire_discard {
                if record.bKeyDown != 0 {
                    self.suppressed
                        .insert((record.wVirtualKeyCode, record.wVirtualScanCode));
                    if let Some(unit) = paste_unit(&record, unit) {
                        for (ch, count) in self
                            .wire_utf16
                            .push(unit, record.wRepeatCount.max(1))
                            .into_iter()
                            .flatten()
                        {
                            for _ in 0..count {
                                self.wire_char(ch, now, events);
                            }
                        }
                    }
                } else {
                    let identity = (record.wVirtualKeyCode, record.wVirtualScanCode);
                    self.held.remove(&identity);
                    self.suppressed.remove(&identity);
                }
                return;
            }
            if !self.wire_sequence.is_empty() {
                self.flush_wire(now, events);
            }
            self.decoded_key(record, now, events);
        }
    }

    fn wire_char(&mut self, ch: char, now: Instant, events: &mut Vec<AppEvent>) {
        if ch == '\x1b' {
            if self.wire_sequence == b"\x1b" {
                self.flush_wire(now, events);
            } else {
                self.wire_sequence.clear();
            }
            self.wire_discard = false;
        }
        if self.wire_discard {
            if ch.is_ascii() && (0x40..=0x7e).contains(&(ch as u8)) {
                self.wire_discard = false;
            }
            return;
        }
        if self.wire_sequence.is_empty() && ch != '\x1b' {
            self.vt_char(ch, now, events);
            return;
        }
        let mut bytes = [0; 4];
        self.wire_sequence
            .extend_from_slice(ch.encode_utf8(&mut bytes).as_bytes());
        self.wire_deadline = None;
        if self.wire_sequence == b"\x1b" {
            self.wire_deadline = Some(now + ESCAPE_IDLE);
            return;
        }
        if self.wire_sequence.len() > MAX_VT_SEQUENCE_BYTES {
            self.wire_sequence.clear();
            self.wire_discard = !(ch.is_ascii() && (0x40..=0x7e).contains(&(ch as u8)));
            if let Some(paste) = &mut self.paste {
                paste.discarded = true;
                paste.payload.clear();
            }
            events.push(AppEvent::Notice(
                "Discarded an oversized Windows input sequence".into(),
            ));
            return;
        }
        if (self.wire_sequence.starts_with(b"\x1b[") || self.wire_sequence.starts_with(b"\x1bO"))
            && (self.wire_sequence.len() <= 2
                || !(ch.is_ascii() && (0x40..=0x7e).contains(&(ch as u8))))
        {
            return;
        }
        self.flush_wire(now, events);
    }

    fn flush_wire(&mut self, now: Instant, events: &mut Vec<AppEvent>) {
        let sequence = std::mem::take(&mut self.wire_sequence);
        self.wire_deadline = None;
        // Win32 packets can contain the paste delimiters themselves, so unwrap before VT framing.
        if sequence.starts_with(b"\x1b[") && sequence.last() == Some(&b'_') {
            if let Some(record) = win32_record(&sequence) {
                self.decoded_key(record, now, events);
            } else {
                events.push(AppEvent::Notice(
                    "Discarded a malformed win32 input packet".into(),
                ));
            }
        } else if let Ok(text) = std::str::from_utf8(&sequence) {
            for ch in text.chars() {
                self.vt_char(ch, now, events);
            }
        }
    }

    fn decoded_key(&mut self, record: KEY_EVENT_RECORD, now: Instant, events: &mut Vec<AppEvent>) {
        let unit = unsafe { record.uChar.UnicodeChar };
        let identity = (record.wVirtualKeyCode, record.wVirtualScanCode);
        if record.wVirtualKeyCode == 0 {
            if record.bKeyDown != 0 {
                self.vt_unit(unit, record.wRepeatCount.max(1), now, events);
                // Encapsulated synthetic ESC may be a paste delimiter; real Escape has a VK code.
                self.escape_deadline = None;
            }
            return;
        }
        if record.bKeyDown == 0 && self.suppressed.remove(&identity) {
            self.held.remove(&identity);
            return;
        }
        if self.paste.is_some() || self.sequence.len() >= 2 || self.discard_sequence {
            self.native_utf16[usize::from(record.bKeyDown == 0)].high = None;
            if record.bKeyDown != 0 {
                self.suppressed.insert(identity);
                if let Some(unit) = paste_unit(&record, unit) {
                    self.vt_unit(unit, record.wRepeatCount.max(1), now, events);
                }
            } else {
                self.held.remove(&identity);
            }
            return;
        }
        if record.bKeyDown != 0 {
            self.suppressed.remove(&identity);
        }
        if !self.sequence.is_empty() {
            self.flush_sequence(events);
        }
        let index = usize::from(record.bKeyDown == 0);
        if let Some(code) = native_special(&record) {
            if let Some((_, saved)) = self.native_utf16[index].high.take() {
                self.emit_native(saved, KeyCode::Char('\u{fffd}'), events);
            }
            self.emit_native(record, code, events);
        } else if unit >= 0x20 || record.wVirtualKeyCode == VK_PACKET {
            for (ch, metadata) in self.native_utf16[index]
                .push(unit, record)
                .into_iter()
                .flatten()
            {
                self.emit_native(
                    metadata,
                    if ch == '\0' {
                        KeyCode::Null
                    } else {
                        KeyCode::Char(ch)
                    },
                    events,
                );
            }
        } else {
            if let Some((_, saved)) = self.native_utf16[index].high.take() {
                self.emit_native(saved, KeyCode::Char('\u{fffd}'), events);
            }
            if let Some(ch) = native_control_char(&record, unit) {
                self.emit_native(record, KeyCode::Char(ch), events);
            }
        }
    }

    fn emit_native(&mut self, record: KEY_EVENT_RECORD, code: KeyCode, events: &mut Vec<AppEvent>) {
        let identity = (record.wVirtualKeyCode, record.wVirtualScanCode);
        let kind = if record.bKeyDown == 0 {
            self.held.remove(&identity);
            KeyEventKind::Release
        } else if self.held.insert(identity) {
            KeyEventKind::Press
        } else {
            KeyEventKind::Repeat
        };
        let modifiers = native_modifiers(record.dwControlKeyState);
        let state = native_state(&record);
        let count = if kind == KeyEventKind::Release {
            1
        } else {
            record.wRepeatCount.max(1)
        };
        for index in 0..count {
            events.push(AppEvent::Key(KeyEvent::new_with_kind_and_state(
                code,
                modifiers,
                if index == 0 {
                    kind
                } else {
                    KeyEventKind::Repeat
                },
                state,
            )));
        }
    }

    fn vt_unit(&mut self, unit: u16, count: u16, now: Instant, events: &mut Vec<AppEvent>) {
        for (ch, repeat) in self.vt_utf16.push(unit, count).into_iter().flatten() {
            for _ in 0..repeat {
                self.vt_char(ch, now, events);
            }
        }
        if self.vt_utf16.high.is_some() {
            self.escape_deadline = None;
        }
    }

    fn vt_char(&mut self, ch: char, now: Instant, events: &mut Vec<AppEvent>) {
        let mut utf8 = [0; 4];
        let bytes = ch.encode_utf8(&mut utf8).as_bytes();
        if self.paste.is_some() {
            for &byte in bytes {
                let closed = self.paste.as_mut().unwrap().push(byte, events);
                if closed {
                    let frame = self.paste.take().unwrap();
                    if !frame.discarded {
                        // Termwiz's paste buffer is unbounded, so feed it only a complete, capped frame.
                        let mut parser = InputParser::new();
                        parser.parse(PASTE_START, |_| {}, true);
                        parser.parse(&frame.payload, |_| {}, true);
                        parser.parse(
                            PASTE_END,
                            |event| self.map_event(event, None, events),
                            false,
                        );
                    }
                }
            }
            return;
        }
        // A fresh ESC must still start a paste marker after an interrupted VT sequence.
        if ch == '\x1b' {
            if self.sequence == b"\x1b" {
                self.flush_sequence(events);
            } else {
                self.sequence.clear();
            }
            self.discard_sequence = false;
        }
        if self.discard_sequence {
            if ch.is_ascii() && (0x40..=0x7e).contains(&(ch as u8)) {
                self.discard_sequence = false;
            }
            return;
        }
        if self.sequence.is_empty() && ch != '\x1b' {
            self.parse(bytes, None, events);
            return;
        }
        self.sequence.extend_from_slice(bytes);
        self.escape_deadline = None;
        if PASTE_START.starts_with(&self.sequence) {
            if self.sequence == PASTE_START {
                self.sequence.clear();
                self.paste = Some(PasteFrame::new(MAX_PASTE_BYTES));
            } else if self.sequence.len() == 1 {
                self.escape_deadline = Some(now + ESCAPE_IDLE);
            }
            return;
        }
        if self.sequence.len() > MAX_VT_SEQUENCE_BYTES {
            self.sequence.clear();
            self.discard_sequence = !(ch.is_ascii() && (0x40..=0x7e).contains(&(ch as u8)));
            events.push(AppEvent::Notice(
                "Discarded an oversized VT input sequence".into(),
            ));
            return;
        }
        if (self.sequence.starts_with(b"\x1b[") || self.sequence.starts_with(b"\x1bO"))
            && (self.sequence.len() <= 2 || !(ch.is_ascii() && (0x40..=0x7e).contains(&(ch as u8))))
        {
            return;
        }
        self.flush_sequence(events);
    }

    fn flush_sequence(&mut self, events: &mut Vec<AppEvent>) {
        let sequence = std::mem::take(&mut self.sequence);
        self.escape_deadline = None;
        self.parse(&sequence, sgr_mouse_kind(&sequence), events);
    }

    fn parse(
        &mut self,
        bytes: &[u8],
        mouse_kind: Option<MouseEventKind>,
        events: &mut Vec<AppEvent>,
    ) {
        let mut parsed = Vec::new();
        self.parser.parse(bytes, |event| parsed.push(event), false);
        for event in parsed {
            self.map_event(event, mouse_kind, events);
        }
    }

    fn map_event(
        &mut self,
        event: InputEvent,
        mouse_kind: Option<MouseEventKind>,
        events: &mut Vec<AppEvent>,
    ) {
        match event {
            InputEvent::Key(key) => {
                let modifiers = wiz_modifiers(key.modifiers);
                if let Some(code) = wiz_key_code(key.key, modifiers) {
                    events.push(AppEvent::Key(KeyEvent::new(code, modifiers)));
                }
            }
            InputEvent::Paste(text) => events.push(AppEvent::Paste(text)),
            InputEvent::Mouse(mouse) => {
                let mut buttons = 0;
                for (wiz, native, _) in button_map() {
                    if mouse.mouse_buttons.contains(wiz) {
                        buttons |= native;
                    }
                }
                let modifiers = wiz_modifiers(mouse.modifiers);
                let column = mouse.x.saturating_sub(1);
                let row = mouse.y.saturating_sub(1);
                let kind = mouse_kind.or_else(|| wiz_wheel(&mouse.mouse_buttons));
                if let Some(kind) = kind {
                    match kind {
                        MouseEventKind::Down(button) | MouseEventKind::Drag(button) => {
                            self.vt_buttons |= button_bit(button);
                        }
                        MouseEventKind::Up(button) => self.vt_buttons &= !button_bit(button),
                        MouseEventKind::Moved => self.vt_buttons = 0,
                        _ => {}
                    }
                    events.push(AppEvent::Mouse(MouseEvent {
                        kind,
                        column,
                        row,
                        modifiers,
                    }));
                } else {
                    emit_button_changes(self.vt_buttons, buttons, column, row, modifiers, events);
                    if self.vt_buttons == buttons {
                        events.push(AppEvent::Mouse(MouseEvent {
                            kind: drag_or_move(buttons),
                            column,
                            row,
                            modifiers,
                        }));
                    }
                    self.vt_buttons = buttons;
                }
            }
            InputEvent::Resized { cols, rows } => {
                if let (Ok(cols), Ok(rows)) = (u16::try_from(cols), u16::try_from(rows)) {
                    events.push(AppEvent::Resize(cols, rows));
                }
            }
            InputEvent::PixelMouse(_) | InputEvent::Wake => {}
        }
    }

    fn native_mouse(
        &mut self,
        record: MOUSE_EVENT_RECORD,
        viewport: Viewport,
        events: &mut Vec<AppEvent>,
    ) {
        let previous = self.native_buttons;
        self.native_buttons = record.dwButtonState & 0xffff;
        let Some((column, row)) = viewport.relative(record.dwMousePosition) else {
            return;
        };
        let modifiers = native_modifiers(record.dwControlKeyState);
        let wheel = (record.dwButtonState >> 16) as u16 as i16;
        let kind = if record.dwEventFlags & MOUSE_WHEELED != 0 {
            match wheel.cmp(&0) {
                std::cmp::Ordering::Greater => Some(MouseEventKind::ScrollUp),
                std::cmp::Ordering::Less => Some(MouseEventKind::ScrollDown),
                _ => None,
            }
        } else if record.dwEventFlags & MOUSE_HWHEELED != 0 {
            match wheel.cmp(&0) {
                std::cmp::Ordering::Greater => Some(MouseEventKind::ScrollRight),
                std::cmp::Ordering::Less => Some(MouseEventKind::ScrollLeft),
                _ => None,
            }
        } else if record.dwEventFlags & MOUSE_MOVED != 0 {
            Some(drag_or_move(self.native_buttons))
        } else {
            emit_button_changes(
                previous,
                self.native_buttons,
                column,
                row,
                modifiers,
                events,
            );
            if record.dwEventFlags & DOUBLE_CLICK != 0 && previous == self.native_buttons {
                for (_, bit, button) in button_map() {
                    if self.native_buttons & bit != 0 {
                        events.push(AppEvent::Mouse(MouseEvent {
                            kind: MouseEventKind::Down(button),
                            column,
                            row,
                            modifiers,
                        }));
                    }
                }
            }
            None
        };
        if let Some(kind) = kind {
            events.push(AppEvent::Mouse(MouseEvent {
                kind,
                column,
                row,
                modifiers,
            }));
        }
    }
}

fn win32_record(sequence: &[u8]) -> Option<KEY_EVENT_RECORD> {
    let text = std::str::from_utf8(sequence.strip_prefix(b"\x1b[")?.strip_suffix(b"_")?).ok()?;
    let mut fields = text.split(';');
    let vk = fields.next()?.parse::<u16>().ok()?;
    let scan = fields.next()?.parse::<u16>().ok()?;
    let unit = fields.next()?.parse::<u16>().ok()?;
    let down = fields.next()?.parse::<u8>().ok()?;
    let modifiers = fields.next()?.parse::<u32>().ok()?;
    let repeat = fields.next()?.parse::<u16>().ok()?;
    if down > 1 || fields.next().is_some() {
        return None;
    }
    Some(KEY_EVENT_RECORD {
        bKeyDown: i32::from(down),
        wRepeatCount: repeat,
        wVirtualKeyCode: vk,
        wVirtualScanCode: scan,
        uChar: KEY_EVENT_RECORD_0 { UnicodeChar: unit },
        dwControlKeyState: modifiers,
    })
}

fn native_modifiers(state: u32) -> KeyModifiers {
    let mut modifiers = KeyModifiers::NONE;
    if state & SHIFT_PRESSED != 0 {
        modifiers |= KeyModifiers::SHIFT;
    }
    if state & (LEFT_CTRL_PRESSED | RIGHT_CTRL_PRESSED) != 0 {
        modifiers |= KeyModifiers::CONTROL;
    }
    if state & (LEFT_ALT_PRESSED | RIGHT_ALT_PRESSED) != 0 {
        modifiers |= KeyModifiers::ALT;
    }
    modifiers
}

fn native_state(record: &KEY_EVENT_RECORD) -> KeyEventState {
    let mut state = KeyEventState::NONE;
    if record.dwControlKeyState & CAPSLOCK_ON != 0 {
        state |= KeyEventState::CAPS_LOCK;
    }
    if record.dwControlKeyState & NUMLOCK_ON != 0 {
        state |= KeyEventState::NUM_LOCK;
    }
    let vk = record.wVirtualKeyCode;
    let enhanced = record.dwControlKeyState & ENHANCED_KEY != 0;
    if (VK_NUMPAD0..=VK_DIVIDE).contains(&vk)
        || (vk == VK_RETURN && enhanced)
        || (!enhanced
            && matches!(
                vk,
                VK_INSERT
                    | VK_DELETE
                    | VK_HOME
                    | VK_END
                    | VK_PRIOR
                    | VK_NEXT
                    | VK_LEFT
                    | VK_RIGHT
                    | VK_UP
                    | VK_DOWN
                    | VK_CLEAR
            ))
    {
        state |= KeyEventState::KEYPAD;
    }
    state
}

fn native_special(record: &KEY_EVENT_RECORD) -> Option<KeyCode> {
    let right = record.dwControlKeyState & ENHANCED_KEY != 0;
    Some(match record.wVirtualKeyCode {
        VK_RETURN => KeyCode::Enter,
        VK_BACK => KeyCode::Backspace,
        VK_ESCAPE => KeyCode::Esc,
        VK_TAB if record.dwControlKeyState & SHIFT_PRESSED != 0 => KeyCode::BackTab,
        VK_TAB => KeyCode::Tab,
        VK_LEFT => KeyCode::Left,
        VK_RIGHT => KeyCode::Right,
        VK_UP => KeyCode::Up,
        VK_DOWN => KeyCode::Down,
        VK_HOME => KeyCode::Home,
        VK_END => KeyCode::End,
        VK_PRIOR => KeyCode::PageUp,
        VK_NEXT => KeyCode::PageDown,
        VK_INSERT => KeyCode::Insert,
        VK_DELETE => KeyCode::Delete,
        VK_CLEAR => KeyCode::KeypadBegin,
        VK_F1..=VK_F24 => KeyCode::F((record.wVirtualKeyCode - VK_F1 + 1) as u8),
        VK_CAPITAL => KeyCode::CapsLock,
        VK_NUMLOCK => KeyCode::NumLock,
        VK_SCROLL => KeyCode::ScrollLock,
        VK_PAUSE => KeyCode::Pause,
        VK_SNAPSHOT => KeyCode::PrintScreen,
        VK_APPS => KeyCode::Menu,
        VK_LWIN => KeyCode::Modifier(ModifierKeyCode::LeftSuper),
        VK_RWIN => KeyCode::Modifier(ModifierKeyCode::RightSuper),
        VK_LSHIFT => KeyCode::Modifier(ModifierKeyCode::LeftShift),
        VK_RSHIFT => KeyCode::Modifier(ModifierKeyCode::RightShift),
        VK_SHIFT => KeyCode::Modifier(if record.wVirtualScanCode == 0x36 {
            ModifierKeyCode::RightShift
        } else {
            ModifierKeyCode::LeftShift
        }),
        VK_LCONTROL => KeyCode::Modifier(ModifierKeyCode::LeftControl),
        VK_RCONTROL => KeyCode::Modifier(ModifierKeyCode::RightControl),
        VK_CONTROL => KeyCode::Modifier(if right {
            ModifierKeyCode::RightControl
        } else {
            ModifierKeyCode::LeftControl
        }),
        VK_MENU if record.bKeyDown == 0 && unsafe { record.uChar.UnicodeChar } != 0 => return None,
        VK_LMENU => KeyCode::Modifier(ModifierKeyCode::LeftAlt),
        VK_RMENU => KeyCode::Modifier(ModifierKeyCode::RightAlt),
        VK_MENU => KeyCode::Modifier(if right {
            ModifierKeyCode::RightAlt
        } else {
            ModifierKeyCode::LeftAlt
        }),
        VK_VOLUME_MUTE => KeyCode::Media(MediaKeyCode::MuteVolume),
        VK_VOLUME_DOWN => KeyCode::Media(MediaKeyCode::LowerVolume),
        VK_VOLUME_UP => KeyCode::Media(MediaKeyCode::RaiseVolume),
        VK_MEDIA_NEXT_TRACK => KeyCode::Media(MediaKeyCode::TrackNext),
        VK_MEDIA_PREV_TRACK => KeyCode::Media(MediaKeyCode::TrackPrevious),
        VK_MEDIA_STOP => KeyCode::Media(MediaKeyCode::Stop),
        VK_MEDIA_PLAY_PAUSE => KeyCode::Media(MediaKeyCode::PlayPause),
        _ => return None,
    })
}

fn native_control_char(record: &KEY_EVENT_RECORD, unit: u16) -> Option<char> {
    let uppercase = (record.dwControlKeyState & SHIFT_PRESSED != 0)
        ^ (record.dwControlKeyState & CAPSLOCK_ON != 0);
    if (1..=26).contains(&unit) {
        let ch = (b'a' + unit as u8 - 1) as char;
        return Some(if uppercase {
            ch.to_ascii_uppercase()
        } else {
            ch
        });
    }
    if (0x1c..=0x1f).contains(&unit) {
        return char::from_u32(u32::from(unit) + 0x40);
    }
    let mut state = [0u8; 256];
    state[VK_SHIFT as usize] = if record.dwControlKeyState & SHIFT_PRESSED != 0 {
        0x80
    } else {
        0
    };
    state[VK_CAPITAL as usize] = u8::from(record.dwControlKeyState & CAPSLOCK_ON != 0);
    let mut buffer = [0u16; 8];
    let count = unsafe {
        ToUnicodeEx(
            u32::from(record.wVirtualKeyCode),
            u32::from(record.wVirtualScanCode),
            state.as_ptr(),
            buffer.as_mut_ptr(),
            buffer.len() as i32,
            4,
            GetKeyboardLayout(0),
        )
    };
    if count > 0 {
        let mut chars =
            char::decode_utf16(buffer[..(count as usize).min(buffer.len())].iter().copied());
        let ch = chars.next()?.ok()?;
        if chars.next().is_none() {
            return Some(ch);
        }
    }
    if (VK_A..=VK_Z).contains(&record.wVirtualKeyCode) {
        let ch = record.wVirtualKeyCode as u8 as char;
        return Some(if uppercase {
            ch
        } else {
            ch.to_ascii_lowercase()
        });
    }
    None
}

fn paste_unit(record: &KEY_EVENT_RECORD, unit: u16) -> Option<u16> {
    if unit != 0 {
        return Some(unit);
    }
    Some(match record.wVirtualKeyCode {
        VK_RETURN => b'\r' as u16,
        VK_TAB => b'\t' as u16,
        VK_ESCAPE => 0x1b,
        VK_BACK => 8,
        VK_PACKET => 0,
        vk if (VK_A..=VK_Z).contains(&vk)
            && native_modifiers(record.dwControlKeyState).contains(KeyModifiers::CONTROL) =>
        {
            vk & 0x1f
        }
        _ => return None,
    })
}

fn wiz_modifiers(modifiers: Modifiers) -> KeyModifiers {
    let mut result = KeyModifiers::NONE;
    if modifiers.intersects(Modifiers::SHIFT | Modifiers::LEFT_SHIFT | Modifiers::RIGHT_SHIFT) {
        result |= KeyModifiers::SHIFT;
    }
    if modifiers.intersects(Modifiers::CTRL | Modifiers::LEFT_CTRL | Modifiers::RIGHT_CTRL) {
        result |= KeyModifiers::CONTROL;
    }
    if modifiers.intersects(Modifiers::ALT | Modifiers::LEFT_ALT | Modifiers::RIGHT_ALT) {
        result |= KeyModifiers::ALT;
    }
    if modifiers.contains(Modifiers::SUPER) {
        result |= KeyModifiers::SUPER;
    }
    result
}

fn wiz_key_code(code: WizKeyCode, modifiers: KeyModifiers) -> Option<KeyCode> {
    use WizKeyCode as W;
    Some(match code {
        W::Char('\r' | '\n') | W::Enter => KeyCode::Enter,
        W::Char('\t') | W::Tab if modifiers.contains(KeyModifiers::SHIFT) => KeyCode::BackTab,
        W::Char('\t') | W::Tab => KeyCode::Tab,
        W::Char('\x1b') | W::Escape => KeyCode::Esc,
        W::Char('\x08' | '\x7f') | W::Backspace => KeyCode::Backspace,
        W::Char('\0') => KeyCode::Null,
        W::Char(ch) => KeyCode::Char(ch),
        W::LeftArrow | W::ApplicationLeftArrow => KeyCode::Left,
        W::RightArrow | W::ApplicationRightArrow => KeyCode::Right,
        W::UpArrow | W::ApplicationUpArrow => KeyCode::Up,
        W::DownArrow | W::ApplicationDownArrow => KeyCode::Down,
        W::Home | W::KeyPadHome => KeyCode::Home,
        W::End | W::KeyPadEnd => KeyCode::End,
        W::PageUp | W::KeyPadPageUp => KeyCode::PageUp,
        W::PageDown | W::KeyPadPageDown => KeyCode::PageDown,
        W::Insert => KeyCode::Insert,
        W::Delete => KeyCode::Delete,
        W::Function(n) => KeyCode::F(n),
        W::CapsLock => KeyCode::CapsLock,
        W::NumLock => KeyCode::NumLock,
        W::ScrollLock => KeyCode::ScrollLock,
        W::PrintScreen => KeyCode::PrintScreen,
        W::Pause => KeyCode::Pause,
        W::Applications => KeyCode::Menu,
        W::Clear | W::KeyPadBegin => KeyCode::KeypadBegin,
        W::Numpad0 => KeyCode::Char('0'),
        W::Numpad1 => KeyCode::Char('1'),
        W::Numpad2 => KeyCode::Char('2'),
        W::Numpad3 => KeyCode::Char('3'),
        W::Numpad4 => KeyCode::Char('4'),
        W::Numpad5 => KeyCode::Char('5'),
        W::Numpad6 => KeyCode::Char('6'),
        W::Numpad7 => KeyCode::Char('7'),
        W::Numpad8 => KeyCode::Char('8'),
        W::Numpad9 => KeyCode::Char('9'),
        W::Multiply => KeyCode::Char('*'),
        W::Add => KeyCode::Char('+'),
        W::Separator => KeyCode::Char(','),
        W::Subtract => KeyCode::Char('-'),
        W::Decimal => KeyCode::Char('.'),
        W::Divide => KeyCode::Char('/'),
        W::Shift | W::LeftShift => KeyCode::Modifier(ModifierKeyCode::LeftShift),
        W::RightShift => KeyCode::Modifier(ModifierKeyCode::RightShift),
        W::Control | W::LeftControl => KeyCode::Modifier(ModifierKeyCode::LeftControl),
        W::RightControl => KeyCode::Modifier(ModifierKeyCode::RightControl),
        W::Alt | W::LeftAlt | W::Menu | W::LeftMenu => KeyCode::Modifier(ModifierKeyCode::LeftAlt),
        W::RightAlt | W::RightMenu => KeyCode::Modifier(ModifierKeyCode::RightAlt),
        W::Super | W::LeftWindows => KeyCode::Modifier(ModifierKeyCode::LeftSuper),
        W::RightWindows => KeyCode::Modifier(ModifierKeyCode::RightSuper),
        W::Hyper => KeyCode::Modifier(ModifierKeyCode::LeftHyper),
        W::Meta => KeyCode::Modifier(ModifierKeyCode::LeftMeta),
        W::VolumeMute => KeyCode::Media(MediaKeyCode::MuteVolume),
        W::VolumeDown => KeyCode::Media(MediaKeyCode::LowerVolume),
        W::VolumeUp => KeyCode::Media(MediaKeyCode::RaiseVolume),
        W::MediaNextTrack => KeyCode::Media(MediaKeyCode::TrackNext),
        W::MediaPrevTrack => KeyCode::Media(MediaKeyCode::TrackPrevious),
        W::MediaStop => KeyCode::Media(MediaKeyCode::Stop),
        W::MediaPlayPause => KeyCode::Media(MediaKeyCode::PlayPause),
        _ => return None,
    })
}

fn button_map() -> [(MouseButtons, u32, MouseButton); 3] {
    [
        (
            MouseButtons::LEFT,
            FROM_LEFT_1ST_BUTTON_PRESSED,
            MouseButton::Left,
        ),
        (
            MouseButtons::RIGHT,
            RIGHTMOST_BUTTON_PRESSED,
            MouseButton::Right,
        ),
        (
            MouseButtons::MIDDLE,
            FROM_LEFT_2ND_BUTTON_PRESSED,
            MouseButton::Middle,
        ),
    ]
}

fn button_bit(button: MouseButton) -> u32 {
    match button {
        MouseButton::Left => FROM_LEFT_1ST_BUTTON_PRESSED,
        MouseButton::Right => RIGHTMOST_BUTTON_PRESSED,
        MouseButton::Middle => FROM_LEFT_2ND_BUTTON_PRESSED,
    }
}

fn drag_or_move(buttons: u32) -> MouseEventKind {
    for (_, bit, button) in button_map() {
        if buttons & bit != 0 {
            return MouseEventKind::Drag(button);
        }
    }
    MouseEventKind::Moved
}

fn emit_button_changes(
    previous: u32,
    current: u32,
    column: u16,
    row: u16,
    modifiers: KeyModifiers,
    events: &mut Vec<AppEvent>,
) {
    for (_, bit, button) in button_map() {
        if (previous ^ current) & bit != 0 {
            let kind = if current & bit != 0 {
                MouseEventKind::Down(button)
            } else {
                MouseEventKind::Up(button)
            };
            events.push(AppEvent::Mouse(MouseEvent {
                kind,
                column,
                row,
                modifiers,
            }));
        }
    }
}

fn wiz_wheel(buttons: &MouseButtons) -> Option<MouseEventKind> {
    let positive = buttons.contains(MouseButtons::WHEEL_POSITIVE);
    if buttons.contains(MouseButtons::VERT_WHEEL) {
        Some(if positive {
            MouseEventKind::ScrollUp
        } else {
            MouseEventKind::ScrollDown
        })
    } else if buttons.contains(MouseButtons::HORZ_WHEEL) {
        Some(if positive {
            MouseEventKind::ScrollLeft
        } else {
            MouseEventKind::ScrollRight
        })
    } else {
        None
    }
}

fn sgr_mouse_kind(sequence: &[u8]) -> Option<MouseEventKind> {
    // InputParser erases explicit SGR release/drag metadata, so preserve it before parsing.
    let body = std::str::from_utf8(sequence.strip_prefix(b"\x1b[<")?).ok()?;
    let code: u16 = body.split(';').next()?.parse().ok()?;
    let final_byte = *sequence.last()?;
    if !matches!(final_byte, b'M' | b'm') {
        return None;
    }
    let button = match code & 3 {
        0 => Some(MouseButton::Left),
        1 => Some(MouseButton::Middle),
        2 => Some(MouseButton::Right),
        _ => None,
    };
    if code & 64 != 0 {
        return Some(match code & 3 {
            0 => MouseEventKind::ScrollUp,
            1 => MouseEventKind::ScrollDown,
            2 => MouseEventKind::ScrollLeft,
            _ => MouseEventKind::ScrollRight,
        });
    }
    if final_byte == b'm' {
        return button.map(MouseEventKind::Up);
    }
    if code & 32 != 0 {
        Some(button.map_or(MouseEventKind::Moved, MouseEventKind::Drag))
    } else {
        button.map(MouseEventKind::Down)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use windows_sys::Win32::System::Console::KEY_EVENT_RECORD_0;

    fn key_record(vk: u16, unit: u16, state: u32, down: bool) -> KEY_EVENT_RECORD {
        KEY_EVENT_RECORD {
            bKeyDown: i32::from(down),
            wRepeatCount: 1,
            wVirtualKeyCode: vk,
            wVirtualScanCode: 0,
            uChar: KEY_EVENT_RECORD_0 { UnicodeChar: unit },
            dwControlKeyState: state,
        }
    }

    fn raw(decoder: &mut Decoder, text: &str, now: Instant, events: &mut Vec<AppEvent>) {
        for unit in text.encode_utf16() {
            decoder.key(key_record(0, unit, 0, true), now, events);
            decoder.key(key_record(0, unit, 0, false), now, events);
        }
    }

    #[test]
    fn fragmented_paste_and_idle_never_expose_actions() {
        let now = Instant::now();
        let mut decoder = Decoder::default();
        let mut events = Vec::new();
        for fragment in [
            "\x1b[",
            "2",
            "00",
            "~first\n",
            "世界",
            "\t\x03\x1b[A",
            "\x1b[20",
            "1",
            "~",
        ] {
            raw(&mut decoder, fragment, now, &mut events);
            decoder.idle(now + Duration::from_secs(60), &mut events);
            if fragment != "~" {
                assert!(events.is_empty());
            }
        }
        assert!(
            matches!(events.as_slice(), [AppEvent::Paste(text)] if text == "first\n世界\t\x03\x1b[A")
        );
        assert!(decoder.paste.is_none());
        assert!(decoder.sequence.is_empty());
    }

    #[test]
    fn every_utf16_split_preserves_a_single_unicode_paste() {
        let input: Vec<u16> = "\x1b[200~a\n世界\u{1f642}\x03\x1b[201~"
            .encode_utf16()
            .collect();
        let now = Instant::now();
        for split in 0..=input.len() {
            let mut decoder = Decoder::default();
            let mut events = Vec::new();
            for chunk in [&input[..split], &input[split..]] {
                for &unit in chunk {
                    decoder.key(key_record(0, unit, 0, true), now, &mut events);
                }
                if decoder.sequence != b"\x1b" && decoder.wire_sequence != b"\x1b" {
                    decoder.idle(now + Duration::from_secs(60), &mut events);
                }
            }
            assert!(
                matches!(events.as_slice(), [AppEvent::Paste(text)] if text == "a\n世界\u{1f642}\x03"),
                "split {split}: {events:?}"
            );
        }
    }

    #[test]
    fn fresh_paste_markers_survive_escape_and_interrupted_sequence_prefixes() {
        let now = Instant::now();
        for prefix in ["\x1b", "\x1b[2", "\x1b[1;"] {
            let mut decoder = Decoder::default();
            let mut events = Vec::new();
            raw(&mut decoder, prefix, now, &mut events);
            raw(&mut decoder, "\x1b[200~\n\x03\x1b[201~", now, &mut events);
            assert!(matches!(events.last(), Some(AppEvent::Paste(text)) if text == "\n\x03"));
            assert!(events
                .iter()
                .all(|event| matches!(event, AppEvent::Paste(_))
                    || matches!(event, AppEvent::Key(key) if key.code == KeyCode::Esc)));
        }
    }

    #[test]
    fn dropping_an_unterminated_paste_does_not_emit_partial_input() {
        let now = Instant::now();
        let mut decoder = Decoder::default();
        let mut events = Vec::new();
        raw(&mut decoder, "\x1b[200~\n\x03\x1b[20", now, &mut events);
        decoder.idle(now + Duration::from_secs(3600), &mut events);
        drop(decoder);
        assert!(events.is_empty());
    }

    #[test]
    fn trailing_native_shift_enter_keeps_kind_modifiers_and_state() {
        let now = Instant::now();
        let mut decoder = Decoder::default();
        let mut events = Vec::new();
        raw(
            &mut decoder,
            "\x1b[200~line\n\x03\x1b[201~",
            now,
            &mut events,
        );
        let state = SHIFT_PRESSED | CAPSLOCK_ON | NUMLOCK_ON | ENHANCED_KEY;
        let mut enter = key_record(VK_RETURN, 13, state, true);
        enter.wRepeatCount = 2;
        decoder.key(enter, now, &mut events);
        decoder.key(key_record(VK_RETURN, 13, state, false), now, &mut events);
        assert!(matches!(&events[0], AppEvent::Paste(text) if text == "line\n\x03"));
        for (event, kind) in events[1..].iter().zip([
            KeyEventKind::Press,
            KeyEventKind::Repeat,
            KeyEventKind::Release,
        ]) {
            let AppEvent::Key(key) = event else {
                panic!("not a key")
            };
            assert_eq!(key.code, KeyCode::Enter);
            assert_eq!(key.modifiers, KeyModifiers::SHIFT);
            assert_eq!(key.kind, kind);
            assert_eq!(
                key.state,
                KeyEventState::CAPS_LOCK | KeyEventState::NUM_LOCK | KeyEventState::KEYPAD
            );
        }
        assert_eq!(events.len(), 4);
    }

    #[test]
    fn interrupted_paste_swallows_native_enter_ctrl_c_and_function_keys() {
        let now = Instant::now();
        let mut decoder = Decoder::default();
        let mut events = Vec::new();
        raw(&mut decoder, "\x1b[200~unfinished", now, &mut events);
        decoder.key(
            key_record(VK_RETURN, 13, SHIFT_PRESSED, true),
            now,
            &mut events,
        );
        decoder.key(
            key_record(VK_C, 3, LEFT_CTRL_PRESSED, true),
            now,
            &mut events,
        );
        decoder.key(key_record(VK_F1, 0, 0, true), now, &mut events);
        decoder.idle(now + Duration::from_secs(3600), &mut events);
        assert!(events.is_empty());
        assert!(decoder.paste.is_some());
        raw(&mut decoder, "\x1b[201~", now, &mut events);
        decoder.key(
            key_record(VK_C, 3, LEFT_CTRL_PRESSED, false),
            now,
            &mut events,
        );
        assert!(matches!(events.as_slice(), [AppEvent::Paste(text)] if text == "unfinished\r\x03"));
        decoder.key(key_record(VK_RETURN, 13, 0, true), now, &mut events);
        assert!(
            matches!(events.last(), Some(AppEvent::Key(key)) if key.code == KeyCode::Enter && key.kind == KeyEventKind::Press)
        );
    }

    #[test]
    fn native_release_during_paste_does_not_leave_a_stuck_repeat_key() {
        let now = Instant::now();
        let mut decoder = Decoder::default();
        let mut events = Vec::new();
        decoder.key(key_record(VK_A, b'a' as u16, 0, true), now, &mut events);
        raw(&mut decoder, "\x1b[200~text", now, &mut events);
        decoder.key(key_record(VK_A, b'a' as u16, 0, false), now, &mut events);
        raw(&mut decoder, "\x1b[201~", now, &mut events);
        decoder.key(key_record(VK_A, b'a' as u16, 0, true), now, &mut events);
        assert_eq!(events.len(), 3);
        assert!(matches!(&events[1], AppEvent::Paste(text) if text == "text"));
        assert!(
            matches!(&events[2], AppEvent::Key(key) if key.code == KeyCode::Char('a') && key.kind == KeyEventKind::Press)
        );
    }

    #[test]
    fn oversized_paste_discards_until_fragmented_close_and_then_recovers() {
        let now = Instant::now();
        let mut decoder = Decoder {
            paste: Some(PasteFrame::new(4)),
            ..Decoder::default()
        };
        let mut events = Vec::new();
        raw(&mut decoder, "12345\n\x03\x1b[A\x1b[20", now, &mut events);
        decoder.idle(now + Duration::from_secs(3600), &mut events);
        assert!(matches!(events.as_slice(), [AppEvent::Notice(_)]));
        let frame = decoder.paste.as_ref().unwrap();
        assert!(frame.discarded);
        assert!(frame.payload.is_empty());
        assert!(frame.tail.len() < PASTE_END.len());
        raw(&mut decoder, "1~\r", now, &mut events);
        assert!(
            matches!(events.as_slice(), [AppEvent::Notice(_), AppEvent::Key(key)] if key.code == KeyCode::Enter)
        );
        raw(&mut decoder, "\x1b[200~ok\x1b[201~", now, &mut events);
        assert!(matches!(events.last(), Some(AppEvent::Paste(text)) if text == "ok"));
    }

    #[test]
    fn paste_limit_counts_utf8_bytes_and_excludes_closing_delimiter() {
        assert_eq!(MAX_PASTE_BYTES, 8_388_608);
        let now = Instant::now();
        for (text, accepted) in [("\u{1f642}", true), ("\u{1f642}x", false)] {
            let mut decoder = Decoder {
                paste: Some(PasteFrame::new(4)),
                ..Decoder::default()
            };
            let mut events = Vec::new();
            raw(&mut decoder, text, now, &mut events);
            raw(&mut decoder, "\x1b[201~", now, &mut events);
            assert_eq!(events.len(), 1);
            if accepted {
                assert!(matches!(&events[0], AppEvent::Paste(value) if value == text));
            } else {
                assert!(matches!(&events[0], AppEvent::Notice(_)));
            }
        }
    }

    #[test]
    fn false_closing_prefixes_and_nested_openers_stay_in_payload() {
        let now = Instant::now();
        let mut decoder = Decoder::default();
        let mut events = Vec::new();
        let text = "\x1b[20x\x1b\x1b[201x\x1b[200~\n";
        raw(&mut decoder, "\x1b[200~", now, &mut events);
        raw(&mut decoder, text, now, &mut events);
        raw(&mut decoder, "\x1b[201~", now, &mut events);
        assert!(matches!(events.as_slice(), [AppEvent::Paste(value)] if value == text));
    }

    #[test]
    fn lone_escape_uses_only_its_deadline_and_native_keys_remain_native() {
        let now = Instant::now();
        let mut decoder = Decoder::default();
        let mut events = Vec::new();
        raw(&mut decoder, "\x1b", now, &mut events);
        decoder.idle(now + ESCAPE_IDLE - Duration::from_millis(1), &mut events);
        assert!(events.is_empty());
        decoder.idle(now + ESCAPE_IDLE, &mut events);
        assert!(matches!(events.as_slice(), [AppEvent::Key(key)] if key.code == KeyCode::Esc));
        raw(&mut decoder, "\x1b", now, &mut events);
        decoder.key(
            key_record(VK_RETURN, 13, SHIFT_PRESSED, true),
            now,
            &mut events,
        );
        assert!(
            matches!(events.last(), Some(AppEvent::Key(key)) if key.code == KeyCode::Enter && key.modifiers == KeyModifiers::SHIFT)
        );
    }

    #[test]
    fn native_unicode_surrogates_keep_separate_press_and_release_state() {
        let now = Instant::now();
        let mut decoder = Decoder::default();
        let mut events = Vec::new();
        for unit in "\u{1f642}".encode_utf16() {
            decoder.key(
                key_record(VK_PACKET, unit, LEFT_ALT_PRESSED, true),
                now,
                &mut events,
            );
            decoder.key(
                key_record(VK_PACKET, unit, LEFT_ALT_PRESSED, false),
                now,
                &mut events,
            );
        }
        assert_eq!(events.len(), 2);
        for (event, kind) in events
            .iter()
            .zip([KeyEventKind::Press, KeyEventKind::Release])
        {
            assert!(
                matches!(event, AppEvent::Key(key) if key.code == KeyCode::Char('\u{1f642}') && key.modifiers == KeyModifiers::ALT && key.kind == kind)
            );
        }
    }

    #[test]
    fn malformed_utf16_is_replaced_without_losing_following_characters() {
        let mut decoder = Utf16::<()>::default();
        assert!(decoder
            .push(0xd800, ())
            .into_iter()
            .flatten()
            .next()
            .is_none());
        let output: Vec<char> = decoder
            .push(b'x' as u16, ())
            .into_iter()
            .flatten()
            .map(|(ch, _)| ch)
            .collect();
        assert_eq!(output, ['\u{fffd}', 'x']);
        assert_eq!(decoder.push(0xdc00, ())[1], Some(('\u{fffd}', ())));
    }

    #[test]
    fn raw_surrogate_repeat_count_repeats_scalars_not_surrogate_halves() {
        let now = Instant::now();
        let mut decoder = Decoder::default();
        let mut events = Vec::new();
        raw(&mut decoder, "\x1b[200~", now, &mut events);
        for unit in "\u{1f642}".encode_utf16() {
            let mut record = key_record(0, unit, 0, true);
            record.wRepeatCount = 2;
            decoder.key(record, now, &mut events);
        }
        raw(&mut decoder, "\x1b[201~", now, &mut events);
        assert!(
            matches!(events.as_slice(), [AppEvent::Paste(text)] if text == "\u{1f642}\u{1f642}")
        );
    }

    #[test]
    fn native_control_modifiers_are_not_reinterpreted_as_raw_enter() {
        let now = Instant::now();
        let mut decoder = Decoder::default();
        let mut events = Vec::new();
        decoder.key(
            key_record(
                VK_J,
                10,
                LEFT_CTRL_PRESSED | RIGHT_ALT_PRESSED | SHIFT_PRESSED,
                true,
            ),
            now,
            &mut events,
        );
        assert!(
            matches!(events.as_slice(), [AppEvent::Key(key)] if key.code == KeyCode::Char('J') && key.modifiers == KeyModifiers::CONTROL | KeyModifiers::ALT | KeyModifiers::SHIFT)
        );
    }

    #[test]
    fn vt_arrows_function_keys_alt_unicode_and_shift_enter_use_public_parser() {
        let now = Instant::now();
        let mut decoder = Decoder::default();
        let mut events = Vec::new();
        for fragment in [
            "\x1b[1;",
            "5A",
            "\x1bO",
            "P",
            "\x1b[15~",
            "\x1b[13;2u",
            "\x1b世界",
        ] {
            raw(&mut decoder, fragment, now, &mut events);
        }
        let keys: Vec<_> = events
            .iter()
            .map(|event| match event {
                AppEvent::Key(key) => (key.code, key.modifiers),
                _ => panic!("not a key"),
            })
            .collect();
        assert_eq!(
            keys,
            [
                (KeyCode::Up, KeyModifiers::CONTROL),
                (KeyCode::F(1), KeyModifiers::NONE),
                (KeyCode::F(5), KeyModifiers::NONE),
                (KeyCode::Enter, KeyModifiers::SHIFT),
                (KeyCode::Char('世'), KeyModifiers::ALT),
                (KeyCode::Char('界'), KeyModifiers::NONE),
            ]
        );
    }

    #[test]
    fn fragmented_sgr_mouse_preserves_down_drag_up_coordinates_and_wheel() {
        let now = Instant::now();
        let mut decoder = Decoder::default();
        let mut events = Vec::new();
        for fragment in [
            "\x1b[<0;",
            "3;4M",
            "\x1b[<32;4;5M",
            "\x1b[<0;4;5m",
            "\x1b[<64;1;1M",
        ] {
            raw(&mut decoder, fragment, now, &mut events);
            decoder.idle(now + Duration::from_secs(60), &mut events);
        }
        let mouse: Vec<_> = events
            .iter()
            .map(|event| match event {
                AppEvent::Mouse(mouse) => (mouse.kind, mouse.column, mouse.row),
                _ => panic!("not a mouse"),
            })
            .collect();
        assert_eq!(
            mouse,
            [
                (MouseEventKind::Down(MouseButton::Left), 2, 3),
                (MouseEventKind::Drag(MouseButton::Left), 3, 4),
                (MouseEventKind::Up(MouseButton::Left), 3, 4),
                (MouseEventKind::ScrollUp, 0, 0),
            ]
        );
    }

    #[test]
    fn native_mouse_and_resize_use_viewport_not_scrollback_buffer() {
        let viewport = Viewport::from_rect(SMALL_RECT {
            Left: 5,
            Top: 100,
            Right: 84,
            Bottom: 123,
        })
        .unwrap();
        assert_eq!((viewport.cols, viewport.rows), (80, 24));
        let mut decoder = Decoder::default();
        let mut events = Vec::new();
        let mut record = MOUSE_EVENT_RECORD {
            dwMousePosition: COORD { X: 7, Y: 103 },
            dwButtonState: FROM_LEFT_1ST_BUTTON_PRESSED,
            dwControlKeyState: SHIFT_PRESSED,
            dwEventFlags: 0,
        };
        decoder.native_mouse(record, viewport, &mut events);
        record.dwEventFlags = MOUSE_MOVED;
        decoder.native_mouse(record, viewport, &mut events);
        record.dwEventFlags = 0;
        record.dwButtonState = 0;
        decoder.native_mouse(record, viewport, &mut events);
        record.dwEventFlags = MOUSE_HWHEELED;
        record.dwButtonState = u32::from((-120i16) as u16) << 16;
        decoder.native_mouse(record, viewport, &mut events);
        let kinds: Vec<_> = events
            .iter()
            .map(|event| match event {
                AppEvent::Mouse(mouse) => {
                    assert_eq!((mouse.column, mouse.row), (2, 3));
                    assert_eq!(mouse.modifiers, KeyModifiers::SHIFT);
                    mouse.kind
                }
                _ => panic!("not a mouse"),
            })
            .collect();
        assert_eq!(
            kinds,
            [
                MouseEventKind::Down(MouseButton::Left),
                MouseEventKind::Drag(MouseButton::Left),
                MouseEventKind::Up(MouseButton::Left),
                MouseEventKind::ScrollLeft
            ]
        );
    }

    #[test]
    fn cleanup_never_overwrites_flags_restored_by_a_panic_hook() {
        let restored = ENABLE_LINE_INPUT | ENABLE_ECHO_INPUT | ENABLE_PROCESSED_INPUT;
        assert_eq!(
            cleanup_mode(restored | ENABLE_VIRTUAL_TERMINAL_INPUT, true),
            restored
        );
        assert_eq!(cleanup_mode(restored, true), restored);
        assert_eq!(
            cleanup_mode(restored | ENABLE_VIRTUAL_TERMINAL_INPUT, false),
            restored | ENABLE_VIRTUAL_TERMINAL_INPUT
        );
    }

    #[test]
    fn win32_packets_keep_shift_enter_release_and_repeat_metadata() {
        let now = Instant::now();
        let mut decoder = Decoder::default();
        let mut events = Vec::new();
        for fragment in ["\x1b[13;0;13;", "1;16;2_", "\x1b[13;0;13;0;16;1_"] {
            raw(&mut decoder, fragment, now, &mut events);
            decoder.idle(now + Duration::from_secs(60), &mut events);
        }
        assert_eq!(events.len(), 3);
        for (event, kind) in events.iter().zip([
            KeyEventKind::Press,
            KeyEventKind::Repeat,
            KeyEventKind::Release,
        ]) {
            assert!(
                matches!(event, AppEvent::Key(key) if key.code == KeyCode::Enter && key.modifiers == KeyModifiers::SHIFT && key.kind == kind)
            );
        }
    }

    #[test]
    fn win32_encapsulated_paste_delimiters_and_unicode_stay_one_draft() {
        let now = Instant::now();
        let mut decoder = Decoder::default();
        let mut events = Vec::new();
        for unit in "\x1b[200~世界🙂\n\x03\x1b[201~".encode_utf16() {
            for down in [1, 0] {
                raw(
                    &mut decoder,
                    &format!("\x1b[0;0;{unit};{down};0;1_"),
                    now,
                    &mut events,
                );
                decoder.idle(now + Duration::from_secs(60), &mut events);
            }
        }
        assert!(matches!(events.as_slice(), [AppEvent::Paste(text)] if text == "世界🙂\n\x03"));
        raw(&mut decoder, "\x1b[13;0;13;1;0;1_", now, &mut events);
        assert!(matches!(events.last(), Some(AppEvent::Key(key)) if key.code == KeyCode::Enter));
    }

    #[test]
    fn release_during_fragmented_wire_prefix_does_not_stick_a_key() {
        let now = Instant::now();
        let mut decoder = Decoder::default();
        let mut events = Vec::new();
        decoder.key(key_record(VK_A, b'a' as u16, 0, true), now, &mut events);
        raw(&mut decoder, "\x1b[200", now, &mut events);
        decoder.key(key_record(VK_A, b'a' as u16, 0, false), now, &mut events);
        raw(&mut decoder, "~ok\x1b[201~", now, &mut events);
        decoder.key(key_record(VK_A, b'a' as u16, 0, true), now, &mut events);
        assert_eq!(events.len(), 3);
        assert!(matches!(&events[1], AppEvent::Paste(text) if text == "ok"));
        assert!(matches!(&events[2], AppEvent::Key(key) if key.kind == KeyEventKind::Press));
    }

    #[test]
    fn malformed_win32_packets_do_not_become_key_actions() {
        let now = Instant::now();
        let mut decoder = Decoder::default();
        let mut events = Vec::new();
        for sequence in [
            "\x1b[13;0;13;2;0;1_",
            "\x1b[13;0;999999;1;0;1_",
            "\x1b[13;0;13;1;0_",
            "\x1b[13;0;13;1;0;1;7_",
        ] {
            raw(&mut decoder, sequence, now, &mut events);
        }
        assert_eq!(events.len(), 4);
        assert!(events
            .iter()
            .all(|event| matches!(event, AppEvent::Notice(_))));
        raw(&mut decoder, "\x1b[200~ok\x1b[201~", now, &mut events);
        assert!(matches!(events.last(), Some(AppEvent::Paste(text)) if text == "ok"));
    }

    #[test]
    fn full_channel_send_is_stoppable_without_a_console_or_tokio_runtime() {
        let (tx, _rx) = tokio::sync::mpsc::channel(1);
        tx.try_send(AppEvent::Notice("full".into())).unwrap();
        let stop = Arc::new(create_stop_event().unwrap());
        let thread_stop = Arc::clone(&stop);
        let worker = thread::spawn(move || {
            send_event(
                &tx,
                AppEvent::Paste("pending".into()),
                &thread_stop,
                &AtomicBool::new(false),
            )
        });
        assert_ne!(unsafe { SetEvent(stop.as_raw_handle()) }, 0);
        assert!(!worker.join().unwrap());
    }
}
