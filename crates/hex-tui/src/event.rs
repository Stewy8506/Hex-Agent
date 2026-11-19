//! Event handling and asynchronous multiplexing for terminal and backend protocol events.

#[cfg(not(windows))]
use crossterm::event::{Event as CrosstermEvent, EventStream};
use crossterm::event::{KeyEvent, MouseEvent};
#[cfg(not(windows))]
use futures::StreamExt;
use std::time::{Duration, Instant};
use tokio::sync::mpsc;
#[cfg(not(windows))]
use tokio::task::JoinHandle;
use tokio::time::{Interval, MissedTickBehavior};

use hex_protocol::client::NotificationPacket;
use hex_protocol::models::{
    ApprovalDecisionResponse, SessionCancelResponse, SessionCommandResponse, SubagentInfo,
};

#[derive(Debug)]
pub enum AppEvent {
    Key(KeyEvent),
    Paste(String),
    Mouse(MouseEvent),
    Resize(u16, u16),
    Tick,
    Notification(NotificationPacket),
    Notice(String),
    ApprovalDecision {
        session_id: String,
        approval_id: String,
        generation: u64,
        decision: String,
        result: Result<ApprovalDecisionResponse, String>,
    },
    CancelResult {
        session_id: String,
        request_id: u64,
        result: Result<SessionCancelResponse, String>,
    },
    CommandResult {
        session_id: String,
        request_id: u64,
        command: String,
        result: Result<SessionCommandResponse, String>,
    },
    PromptText {
        session_id: String,
        request_id: u64,
        text: String,
        first: bool,
    },
    PromptFinished {
        session_id: String,
        request_id: u64,
        result: Result<(), String>,
    },
    ResumeResult {
        session_id: String,
        request_id: u64,
        result: Result<hex_protocol::models::PagedSessionLoad, String>,
    },
    Subagents(Result<Vec<SubagentInfo>, String>),
    Ready,
    Error(String),
    TerminalError(String),
}

pub struct EventHandler {
    rx: mpsc::Receiver<AppEvent>,
    tx: mpsc::Sender<AppEvent>,
    input: mpsc::Receiver<AppEvent>,
    #[cfg(not(windows))]
    input_task: JoinHandle<()>,
    #[cfg(windows)]
    input_reader: Option<crate::windows_input::WindowsInput>,
    ticks: Interval,
}

impl EventHandler {
    pub fn new(tick_rate_ms: u64) -> std::io::Result<Self> {
        let (tx, rx) = mpsc::channel(128);
        let (input_tx, input) = mpsc::channel(32);
        #[cfg(windows)]
        let input_reader = Some(crate::windows_input::WindowsInput::spawn(input_tx)?);
        #[cfg(not(windows))]
        let input_task = tokio::spawn(async move {
            let mut reader = EventStream::new();
            while let Some(event) = reader.next().await {
                let event = match event {
                    Ok(CrosstermEvent::Key(key)) => Some(AppEvent::Key(key)),
                    Ok(CrosstermEvent::Paste(text)) => Some(AppEvent::Paste(text)),
                    Ok(CrosstermEvent::Mouse(mouse)) => Some(AppEvent::Mouse(mouse)),
                    Ok(CrosstermEvent::Resize(w, h)) => Some(AppEvent::Resize(w, h)),
                    Ok(_) => None,
                    Err(err) => Some(AppEvent::TerminalError(err.to_string())),
                };
                if let Some(event) = event {
                    if input_tx.send(event).await.is_err() {
                        break;
                    }
                }
            }
        });
        let mut ticks = tokio::time::interval(Duration::from_millis(tick_rate_ms));
        ticks.set_missed_tick_behavior(MissedTickBehavior::Skip);
        Ok(Self {
            rx,
            tx,
            input,
            #[cfg(not(windows))]
            input_task,
            #[cfg(windows)]
            input_reader,
            ticks,
        })
    }

    pub fn sender(&self) -> mpsc::Sender<AppEvent> {
        self.tx.clone()
    }

    pub async fn next(&mut self) -> Option<AppEvent> {
        tokio::select! {
            biased;
            Some(event) = self.input.recv() => Some(event),
            _ = self.ticks.tick() => Some(AppEvent::Tick),
            event = self.rx.recv() => event,
        }
    }
}

impl Drop for EventHandler {
    fn drop(&mut self) {
        self.input.close();
        #[cfg(not(windows))]
        self.input_task.abort();
        #[cfg(windows)]
        self.input_reader.take();
    }
}

pub struct FramePacer {
    interval: Duration,
    last_draw: Option<Instant>,
    dirty: bool,
}

impl FramePacer {
    pub fn new(interval: Duration) -> Self {
        Self {
            interval,
            last_draw: None,
            dirty: true,
        }
    }

    pub fn mark_dirty(&mut self) {
        self.dirty = true;
    }

    pub fn draw_due(&mut self, now: Instant) -> bool {
        if self.dirty
            && self
                .last_draw
                .is_none_or(|last| now.duration_since(last) >= self.interval)
        {
            self.dirty = false;
            self.last_draw = Some(now);
            true
        } else {
            false
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn terminal_input_and_frame_tick_precede_backend_backlog() {
        let (tx, rx) = mpsc::channel(128);
        let (input_tx, input) = mpsc::channel(32);
        for _ in 0..128 {
            tx.try_send(AppEvent::Ready).unwrap();
        }
        input_tx
            .send(AppEvent::Key(KeyEvent::new(
                crossterm::event::KeyCode::Char('c'),
                crossterm::event::KeyModifiers::CONTROL,
            )))
            .await
            .unwrap();
        #[cfg(not(windows))]
        let input_task = tokio::spawn(std::future::pending());
        let mut events = EventHandler {
            rx,
            tx,
            input,
            #[cfg(not(windows))]
            input_task,
            #[cfg(windows)]
            input_reader: None,
            ticks: tokio::time::interval(Duration::from_millis(50)),
        };
        assert!(matches!(events.next().await, Some(AppEvent::Key(_))));
        tokio::time::timeout(Duration::from_millis(200), async {
            loop {
                match events.next().await {
                    Some(AppEvent::Tick) => break,
                    Some(AppEvent::Ready) => events.tx.send(AppEvent::Ready).await.unwrap(),
                    _ => panic!("unexpected event"),
                }
            }
        })
        .await
        .unwrap();
        assert!(matches!(events.next().await, Some(AppEvent::Ready)));
        assert!(events.sender().try_send(AppEvent::Ready).is_ok());
        assert!(events.sender().try_send(AppEvent::Ready).is_err());
    }

    #[test]
    fn bursts_obey_frame_deadlines_and_idle_does_not_redraw() {
        let start = Instant::now();
        let mut pacer = FramePacer::new(Duration::from_millis(50));
        let mut draws = 0;
        for ms in 0..1000 {
            for _ in 0..10 {
                pacer.mark_dirty();
            }
            draws += usize::from(pacer.draw_due(start + Duration::from_millis(ms)));
        }
        assert_eq!(draws, 20);
        assert!(pacer.draw_due(start + Duration::from_secs(1)));
        assert!(!pacer.draw_due(start + Duration::from_secs(2)));
    }
}
