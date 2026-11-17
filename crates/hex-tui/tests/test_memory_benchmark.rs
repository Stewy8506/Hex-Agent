use std::alloc::{GlobalAlloc, Layout, System};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::{Duration, Instant};

use hex_tui::app::{App, ScreenState};
use hex_tui::ui;
use ratatui::{backend::TestBackend, Terminal};

struct CountingAllocator;
static LIVE: AtomicUsize = AtomicUsize::new(0);
static PEAK: AtomicUsize = AtomicUsize::new(0);
static ALLOCATED: AtomicUsize = AtomicUsize::new(0);

unsafe impl GlobalAlloc for CountingAllocator {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        let pointer = System.alloc(layout);
        if !pointer.is_null() {
            let live = LIVE.fetch_add(layout.size(), Ordering::Relaxed) + layout.size();
            PEAK.fetch_max(live, Ordering::Relaxed);
            ALLOCATED.fetch_add(layout.size(), Ordering::Relaxed);
        }
        pointer
    }

    unsafe fn dealloc(&self, pointer: *mut u8, layout: Layout) {
        LIVE.fetch_sub(layout.size(), Ordering::Relaxed);
        System.dealloc(pointer, layout);
    }
}

#[global_allocator]
static ALLOCATOR: CountingAllocator = CountingAllocator;

#[test]
#[ignore = "offline release benchmark: cargo test --release -p hex-tui --test test_memory_benchmark -- --ignored --nocapture"]
fn benchmark_transcript_rendering() {
    for width in [60, 120] {
        for count in [100, 1_000, 10_000] {
            let mut app = App::new("benchmark", "offline", "none");
            app.screen_state = ScreenState::Session;
            app.dialogue_items.clear();
            for index in 0..count {
                app.add_assistant_message(&format!(
                    "## Message {index}\n\nA wrapped paragraph with **emphasis**, `code`, and Unicode 世界.\n\n- One blue marker\n- Another entry"
                ));
            }
            let mut terminal = Terminal::new(TestBackend::new(width, 40)).unwrap();
            app.session_started = Instant::now() - Duration::from_secs(60);
            terminal.draw(|frame| ui::draw(frame, &app)).unwrap();
            std::thread::sleep(Duration::from_millis(500));
            let live_before = LIVE.load(Ordering::Relaxed);
            PEAK.store(live_before, Ordering::Relaxed);
            let allocated_before = ALLOCATED.load(Ordering::Relaxed);
            let mut timings = Vec::new();
            for _ in 0..10 {
                let start = Instant::now();
                terminal.draw(|frame| ui::draw(frame, &app)).unwrap();
                timings.push(start.elapsed().as_micros());
            }
            timings.sort_unstable();
            println!(
                "MEMORY_BENCH {}",
                serde_json::json!({
                    "width": width,
                    "messages": count,
                    "frames": timings.len(),
                    "p95_frame_us": timings[9],
                    "allocated_bytes_per_frame": (ALLOCATED.load(Ordering::Relaxed) - allocated_before) / 10,
                    "transient_peak_bytes": PEAK.load(Ordering::Relaxed).saturating_sub(live_before),
                    "retained_bytes_after_frames": LIVE.load(Ordering::Relaxed),
                    "total_rows": app.dialogue_metrics.get().0,
                })
            );
        }
    }
}
