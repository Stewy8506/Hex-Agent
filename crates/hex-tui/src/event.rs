//! Event handling and asynchronous multiplexing for terminal and backend protocol events.

use std::time::Duration;
use crossterm::event::{Event as CrosstermEvent, EventStream, KeyEvent, MouseEvent};
use futures::{FutureExt, StreamExt};
use tokio::sync::mpsc;

use hex_protocol::models::JsonRpcNotification;

/// Unified event type delivered to the main loop.
#[derive(Debug, Clone)]
pub enum AppEvent {
    Key(KeyEvent),
    Mouse(MouseEvent),
    Resize(u16, u16),
    Tick,
    Notification(JsonRpcNotification),
    AcpMessage(String),
    Error(String),
}

/// Asynchronous event listener combining Crossterm input and periodic ticks.
pub struct EventHandler {
    rx: mpsc::UnboundedReceiver<AppEvent>,
    tx: mpsc::UnboundedSender<AppEvent>,
}

impl EventHandler {
    /// Create a new event handler with the given tick interval (e.g. 50ms for 20 FPS).
    pub fn new(tick_rate_ms: u64) -> Self {
        let (tx, rx) = mpsc::unbounded_channel();
        let event_tx = tx.clone();

        tokio::spawn(async move {
            let mut reader = EventStream::new();
            let mut tick_interval = tokio::time::interval(Duration::from_millis(tick_rate_ms));

            loop {
                let tick_delay = tick_interval.tick();
                let crossterm_event = reader.next().fuse();

                tokio::select! {
                    _ = tick_delay => {
                        if event_tx.send(AppEvent::Tick).is_err() {
                            break;
                        }
                    }
                    maybe_event = crossterm_event => {
                        match maybe_event {
                            Some(Ok(evt)) => {
                                let app_evt = match evt {
                                    CrosstermEvent::Key(key) => Some(AppEvent::Key(key)),
                                    CrosstermEvent::Mouse(mouse) => Some(AppEvent::Mouse(mouse)),
                                    CrosstermEvent::Resize(w, h) => Some(AppEvent::Resize(w, h)),
                                    _ => None,
                                };
                                if let Some(e) = app_evt {
                                    if event_tx.send(e).is_err() {
                                        break;
                                    }
                                }
                            }
                            Some(Err(err)) => {
                                let _ = event_tx.send(AppEvent::Error(err.to_string()));
                            }
                            None => break,
                        }
                    }
                }
            }
        });

        Self { rx, tx }
    }

    /// Get a cloneable sender to inject events (such as ACP notifications).
    pub fn sender(&self) -> mpsc::UnboundedSender<AppEvent> {
        self.tx.clone()
    }

    /// Receive the next event asynchronously.
    pub async fn next(&mut self) -> Option<AppEvent> {
        self.rx.recv().await
    }
}
