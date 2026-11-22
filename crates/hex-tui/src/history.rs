//! Opt-in, append-only native transcript storage. The archive is never cached in RAM.

use crate::app::{App, DialogueItem, MessageItem, ToolCallItem, ToolResultRef};
use hex_protocol::models::Role;
use serde::{Deserialize, Serialize};
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};

pub const TRANSCRIPT_BUDGET: usize = 2 * 1024 * 1024;
pub const MESSAGE_DISPLAY_BYTES: usize = 64 * 1024;
pub const COMMAND_HISTORY_BUDGET: usize = 256 * 1024;
const METADATA_BYTES: usize = 64 * 1024;
const PAGE_BYTES: usize = 4096;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TranscriptPage {
    pub content: String,
    pub total_bytes: usize,
    pub next_offset: Option<usize>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TranscriptSummary {
    pub content: String,
    pub total_count: usize,
    /// Exclusive upper record ID for the next, older page.
    pub next_before: Option<usize>,
}

#[derive(Serialize, Deserialize)]
#[serde(tag = "kind")]
enum Metadata {
    Message {
        role: Role,
        timestamp: String,
    },
    Tool {
        item: ToolCallItem,
        reference: Option<ToolResultRef>,
    },
}

#[derive(Serialize)]
#[serde(tag = "kind")]
enum MetadataRef<'a> {
    Message {
        role: &'a Role,
        timestamp: &'a str,
    },
    Tool {
        item: &'a ToolCallItem,
        reference: Option<&'a ToolResultRef>,
    },
}

/// Only the session directory and the advisory-locked count handle are retained.
pub struct TranscriptStore {
    path: PathBuf,
    count: File,
}

fn valid_session(id: &str) -> bool {
    !id.is_empty()
        && id.len() <= 128
        && id
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || c == b'_' || c == b'-')
}

fn private_new(path: &Path) -> Result<File, String> {
    let mut options = OpenOptions::new();
    options.read(true).write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    options
        .open(path)
        .map_err(|e| format!("{}: {e}", path.display()))
}

fn regular_file(path: &Path) -> Result<(), String> {
    let meta = fs::symlink_metadata(path).map_err(|e| e.to_string())?;
    if !meta.file_type().is_file() {
        return Err(format!("Not a regular transcript file: {}", path.display()));
    }
    Ok(())
}

fn sync_directory(path: &Path) -> Result<(), String> {
    #[cfg(not(windows))]
    File::open(path)
        .and_then(|file| file.sync_all())
        .map_err(|e| e.to_string())?;
    // Windows flushes writable files, but has no portable directory-fsync guarantee.
    #[cfg(windows)]
    let _ = path;
    Ok(())
}

fn private_directory(path: &Path) -> Result<(), String> {
    let mut builder = fs::DirBuilder::new();
    #[cfg(unix)]
    {
        use std::os::unix::fs::DirBuilderExt;
        builder.mode(0o700);
    }
    let created = match builder.create(path) {
        Ok(()) => true,
        Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => false,
        Err(e) => return Err(format!("{}: {e}", path.display())),
    };
    if !fs::symlink_metadata(path)
        .map_err(|e| e.to_string())?
        .file_type()
        .is_dir()
    {
        return Err(format!(
            "Not a real transcript directory: {}",
            path.display()
        ));
    }
    if created {
        sync_directory(path.parent().ok_or("Transcript directory has no parent")?)?;
    }
    Ok(())
}

fn read_count(file: &mut &File) -> Result<usize, String> {
    file.seek(SeekFrom::Start(0)).map_err(|e| e.to_string())?;
    let mut bytes = [0; 8];
    file.read_exact(&mut bytes).map_err(|e| e.to_string())?;
    usize::try_from(u64::from_le_bytes(bytes)).map_err(|e| e.to_string())
}

fn write_count(file: &mut &File, count: usize) -> Result<(), String> {
    file.seek(SeekFrom::Start(0)).map_err(|e| e.to_string())?;
    file.write_all(&(count as u64).to_le_bytes())
        .map_err(|e| e.to_string())?;
    file.sync_all().map_err(|e| e.to_string())
}

impl TranscriptStore {
    pub fn new(workspace: &Path, session_id: &str) -> Result<Self, String> {
        if !valid_session(session_id) {
            return Err("Invalid ASCII transcript session identifier".to_string());
        }
        let mut path = workspace.canonicalize().map_err(|e| e.to_string())?;
        for component in [".hex", "data", "tui_transcripts", session_id] {
            path.push(component);
            private_directory(&path)?;
        }
        let count_path = path.join("count");
        let count = match private_new(&count_path) {
            Ok(file) => file,
            Err(_) => {
                regular_file(&count_path)?;
                OpenOptions::new()
                    .read(true)
                    .write(true)
                    .open(&count_path)
                    .map_err(|e| e.to_string())?
            }
        };
        let store = Self { path, count };
        store.with_count_lock(|file| {
            if file.metadata().map_err(|e| e.to_string())?.len() == 0 {
                write_count(file, 0)?;
                sync_directory(&store.path)?;
            }
            read_count(file).map(|_| ())
        })?;
        Ok(store)
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    fn with_count_lock<T>(
        &self,
        action: impl FnOnce(&mut &File) -> Result<T, String>,
    ) -> Result<T, String> {
        self.count.lock().map_err(|e| e.to_string())?;
        let result = action(&mut &self.count);
        let unlock = self.count.unlock().map_err(|e| e.to_string());
        match result {
            Err(e) => Err(e),
            Ok(value) => unlock.map(|_| value),
        }
    }

    pub fn total_count(&self) -> Result<usize, String> {
        self.with_count_lock(read_count)
    }

    pub fn append(
        &mut self,
        item: &DialogueItem,
        reference: Option<&ToolResultRef>,
    ) -> Result<usize, String> {
        match item {
            DialogueItem::Message(msg) => self.append_text(&msg.role, &msg.timestamp, &msg.content),
            DialogueItem::ToolCall(tool) => self.append_tool(
                tool,
                reference,
                tool.result.as_deref().unwrap_or(&tool.args),
            ),
        }
    }

    pub(crate) fn append_text(
        &mut self,
        role: &Role,
        timestamp: &str,
        text: &str,
    ) -> Result<usize, String> {
        self.commit(MetadataRef::Message { role, timestamp }, text)
    }

    pub(crate) fn append_tool(
        &mut self,
        item: &ToolCallItem,
        reference: Option<&ToolResultRef>,
        text: &str,
    ) -> Result<usize, String> {
        self.commit(MetadataRef::Tool { item, reference }, text)
    }

    fn commit(&mut self, metadata: MetadataRef<'_>, text: &str) -> Result<usize, String> {
        let json = serde_json::to_vec(&metadata).map_err(|e| e.to_string())?;
        if json.len() > METADATA_BYTES {
            return Err("Transcript tool metadata exceeds its bounded record size".to_string());
        }
        self.with_count_lock(|count| {
            let mut id = read_count(count)?;
            // A crash after publishing metadata but before updating count is recoverable.
            // Incomplete raw-only records fail closed; they are never overwritten.
            while self.path.join(format!("{id}.json")).exists() {
                self.metadata(id)?;
                let raw_path = self.path.join(format!("{id}.txt"));
                regular_file(&raw_path)?;
                OpenOptions::new().read(true).write(true).open(raw_path)
                    .and_then(|file| file.sync_all())
                    .map_err(|e| e.to_string())?;
                OpenOptions::new().read(true).write(true).open(self.path.join(format!("{id}.json")))
                    .and_then(|file| file.sync_all())
                    .map_err(|e| e.to_string())?;
                sync_directory(&self.path)?;
                id = id.checked_add(1).ok_or("Transcript record ID exhausted")?;
                write_count(count, id)?;
            }
            let next = id.checked_add(1).ok_or("Transcript record ID exhausted")?;
            let raw_path = self.path.join(format!("{id}.txt"));
            let meta_path = self.path.join(format!("{id}.json"));
            let mut raw = private_new(&raw_path)?;
            raw.write_all(text.as_bytes())
                .and_then(|_| raw.sync_all())
                .map_err(|e| e.to_string())?;
            let mut meta = private_new(&meta_path)?;
            meta.write_all(&json)
                .and_then(|_| meta.sync_all())
                .map_err(|e| e.to_string())?;
            sync_directory(&self.path)?;
            write_count(count, next)?;
            Ok(id)
        })
    }

    /// Append only the new raw delta, never the resident display marker/tail.
    pub fn append_delta(&mut self, id: usize, delta: &str) -> Result<(), String> {
        if id >= self.total_count()? {
            return Err("Transcript record not found".to_string());
        }
        if !matches!(self.metadata(id)?, Metadata::Message { .. }) {
            return Err("Cannot stream into a tool transcript record".to_string());
        }
        let path = self.path.join(format!("{id}.txt"));
        regular_file(&path)?;
        let mut file = OpenOptions::new()
            .read(true)
            .write(true)
            .open(path)
            .map_err(|e| e.to_string())?;
        file.lock().map_err(|e| e.to_string())?;
        let result = (|| {
            let original = file.seek(SeekFrom::End(0)).map_err(|e| e.to_string())?;
            if let Err(e) = file
                .write_all(delta.as_bytes())
                .and_then(|_| file.sync_all())
            {
                let rollback = file.set_len(original).and_then(|_| file.sync_all());
                return Err(format!(
                    "Transcript delta write failed: {e}; rollback: {rollback:?}"
                ));
            }
            Ok(())
        })();
        let unlock = file.unlock().map_err(|e| e.to_string());
        result.and(unlock)
    }

    fn metadata(&self, id: usize) -> Result<Metadata, String> {
        let path = self.path.join(format!("{id}.json"));
        regular_file(&path)?;
        let file = File::open(path).map_err(|e| e.to_string())?;
        if file.metadata().map_err(|e| e.to_string())?.len() > METADATA_BYTES as u64 {
            return Err("Oversized transcript metadata".to_string());
        }
        serde_json::from_reader(file.take((METADATA_BYTES + 1) as u64)).map_err(|e| e.to_string())
    }

    /// Byte offsets inside a scalar advance to its next boundary. Page ends
    /// retreat to a boundary; callers continue using the returned next_offset.
    pub fn page(&self, id: usize, offset: usize) -> Result<TranscriptPage, String> {
        if id >= self.total_count()? {
            return Err("Transcript record not found".to_string());
        }
        self.metadata(id)?;
        let path = self.path.join(format!("{id}.txt"));
        regular_file(&path)?;
        let mut file = File::open(path).map_err(|e| e.to_string())?;
        file.lock_shared().map_err(|e| e.to_string())?;
        let result = (|| {
            let total_bytes = usize::try_from(file.metadata().map_err(|e| e.to_string())?.len())
                .map_err(|e| e.to_string())?;
            if offset > total_bytes {
                return Err("Transcript byte offset exceeds record size".to_string());
            }
            file.seek(SeekFrom::Start(offset as u64))
                .map_err(|e| e.to_string())?;
            let mut bytes = Vec::with_capacity(PAGE_BYTES);
            (&mut file)
                .take(PAGE_BYTES as u64)
                .read_to_end(&mut bytes)
                .map_err(|e| e.to_string())?;
            let start = bytes.iter().take_while(|b| **b & 0xc0 == 0x80).count();
            let end = match std::str::from_utf8(&bytes[start..]) {
                Ok(_) => bytes.len(),
                Err(e) if e.error_len().is_none() => start + e.valid_up_to(),
                Err(e) => return Err(format!("Invalid UTF-8 transcript source: {e}")),
            };
            let next = offset + end;
            Ok(TranscriptPage {
                content: std::str::from_utf8(&bytes[start..end])
                    .map_err(|e| e.to_string())?
                    .to_string(),
                total_bytes,
                next_offset: (next < total_bytes).then_some(next),
            })
        })();
        let unlock = file.unlock().map_err(|e| e.to_string());
        match result {
            Err(e) => Err(e),
            Ok(page) => unlock.map(|_| page),
        }
    }

    pub fn summary(&self, before: Option<usize>) -> Result<TranscriptSummary, String> {
        let total_count = self.total_count()?;
        let end = before.unwrap_or(total_count).min(total_count);
        let start = end.saturating_sub(32);
        let mut content = String::new();
        for id in start..end {
            let (role, timestamp) = match self.metadata(id)? {
                Metadata::Message { role, timestamp } => (format!("{role:?}"), timestamp),
                Metadata::Tool { item, .. } => (format!("tool {}", item.tool), String::new()),
            };
            let page = self.page(id, 0)?;
            let clean = |text: &str, limit: usize| -> String {
                text.chars()
                    .take(limit)
                    .map(|c| if c.is_control() { ' ' } else { c })
                    .collect()
            };
            let prefix = clean(&page.content, 160);
            let label = clean(&role, 80);
            let time = clean(&timestamp, 80);
            content.push_str(&format!("{id} {label} {time}: {prefix}\n"));
        }
        debug_assert!(content.len() <= METADATA_BYTES);
        Ok(TranscriptSummary {
            content,
            total_count,
            next_before: (start > 0).then_some(start),
        })
    }

    /// Scan one bounded metadata record at a time, newest first. Raw sources
    /// are not opened and no archive-sized index is built.
    pub fn find_tool_reference(&self, ui_id: &str) -> Option<ToolResultRef> {
        let count = self.total_count().ok()?;
        for id in (0..count).rev() {
            if let Ok(Metadata::Tool {
                item,
                reference: Some(reference),
            }) = self.metadata(id)
            {
                if item.id == ui_id {
                    return Some(reference);
                }
            }
        }
        None
    }
}

pub(crate) fn item_bytes(item: &DialogueItem) -> usize {
    match item {
        DialogueItem::Message(msg) => msg.content.capacity() + msg.timestamp.capacity(),
        DialogueItem::ToolCall(tool) => {
            tool.id.capacity()
                + tool.tool.capacity()
                + tool.args.capacity()
                + tool.result.as_ref().map_or(0, String::capacity)
                + tool.exit_status.as_ref().map_or(0, String::capacity)
        }
    }
}

fn ref_bytes(key: &String, reference: &ToolResultRef) -> usize {
    key.capacity() + reference.task_id.capacity() + reference.call_id.capacity()
}

fn diff_bytes(diff: &hex_protocol::diff::UnifiedDiff) -> usize {
    use hex_protocol::diff::{DiffFile, DiffHunk, DiffLine};
    diff.files.capacity() * std::mem::size_of::<DiffFile>()
        + diff
            .files
            .iter()
            .map(|file| {
                file.old_path.as_ref().map_or(0, String::capacity)
                    + file.new_path.as_ref().map_or(0, String::capacity)
                    + file.hunks.capacity() * std::mem::size_of::<DiffHunk>()
                    + file
                        .hunks
                        .iter()
                        .map(|hunk| {
                            hunk.heading.as_ref().map_or(0, String::capacity)
                                + hunk.lines.capacity() * std::mem::size_of::<DiffLine>()
                                + hunk
                                    .lines
                                    .iter()
                                    .map(|line| line.content.capacity())
                                    .sum::<usize>()
                        })
                        .sum::<usize>()
            })
            .sum::<usize>()
}

pub(crate) fn short_prefix(text: &str, limit: usize) -> &str {
    let mut end = text.len().min(limit);
    while !text.is_char_boundary(end) {
        end -= 1;
    }
    &text[..end]
}

pub(crate) fn diff_prefix(text: &str) -> &str {
    // Bound structural allocations as well as raw bytes (tiny lines are costly).
    let bytes = short_prefix(text, MESSAGE_DISPLAY_BYTES - 256);
    let end = bytes.split_inclusive('\n').take(512).map(str::len).sum();
    &bytes[..end]
}

pub(crate) fn recent_tail(text: &str, id: usize) -> String {
    let marker = format!("[Earlier text stored: /transcript {id}]\n");
    let mut start = text
        .len()
        .saturating_sub(MESSAGE_DISPLAY_BYTES - marker.len());
    while !text.is_char_boundary(start) {
        start += 1;
    }
    let mut tail = String::with_capacity(marker.len() + text.len() - start);
    tail.push_str(&marker);
    tail.push_str(&text[start..]);
    tail
}

impl App {
    /// Call only after main has adopted the authoritative session handshake.
    pub fn enable_transcript_storage(&mut self) -> Result<(), String> {
        if let Some(store) = &self.transcript_store {
            let expected = Path::new(&self.workspace_path)
                .canonicalize()
                .map_err(|e| e.to_string())?
                .join(".hex/data/tui_transcripts")
                .join(&self.session_id);
            if store.path == expected {
                return self.transcript_error.clone().map_or(Ok(()), Err);
            }
            if !self.dialogue_items.is_empty()
                || !self.command_history.is_empty()
                || self.raw_diff.is_some()
            {
                let error =
                    "Clear the current transcript before changing its storage session".to_string();
                self.transcript_error = Some(error.clone());
                return Err(error);
            }
        }
        match TranscriptStore::new(Path::new(&self.workspace_path), &self.session_id) {
            Ok(store) => {
                self.transcript_store = Some(store);
                self.transcript_error = None;
                self.enforce_transcript_budget();
                self.transcript_error.clone().map_or(Ok(()), Err)
            }
            Err(e) => {
                self.transcript_error = Some(e.clone());
                Err(e)
            }
        }
    }

    pub(crate) fn archive_resident_transcript(&mut self) -> Result<(), String> {
        if self.transcript_store.is_none() {
            return Ok(());
        }
        if let Some(error) = &self.transcript_error {
            return Err(error.clone());
        }
        for index in 0..self.dialogue_items.len() {
            if self.stored_message_id(index).is_some() {
                continue;
            }
            let item = &self.dialogue_items[index];
            let reference = if let DialogueItem::ToolCall(tool) = item {
                self.tool_result_refs.get(&tool.id)
            } else {
                None
            };
            let id = self
                .transcript_store
                .as_mut()
                .unwrap()
                .append(item, reference)?;
            self.transcript_ids.push((index, id));
        }
        Ok(())
    }

    pub fn flush_transcript(&mut self) -> Result<(), String> {
        let result = self.archive_resident_transcript();
        if let Err(error) = &result {
            self.transcript_error = Some(error.clone());
        }
        result
    }

    pub fn clear_transcript(&mut self) -> Result<(), String> {
        self.flush_transcript()?;
        self.dialogue_items = Vec::new();
        self.active_assistant_index = None;
        self.tool_result_refs = Default::default();
        self.transcript_ids = Vec::new();
        *self.transcript_layout.borrow_mut() = Default::default();
        self.dialogue_scroll = 0;
        self.dialogue_view_offset.set(0);
        self.dialogue_metrics.set((0, 0));
        self.dialogue_entrance.set((0, None));
        self.follow_dialogue = true;
        self.transcript_history_stored = self.transcript_store.is_some();
        Ok(())
    }

    pub fn resident_transcript_bytes(&self) -> usize {
        self.dialogue_items.capacity() * std::mem::size_of::<DialogueItem>()
            + self.dialogue_items.iter().map(item_bytes).sum::<usize>()
            + if self.tool_result_refs.capacity() == 0 {
                0
            } else {
                // HashMap::capacity reports usable slots, not its power-of-two
                // bucket allocation; include buckets and control-byte padding.
                self.tool_result_refs.capacity().next_power_of_two()
                    * (std::mem::size_of::<(String, ToolResultRef)>() + 1)
                    + 16
            }
            + self
                .tool_result_refs
                .iter()
                .map(|(key, reference)| ref_bytes(key, reference))
                .sum::<usize>()
            + self.transcript_ids.capacity() * std::mem::size_of::<(usize, usize)>()
            + self.command_history_bytes()
            + self.raw_diff.as_ref().map_or(0, String::capacity)
            + self.active_diff.as_ref().map_or(0, diff_bytes)
    }

    pub(crate) fn command_history_bytes(&self) -> usize {
        self.command_history.capacity() * std::mem::size_of::<String>()
            + self
                .command_history
                .iter()
                .map(String::capacity)
                .sum::<usize>()
    }

    pub(crate) fn stored_message_id(&self, index: usize) -> Option<usize> {
        self.transcript_ids
            .iter()
            .find_map(|&(resident, stored)| (resident == index).then_some(stored))
    }

    pub(crate) fn shorten_message(&mut self, index: usize) -> Result<(), String> {
        let DialogueItem::Message(msg) = &self.dialogue_items[index] else {
            return Ok(());
        };
        if msg.content.len() <= MESSAGE_DISPLAY_BYTES {
            if msg.content.capacity() > MESSAGE_DISPLAY_BYTES {
                if let DialogueItem::Message(msg) = &mut self.dialogue_items[index] {
                    msg.content.shrink_to_fit();
                }
            }
            return Ok(());
        }
        let id = match self.stored_message_id(index) {
            Some(id) => id,
            None => {
                let id = self
                    .transcript_store
                    .as_mut()
                    .ok_or("Transcript storage not enabled")?
                    .append(&self.dialogue_items[index], None)?;
                self.transcript_ids.push((index, id));
                id
            }
        };
        if let DialogueItem::Message(msg) = &mut self.dialogue_items[index] {
            msg.content = recent_tail(&msg.content, id);
        }
        Ok(())
    }

    fn bound_command_history(&mut self) -> Result<(), String> {
        if self.command_history_bytes() <= COMMAND_HISTORY_BUDGET {
            return Ok(());
        }
        self.command_history.shrink_to_fit();
        while self.command_history_bytes() > COMMAND_HISTORY_BUDGET
            && !self.command_history.is_empty()
        {
            // Archive the complete repeatable command, not a display preview.
            self.transcript_store
                .as_mut()
                .ok_or("Transcript storage not enabled")?
                .append_text(&Role::User, "command", &self.command_history[0])?;
            self.command_history.remove(0);
            self.command_history.shrink_to_fit();
            self.history_index = self.history_index.and_then(|i| i.checked_sub(1));
        }
        Ok(())
    }

    /// Failure is deliberately sticky: sources remain resident, and main must
    /// warn that the normal cap is no longer guaranteed.
    pub fn enforce_transcript_budget(&mut self) {
        if self.transcript_store.is_none() || self.transcript_error.is_some() {
            return;
        }
        if let Err(e) = self.enforce_budget() {
            self.transcript_error = Some(e);
        }
    }

    fn enforce_budget(&mut self) -> Result<(), String> {
        if self.diff_transcript_id.is_none()
            && self.raw_diff.as_ref().is_some_and(|raw| {
                raw.len() > MESSAGE_DISPLAY_BYTES || raw.lines().take(513).count() > 512
            })
        {
            let raw = self.raw_diff.take().unwrap();
            self.load_diff(&raw);
            if let Some(error) = &self.transcript_error {
                return Err(error.clone());
            }
        }
        self.bound_command_history()?;
        for index in 0..self.dialogue_items.len() {
            self.shorten_message(index)?;
        }
        if self.resident_transcript_bytes() <= TRANSCRIPT_BUDGET {
            return Ok(());
        }
        // Shrink spare allocations before deciding which completed prefix to spill.
        self.dialogue_items.shrink_to_fit();
        self.tool_result_refs.shrink_to_fit();
        self.transcript_ids.shrink_to_fit();
        let mut bytes = self.resident_transcript_bytes();
        if bytes <= TRANSCRIPT_BUDGET {
            return Ok(());
        }
        let protected = if self.is_busy || self.active_prompt_id.is_some() {
            self.dialogue_items
                .iter()
                .rposition(|item| {
                    matches!(
                        item,
                        DialogueItem::Message(MessageItem {
                            role: Role::User,
                            ..
                        })
                    )
                })
                .unwrap_or(0)
        } else {
            self.dialogue_items.len()
        };
        let mut drained = 0;
        // A modest low-water margin avoids archiving/draining on every delta.
        let target = TRANSCRIPT_BUDGET - COMMAND_HISTORY_BUDGET;
        for index in 0..protected {
            if bytes <= target {
                break;
            }
            let item = &self.dialogue_items[index];
            if matches!(item, DialogueItem::ToolCall(tool) if tool.exit_status.as_deref() == Some("RUNNING"))
            {
                break;
            }
            let reference = if let DialogueItem::ToolCall(tool) = item {
                self.tool_result_refs.get(&tool.id)
            } else {
                None
            };
            if self.stored_message_id(index).is_none() {
                let id = self
                    .transcript_store
                    .as_mut()
                    .unwrap()
                    .append(item, reference)?;
                if matches!(item, DialogueItem::Message(_)) {
                    self.transcript_ids.push((index, id));
                }
            }
            bytes = bytes.saturating_sub(std::mem::size_of::<DialogueItem>() + item_bytes(item));
            if let DialogueItem::ToolCall(tool) = item {
                if let Some(reference) = self.tool_result_refs.get(&tool.id) {
                    bytes = bytes.saturating_sub(
                        ref_bytes(&tool.id, reference)
                            + std::mem::size_of::<(String, ToolResultRef)>(),
                    );
                }
            }
            drained += 1;
        }
        if drained > 0 {
            self.drain_transcript_range(0, drained);
        }
        if self.is_busy && self.resident_transcript_bytes() > TRANSCRIPT_BUDGET {
            self.trim_completed_active_output(target)?;
        }
        Ok(())
    }

    fn trim_completed_active_output(&mut self, target: usize) -> Result<(), String> {
        while self.resident_transcript_bytes() > target {
            let prompt = self.dialogue_items.iter().rposition(
                |item| matches!(item, DialogueItem::Message(msg) if msg.role == Role::User),
            );
            let eligible = |index: usize, item: &DialogueItem| {
                Some(index) != prompt
                    && Some(index) != self.active_assistant_index
                    && index + 1 < self.dialogue_items.len()
                    && !matches!(item, DialogueItem::ToolCall(tool) if tool.exit_status.as_deref() == Some("RUNNING"))
            };
            let Some(start) = self
                .dialogue_items
                .iter()
                .enumerate()
                .position(|(index, item)| eligible(index, item))
            else {
                break;
            };
            let end = (start..self.dialogue_items.len())
                .find(|&index| !eligible(index, &self.dialogue_items[index]))
                .unwrap_or(self.dialogue_items.len());
            let mut bytes = self.resident_transcript_bytes();
            let mut drained = start;
            for index in start..end {
                if bytes <= target {
                    break;
                }
                let item = &self.dialogue_items[index];
                let reference = match item {
                    DialogueItem::ToolCall(tool) => self.tool_result_refs.get(&tool.id),
                    _ => None,
                };
                if self.stored_message_id(index).is_none() {
                    let id = self
                        .transcript_store
                        .as_mut()
                        .unwrap()
                        .append(item, reference)?;
                    self.transcript_ids.push((index, id));
                }
                bytes =
                    bytes.saturating_sub(std::mem::size_of::<DialogueItem>() + item_bytes(item));
                if let DialogueItem::ToolCall(tool) = item {
                    if let Some(reference) = self.tool_result_refs.get(&tool.id) {
                        bytes = bytes.saturating_sub(ref_bytes(&tool.id, reference));
                    }
                }
                drained = index + 1;
            }
            self.drain_transcript_range(start, drained);
        }
        Ok(())
    }

    fn drain_transcript_range(&mut self, start: usize, end: usize) {
        let count = end - start;
        let layout = self.transcript_layout.borrow();
        let first_row = layout.discarded_rows(start);
        let last_row = layout.discarded_rows(end);
        drop(layout);
        let removed_rows = last_row - first_row;
        let offset = self.dialogue_view_offset.get();
        for item in &self.dialogue_items[start..end] {
            if let DialogueItem::ToolCall(tool) = item {
                self.tool_result_refs.remove(&tool.id);
            }
        }
        self.active_assistant_index = self.active_assistant_index.and_then(|index| {
            if (start..end).contains(&index) { None }
            else if index >= end { Some(index - count) }
            else { Some(index) }
        });
        self.dialogue_items.drain(start..end);
        self.dialogue_items.shrink_to_fit();
        self.tool_result_refs.shrink_to_fit();
        self.transcript_ids.retain_mut(|(index, _)| {
            if (start..end).contains(index) {
                false
            } else {
                if *index >= end {
                    *index -= count;
                }
                true
            }
        });
        self.transcript_ids.shrink_to_fit();
        let (settled, born) = self.dialogue_entrance.get();
        self.dialogue_entrance
            .set((settled - settled.min(end).saturating_sub(start), born));
        self.transcript_history_stored = true;
        if !self.follow_dialogue {
            self.dialogue_scroll = if offset >= last_row {
                offset.saturating_sub(removed_rows)
            } else {
                offset.min(first_row)
            };
            self.dialogue_view_offset.set(self.dialogue_scroll);
            if self.dialogue_scroll == 0 {
                self.status_message =
                    "Earlier dialogue stored: /history; full text: /transcript ID".to_string();
            }
        }
        let (total, viewport) = self.dialogue_metrics.get();
        self.dialogue_metrics
            .set((total.saturating_sub(removed_rows), viewport));
        self.transcript_layout
            .borrow_mut()
            .discard_items(start, end);
    }
}
