//! Event handling and asynchronous multiplexing for terminal and backend protocol events.

use crossterm::event::{Event as CrosstermEvent, EventStream, KeyEvent, MouseEvent};
use futures::StreamExt;
use std::time::{Duration, Instant};
use tokio::sync::mpsc;
use tokio::task::JoinHandle;
use tokio::time::{Interval, MissedTickBehavior};

use hex_protocol::client::NotificationPacket;
use hex_protocol::models::SubagentInfo;

#[derive(Debug)]
pub enum AppEvent {
    Key(KeyEvent),
    Mouse(MouseEvent),
    Resize(u16, u16),
    Tick,
    Notification(NotificationPacket),
    Notice(String),
    AcpMessage(String),
    AssistantResponse(String),
    Subagents(Result<Vec<SubagentInfo>, String>),
    Ready,
    Error(String),
}

pub struct EventHandler {
    rx: mpsc::Receiver<AppEvent>,
    tx: mpsc::Sender<AppEvent>,
    input: mpsc::Receiver<AppEvent>,
    input_task: JoinHandle<()>,
    ticks: Interval,
}

impl EventHandler {
    pub fn new(tick_rate_ms: u64) -> Self {
        let (tx, rx) = mpsc::channel(128);
        let (input_tx, input) = mpsc::channel(32);
        let input_task = tokio::spawn(async move {
            let mut reader = EventStream::new();
            while let Some(event) = reader.next().await {
                let event = match event {
                    Ok(CrosstermEvent::Key(key)) => Some(AppEvent::Key(key)),
                    Ok(CrosstermEvent::Mouse(mouse)) => Some(AppEvent::Mouse(mouse)),
                    Ok(CrosstermEvent::Resize(w, h)) => Some(AppEvent::Resize(w, h)),
                    Ok(_) => None,
                    Err(err) => Some(AppEvent::Error(err.to_string())),
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
        Self {
            rx,
            tx,
            input,
            input_task,
            ticks,
        }
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
        self.input_task.abort();
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
        let input_task = tokio::spawn(std::future::pending());
        let mut events = EventHandler {
            rx,
            tx,
            input,
            input_task,
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
